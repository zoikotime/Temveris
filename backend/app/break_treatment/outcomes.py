"""Typed results for break-treatment resolution. Mirrors ``PolicyResolution``."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import FrozenSet, Optional

from .errors import InvalidBreakTreatmentProfile


class ProfileOutcome(str, enum.Enum):
    """Why a resolution answered as it did."""

    RESOLVED = "RESOLVED"
    """Exactly one profile governs the date, and every value is in domain."""

    NO_VERSION = "NO_VERSION"
    """The date is below the organization's earliest capture. NOT an error and
    NOT today's profile — there is simply no recorded answer, and the caller
    fails closed rather than assuming one."""

    AMBIGUOUS = "AMBIGUOUS"
    """Two profiles claim the date. The unique constraint makes this
    unreachable through the service, so reaching it means the store was
    written around — refused rather than resolved by ordering."""

    INVALID = "INVALID"
    """A stored value outside its governed domain, e.g. a negative cap."""


UNRESOLVED_OUTCOMES: FrozenSet[ProfileOutcome] = frozenset({
    ProfileOutcome.NO_VERSION,
    ProfileOutcome.AMBIGUOUS,
    ProfileOutcome.INVALID,
})


@dataclass(frozen=True)
class ProfileResolution:
    """The answer to "which break treatment governed this Tenant on that date?"."""

    outcome: ProfileOutcome
    organization_id: int
    as_of: date

    profile_ref: Optional[str] = None
    effective_from: Optional[date] = None
    content_hash: Optional[str] = None

    meal_qualifies: Optional[bool] = None
    rest_qualifies: Optional[bool] = None
    rest_qualifying_cap_minutes: Optional[int] = None

    #: Set only on INVALID, naming the attribute that failed its domain.
    invalid_reason: Optional[str] = None

    def __post_init__(self) -> None:
        # An unresolved outcome carries no values at all — the same structural
        # refusal ``PolicyResolution`` makes, so a caller that forgot to check
        # the outcome cannot read a value that was never established.
        if self.outcome is not ProfileOutcome.RESOLVED:
            for field in ("profile_ref", "effective_from", "content_hash",
                          "meal_qualifies", "rest_qualifies"):
                if getattr(self, field) is not None:
                    raise InvalidBreakTreatmentProfile(
                        f"outcome {self.outcome.value} cannot carry {field}; "
                        f"an unresolved profile has no value")
        else:
            for field in ("profile_ref", "effective_from", "content_hash",
                          "meal_qualifies", "rest_qualifies"):
                if getattr(self, field) is None:
                    raise InvalidBreakTreatmentProfile(
                        f"RESOLVED requires {field}")
            if self.invalid_reason is not None:
                raise InvalidBreakTreatmentProfile(
                    "RESOLVED cannot carry an invalid_reason")
            # NOTE: rest_qualifying_cap_minutes is deliberately NOT required.
            # §13.2 defines NULL as "uncapped", a real answer, so a resolved
            # profile may legitimately carry None there.

    @property
    def is_resolved(self) -> bool:
        return self.outcome is ProfileOutcome.RESOLVED

    @property
    def is_unresolved(self) -> bool:
        return self.outcome in UNRESOLVED_OUTCOMES


__all__ = ["ProfileOutcome", "ProfileResolution", "UNRESOLVED_OUTCOMES"]
