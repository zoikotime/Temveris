"""
Reference implementation of the Step 5 ``ConfigVersionProvider`` port.
======================================================================

STEP 6 (non-schema portion) of the owner-authorised Phase 2 foundation.

WHAT THIS IS
------------
An in-memory, storage-free implementation of the configuration-versioning
semantics: immutable versions, effective dating, deterministic historical
lookup, and the Gate 7 historical floor. It is the **executable specification**
the eventual database-backed adapter must match, and a test double every
downstream step can use without a schema.

WHAT THIS IS NOT
----------------
Not the durable store. It holds nothing across a process and is not wired to
any route. The persistent tables are the schema portion of Step 6, which stops
at the design gate pending the owner's migration ruling.

SEMANTICS IT PINS
-----------------
* **Immutable versions.** A version, once added, cannot be edited or removed.
  Correcting configuration means adding a later version, never rewriting an
  earlier one — the same supersede-don't-mutate rule the ledger uses (INV-3).
* **Effective dating, inclusive at the lower bound.** A version effective on
  the 1st governs the 1st.
* **Deterministic lookup.** The version in force on a date is the latest whose
  ``effective_from <= as_of``. Insertion order cannot change the answer.
* **Historical floor (Gate 7 / correction C-06).** A date preceding the first
  captured version resolves to ``None`` — never to live configuration. Falling
  back would make a historical figure move whenever policy is edited, which is
  the §17 defect versioning exists to prevent.
* **Collision rejection.** Two versions cannot share an ``effective_from`` for
  one organization; overlapping ranges are rejected rather than silently
  merged, so "which version governed that day" always has one answer.
* **Organization isolation.** Every read and write is keyed on
  ``organization_id`` (D-09 Option C: identifiers use the platform vocabulary).

ON PRECEDENCE TIER NAMES
------------------------
Tier labels are supplied by the caller and stored verbatim. Per the owner
ruling of this step, §10.1's six precedence labels are **specification-level
labels**, preserved as-is at the domain boundary; they are not renamed to match
the technical identifier vocabulary, and no second vocabulary is invented for
the same rule. This module therefore stores whatever label it is given and
never rewrites it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from app.requirement_resolution.ports import (
    ConfigVersionRef,
    RequirementCandidate,
)

from .errors import (
    ConfigVersionError,
    EffectiveDateCollision,
    ImmutableVersionError,
)


@dataclass(frozen=True)
class _StoredVersion:
    """One immutable captured version and the candidates frozen into it."""

    config_version_id: str
    effective_from: date
    # user_id -> ordered candidates. ``None`` key holds the org-wide default set.
    candidates: Tuple[Tuple[Optional[int], Tuple[RequirementCandidate, ...]], ...]

    def candidates_for(self, user_id: int) -> Tuple[RequirementCandidate, ...]:
        per_user = dict(self.candidates)
        if user_id in per_user:
            return per_user[user_id]
        return per_user.get(None, ())


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class InMemoryConfigVersionProvider:
    """Reference ``ConfigVersionProvider``. Satisfies the Step 5 port."""

    def __init__(self) -> None:
        # organization_id -> versions, kept sorted by effective_from ascending.
        self._versions: Dict[int, List[_StoredVersion]] = {}

    # ── Write side ──────────────────────────────────────────────────────────
    def capture_version(
        self,
        *,
        organization_id: int,
        config_version_id: str,
        effective_from: date | datetime,
        candidates: Optional[Sequence[RequirementCandidate]] = None,
        candidates_by_user: Optional[Dict[int, Sequence[RequirementCandidate]]] = None,
    ) -> ConfigVersionRef:
        """Capture an immutable version. Raises on collision or reuse of an id."""
        eff = _as_date(effective_from)
        existing = self._versions.setdefault(organization_id, [])

        for v in existing:
            if v.config_version_id == config_version_id:
                raise ImmutableVersionError(
                    f"version {config_version_id!r} already captured for "
                    f"organization {organization_id}; versions are immutable"
                )
            if v.effective_from == eff:
                raise EffectiveDateCollision(
                    f"organization {organization_id} already has a version "
                    f"effective {eff.isoformat()} ({v.config_version_id!r}); "
                    f"two versions cannot start on the same date"
                )

        frozen: List[Tuple[Optional[int], Tuple[RequirementCandidate, ...]]] = []
        if candidates is not None:
            frozen.append((None, tuple(candidates)))
        for uid, rows in (candidates_by_user or {}).items():
            frozen.append((uid, tuple(rows)))

        stored = _StoredVersion(
            config_version_id=config_version_id,
            effective_from=eff,
            candidates=tuple(frozen),
        )
        existing.append(stored)
        existing.sort(key=lambda v: v.effective_from)
        return ConfigVersionRef(config_version_id=config_version_id,
                                effective_from=eff)

    # ── Read side — the Step 5 port ─────────────────────────────────────────
    def version_as_of(
        self, *, organization_id: int, as_of: date
    ) -> Optional[ConfigVersionRef]:
        """The version effective on ``as_of``, or ``None`` below the floor.

        Never returns live configuration for a date preceding the first
        captured version.
        """
        on = _as_date(as_of)
        chosen: Optional[_StoredVersion] = None
        for v in self._versions.get(organization_id, ()):
            if v.effective_from <= on:      # inclusive lower bound
                chosen = v                   # list is sorted; last match wins
            else:
                break
        if chosen is None:
            return None
        return ConfigVersionRef(
            config_version_id=chosen.config_version_id,
            effective_from=chosen.effective_from,
        )

    def requirement_candidates(
        self,
        *,
        organization_id: int,
        user_id: int,
        as_of: date,
        version: ConfigVersionRef,
    ) -> Sequence[RequirementCandidate]:
        """Candidates frozen into ONE version. Never blended across versions."""
        for v in self._versions.get(organization_id, ()):
            if v.config_version_id == version.config_version_id:
                return v.candidates_for(user_id)
        return ()

    # ── Introspection, for diagnostics and tests ────────────────────────────
    def earliest_version_date(self, *, organization_id: int) -> Optional[date]:
        rows = self._versions.get(organization_id, ())
        return rows[0].effective_from if rows else None

    def version_count(self, *, organization_id: int) -> int:
        return len(self._versions.get(organization_id, ()))


__all__ = [
    "InMemoryConfigVersionProvider",
    "ConfigVersionError",
    "EffectiveDateCollision",
    "ImmutableVersionError",
]
