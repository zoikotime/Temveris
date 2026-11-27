"""
What a policy resolution produced, and why it did not.
=======================================================

The same shape Step 5's ``RequirementResolution`` and Worker Context's
``WorkerContextResolution`` already use: an outcome enum plus a frozen result
whose ``__post_init__`` makes a non-resolved answer structurally unable to carry
a value. That is what stops an UNRESOLVED being read as a policy by a caller
that forgot to check.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import FrozenSet, Optional

from .errors import InvalidPolicyVersion


class PolicyOutcome(str, enum.Enum):
    """Why a resolution did or did not produce a policy."""

    RESOLVED = "RESOLVED"
    """A version effective on or before the date was found, and every governed
    value in it is recognised."""

    NO_VERSION = "NO_VERSION"
    """The date precedes the organization's first captured policy. The
    historical floor: live configuration is NOT substituted, for the same
    reason ``version_as_of`` refuses it — a historical figure that moves when
    today's policy is edited is the §17 defect versioning exists to prevent."""

    AMBIGUOUS = "AMBIGUOUS"
    """More than one version claims the same effective date. The unique
    constraint makes this unreachable through the capture path; it is detected
    anyway so a database that somehow held two rows produces a refusal rather
    than a silently arbitrary winner."""

    INVALID = "INVALID"
    """A stored value is outside its governed domain — a netting mode this
    build does not recognise, or a negative grace. Refused rather than
    corrected: the row was written by some path, and guessing what it meant
    would be inventing policy."""


#: The three outcomes that carry no policy. A membership test rather than an
#: enumeration, so a fifth outcome cannot be missed by a caller.
UNRESOLVED_OUTCOMES: FrozenSet[PolicyOutcome] = frozenset({
    PolicyOutcome.NO_VERSION,
    PolicyOutcome.AMBIGUOUS,
    PolicyOutcome.INVALID,
})


@dataclass(frozen=True)
class PolicyResolution:
    """The answer to "which policy governed this Tenant on that date?"."""

    outcome: PolicyOutcome
    organization_id: int
    as_of: date

    policy_version: Optional[str] = None
    effective_from: Optional[date] = None
    content_hash: Optional[str] = None

    counts_toward_week: Optional[bool] = None
    break_treatment_ref: Optional[str] = None
    netting_mode: Optional[str] = None
    rounding_profile_ref: Optional[str] = None
    grace_minutes: Optional[int] = None

    #: D-6.8 / D-7.1. Optional even on RESOLVED, and deliberately NOT in
    #: the required-field list below: a policy that records no cap and no
    #: horizon is a resolved policy, not an unresolved one. None means the
    #: row carries no value, never a default substituted on its behalf.
    surplus_cap_per_day_minutes: Optional[int] = None
    surplus_cap_per_period_minutes: Optional[int] = None
    retroactive_horizon_days: Optional[int] = None

    #: Set only on INVALID, naming the attribute that failed its domain. The
    #: refusal has to be diagnosable without re-reading the row.
    invalid_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome is not PolicyOutcome.RESOLVED:
            for field in ("policy_version", "effective_from", "content_hash",
                          "counts_toward_week", "netting_mode",
                          "grace_minutes"):
                if getattr(self, field) is not None:
                    raise InvalidPolicyVersion(
                        f"outcome {self.outcome.value} cannot carry {field}; "
                        f"an unresolved policy has no value"
                    )
        else:
            for field in ("policy_version", "effective_from",
                          "counts_toward_week", "netting_mode",
                          "grace_minutes"):
                if getattr(self, field) is None:
                    raise InvalidPolicyVersion(
                        f"RESOLVED requires {field}")
            if self.invalid_reason is not None:
                raise InvalidPolicyVersion(
                    "RESOLVED cannot carry an invalid_reason")

    @property
    def is_resolved(self) -> bool:
        return self.outcome is PolicyOutcome.RESOLVED

    @property
    def is_unresolved(self) -> bool:
        return self.outcome in UNRESOLVED_OUTCOMES


__all__ = [
    "PolicyOutcome",
    "UNRESOLVED_OUTCOMES",
    "PolicyResolution",
]
