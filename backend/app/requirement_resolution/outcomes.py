"""
Strict requirement resolution — outcomes and result type.
=========================================================

STEP 5 of the owner-authorised Phase 2 foundation.

THE RULE
--------
ZT-SPEC-ECO-002 §10.2:

    "If no rank yields a value, the day is REQUIREMENT_UNRESOLVED. Shortfall
    MUST NOT be computed, MUST NOT be assumed zero and MUST NOT be assumed to
    be the default."

§24.3 adds that the Worker MUST NOT be shown anything about it — it is a Tenant
configuration defect, and surfacing it as an attendance condition would be a
false statement about them.

TWO DISTINCT NON-RESOLVED OUTCOMES — DELIBERATELY NOT MERGED
------------------------------------------------------------
``NO_VERSION`` and ``REQUIREMENT_UNRESOLVED`` are different findings and the
owner ruling is explicit that they must not be conflated:

* ``NO_VERSION`` — the requested date predates the first captured configuration
  version. Configuration *history does not reach back that far*. There is no
  policy to consult, correct or fix; the honest answer is that none was
  captured. (Gate 7 historical floor / correction C-06.)
* ``REQUIREMENT_UNRESOLVED`` — configuration for that date DOES exist, and no
  rank in it yielded a requirement. That is a live Tenant configuration defect
  an administrator can and should fix. (§10.2 / D-10.)

Both fail closed. They differ in what an operator does next, which is why the
distinction is worth keeping in the type rather than in a comment.

WHAT A NON-RESOLVED OUTCOME MUST NEVER CARRY
--------------------------------------------
``required_minutes`` is ``None`` unless the outcome is ``RESOLVED``. Never 0,
never a default, never a "best guess". A zero would silently become "no
shortfall"; a default would silently become "a shortfall against a number
nobody configured".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Tuple


class ResolutionOutcome(str, enum.Enum):
    """Why the resolver returned what it returned."""

    RESOLVED = "RESOLVED"
    """A rank yielded a valid requirement. ``required_minutes`` is set."""

    NO_VERSION = "NO_VERSION"
    """The date predates the first captured configuration version. There is no
    historical policy to resolve against, and live configuration MUST NOT be
    substituted (Gate 7 historical floor)."""

    REQUIREMENT_UNRESOLVED = "REQUIREMENT_UNRESOLVED"
    """Configuration exists for the date but no rank yielded a requirement.
    §10.2: shortfall MUST NOT be computed, assumed zero, or assumed default."""


#: Outcomes that permit no evaluation of any kind.
NON_EVALUABLE_OUTCOMES = frozenset({
    ResolutionOutcome.NO_VERSION,
    ResolutionOutcome.REQUIREMENT_UNRESOLVED,
})


@dataclass(frozen=True)
class InvalidCandidate:
    """A configured value that could not be used, and why.

    Recorded rather than discarded. A malformed requirement is a configuration
    defect an administrator needs to see; silently skipping it and falling to a
    lower rank would hide the defect and produce a plausible-looking figure
    derived from the wrong tier.
    """

    tier: str
    rank: int
    raw_value: object
    reason: str


@dataclass(frozen=True)
class RequirementResolution:
    """The outcome of resolving one Worker's requirement on one date.

    Immutable, and carries enough provenance for a downstream evaluation to be
    reproduced years later (§35): which tier won, which source object supplied
    it, which configuration version was in force, and which ranks were
    consulted on the way.
    """

    outcome: ResolutionOutcome
    organization_id: int
    user_id: int
    as_of: date

    #: Set ONLY when outcome is RESOLVED. Never 0, never a default.
    required_minutes: Optional[int] = None

    #: Provider-declared precedence tier that supplied the value, e.g.
    #: "assignment" / "shift" / "employee" / "team" / "org" / "system".
    #: The resolver does not own this vocabulary — see ``resolver`` module.
    source_tier: Optional[str] = None
    source_rank: Optional[int] = None
    source_ref: Optional[str] = None

    #: Immutable configuration version the resolution was made against.
    config_version_id: Optional[str] = None
    effective_from: Optional[date] = None

    #: Tiers consulted, in precedence order, for audit (§10.2 chain_evaluated).
    chain_evaluated: Tuple[str, ...] = ()

    #: Configured values that were present but unusable.
    invalid_candidates: Tuple[InvalidCandidate, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.outcome is ResolutionOutcome.RESOLVED

    @property
    def is_evaluable(self) -> bool:
        """Whether ECO evaluation may proceed for this Worker-day.

        False for both non-resolved outcomes. A caller that ignores this and
        reads ``required_minutes`` gets ``None``, not a number — the type makes
        the fail-closed rule hard to bypass by accident.
        """
        return self.outcome is ResolutionOutcome.RESOLVED

    @property
    def requires_administrator_alert(self) -> bool:
        """§10.2: an unresolved requirement is an operational alert to the
        Tenant administrator, not a Worker-facing condition.

        ``NO_VERSION`` does not alert: configuration history simply does not
        reach that far back, which is a property of the cutover rather than a
        defect an administrator can fix.
        """
        return self.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED

    @property
    def is_worker_visible(self) -> bool:
        """§24.3: the Worker MUST NOT be shown anything about an unresolved
        requirement. Always False for non-resolved outcomes."""
        return self.outcome is ResolutionOutcome.RESOLVED

    def __post_init__(self) -> None:
        # The invariant that matters most, enforced by construction rather than
        # by convention: a non-resolved outcome cannot carry a number.
        if self.outcome is not ResolutionOutcome.RESOLVED:
            if self.required_minutes is not None:
                raise ValueError(
                    f"{self.outcome.value} must not carry required_minutes "
                    f"(got {self.required_minutes!r}); §10.2 forbids assuming "
                    f"zero or a default"
                )
        elif self.required_minutes is None:
            raise ValueError("RESOLVED must carry required_minutes")


__all__ = [
    "ResolutionOutcome",
    "NON_EVALUABLE_OUTCOMES",
    "InvalidCandidate",
    "RequirementResolution",
]
