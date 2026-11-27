"""
In-memory reference ``ThresholdProvider``.
==========================================

The executable specification of the storage semantics, and a test double for
callers that should not need a database — the same role
``InMemoryConfigVersionProvider`` plays for Step 6, and held to the same
standard: one shared contract suite runs against this and the database-backed
adapter, so neither can drift.

DB-FREE
-------
No session, no engine, no SQLAlchemy model. Importing this must not require a
database to exist.

IMMUTABLE BY CONSTRUCTION — AND NOT A CONFIGURATION STORE
---------------------------------------------------------
Versions are supplied once, to the constructor, and copied into tuples. There is
no capture, update or delete method: this is a fixture, not a second place
configuration can live. Anything that eventually captures a threshold will do it
through the real immutable ``ConfigVersion``/``ConfigValue`` tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.requirement_resolution.ports import ConfigVersionRef

from .ports import ThresholdCandidate


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


@dataclass(frozen=True)
class StoredThresholdVersion:
    """One captured version and the threshold rows frozen into it.

    ``candidates`` holds rows bearing the canonical threshold key only — the
    same contract the database adapter's query implements. Rows of other
    configuration domains are simply not this provider's business, which is why
    it has no key dimension beyond that.
    """

    config_version_id: str
    effective_from: date
    candidates: Tuple[ThresholdCandidate, ...] = ()


class InMemoryThresholdProvider:
    """Reference ``ThresholdProvider``. Satisfies the port with no database."""

    def __init__(
        self,
        versions_by_organization: Optional[
            Mapping[int, Iterable[StoredThresholdVersion]]
        ] = None,
    ) -> None:
        stored: Dict[int, Tuple[StoredThresholdVersion, ...]] = {}
        for organization_id, versions in (versions_by_organization or {}).items():
            # Sorted ascending by effective date so the scan below can take the
            # last match, mirroring the adapter's ORDER BY ... DESC LIMIT 1.
            # Sorted here rather than trusted from the caller: the order rows
            # were supplied in must not be able to change an answer.
            stored[int(organization_id)] = tuple(
                sorted(versions, key=lambda v: v.effective_from)
            )
        self._versions = stored

    # ── Version selection — Step 6 semantics, reproduced exactly ────────────
    def version_as_of(
        self, *, organization_id: int, as_of: date
    ) -> Optional[ConfigVersionRef]:
        """The version effective on ``as_of``, or ``None`` below the floor.

        Inclusive lower bound; the latest qualifying version wins. Never
        substitutes live configuration for a date below the historical floor —
        there is no live configuration here to substitute, which is part of why
        this implementation is the specification.
        """
        on = _as_date(as_of)
        chosen: Optional[StoredThresholdVersion] = None
        for version in self._versions.get(int(organization_id), ()):
            if version.effective_from <= on:
                chosen = version          # list is sorted; last match wins
            else:
                break
        if chosen is None:
            return None
        return ConfigVersionRef(
            config_version_id=chosen.config_version_id,
            effective_from=chosen.effective_from,
        )

    # ── The threshold rows in that version ──────────────────────────────────
    def threshold_candidates(
        self, *, organization_id: int, version: ConfigVersionRef
    ) -> Sequence[ThresholdCandidate]:
        """Candidates frozen into ONE version. Never blended across versions.

        Returned at every scope and tier, exactly as the database adapter does,
        so the reader sees a malformed row here too rather than a tidied-up view
        that would let a defect pass in tests and fail in production.
        """
        for stored in self._versions.get(int(organization_id), ()):
            if stored.config_version_id == version.config_version_id:
                ordered: List[ThresholdCandidate] = sorted(
                    stored.candidates,
                    # Matches the adapter's ORDER BY: tier, then scope, with a
                    # sentinel so NULL scope sorts first and the sort stays
                    # total (None is not comparable with int).
                    key=lambda c: (
                        c.tier,
                        c.scope_user_id if c.scope_user_id is not None else -1,
                    ),
                )
                return tuple(ordered)
        return ()


__all__ = ["InMemoryThresholdProvider", "StoredThresholdVersion"]
