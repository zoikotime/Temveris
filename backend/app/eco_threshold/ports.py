"""
The threshold reader's storage seam.
====================================

A separate port from the Step 5 ``ConfigVersionProvider``, deliberately.

WHY NOT EXTEND THE REQUIREMENT PORT
-----------------------------------
``ConfigVersionProvider`` returns ``RequirementCandidate``, which carries no
key — and that absence is load-bearing. It is why the in-memory reference
provider cannot hold a value from another configuration domain, and therefore
why the requirement resolver is structurally incapable of receiving a threshold.
Adding a key dimension, or a threshold method, to that port would spend the
guarantee to save a class.

So the two ports share the storage tables and share the version-selection
algorithm, and share nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol, Sequence, runtime_checkable

from app.requirement_resolution.ports import ConfigVersionRef


@dataclass(frozen=True)
class ThresholdCandidate:
    """One stored row bearing the canonical threshold key.

    A *candidate*, not an answer: the row may be mis-tiered, valueless,
    out-of-range or prohibited per-Worker configuration. Deciding which is the
    reader's job, and it is the reader that fails closed.

    THERE IS DELIBERATELY NO ``rank`` FIELD
    ---------------------------------------
    ``config_value.rank`` exists on the stored row and is meaningless for this
    domain (see ``keys.ECO_THRESHOLD_RANK``). Leaving it off the candidate makes
    "resolve ambiguity by rank" impossible to write rather than merely forbidden
    — the same technique the ledger used when it removed ``workweek_key`` from
    its public API.

    ``scope_user_id`` IS present, and must be: a per-Worker threshold row is
    prohibited configuration that has to be *reported*, so it cannot be filtered
    out of sight on the way here.
    """

    key: str
    tier: str
    scope_user_id: Optional[int]
    value_minutes: Optional[int]


@runtime_checkable
class ThresholdProvider(Protocol):
    """What the reader needs from storage. Two questions, no writes."""

    def version_as_of(
        self, *, organization_id: int, as_of: date
    ) -> Optional[ConfigVersionRef]:
        """The immutable configuration version effective on ``as_of``.

        Step 6 semantics, unchanged: ``effective_from`` is an inclusive lower
        bound, the latest qualifying version wins, and a date preceding the
        earliest capture returns ``None``. It MUST NOT return live configuration
        in that case.
        """
        ...

    def threshold_candidates(
        self, *, organization_id: int, version: ConfigVersionRef
    ) -> Sequence[ThresholdCandidate]:
        """Every row in this version bearing the canonical threshold key.

        For that one key, at BOTH scopes and under ANY tier. Filtering by tier
        or by scope here would hide exactly the malformed configuration the
        reader is required to surface; the narrowing to organization scope is a
        decision, so it belongs where decisions are made and observable.

        One version only. Never blended across versions.
        """
        ...


__all__ = ["ThresholdCandidate", "ThresholdProvider"]
