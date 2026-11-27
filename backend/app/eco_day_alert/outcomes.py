"""
The day-scoped administrator alert vocabulary (§24.3, owner ruling D-C: B).
===========================================================================

SEPARATE FROM ``app.alert`` ON PURPOSE
--------------------------------------
``app.alert``'s vocabulary describes a WEEK: a threshold episode identified by
``(organization, worker, workweek_key, tier, sequence)`` and anchored to an
``eco_threshold_event``. §24.3's alert describes a DAY, and a day whose
requirement is unresolved produces **no threshold event at all** — EVAL-WEEK
blocks the week and EVAL-EVENT refuses anything that is not COMPLETE.

Reusing the week-shaped vocabulary would therefore mean either fabricating a
threshold event to hang the alert on, or losing the day identity the alert is
about. Ruling D-C: B forbids the first and this module avoids the second.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class DayAlertType(str, enum.Enum):
    """WHAT the day alert is about. A closed vocabulary of one, today.

    Text rather than a DB enum, the convention ``eco_threshold_event.tier``
    already follows: §32 warns that a value surface cannot be corrected later
    without a breaking change.
    """

    REQUIREMENT_UNRESOLVED = "REQUIREMENT_UNRESOLVED"
    """§24.3. The day's working-time requirement resolved at no rank, so no
    shortfall was calculated and the Worker is shown nothing."""


#: The ONLY audience §33.1 authorises for this row.
#:
#: §33.1's "Requirement unresolved" row reads: WORKER *Never*, PRIMARY ADMIN
#: *Always (operational)*, DIRECT MANAGER *No*, HR ADMIN *No*. Ruling 4
#: authorised exactly that and no more. A frozenset of one, so widening it is a
#: visible edit rather than a quiet addition.
AUTHORIZED_AUDIENCES = frozenset({"PRIMARY_ADMIN"})

#: The audiences that must NEVER receive this alert. Listed explicitly rather
#: than left as "everything else": §24.3 is emphatic that the Worker is shown
#: nothing, and a test asserts each name.
FORBIDDEN_AUDIENCES = frozenset({"WORKER", "DIRECT_MANAGER", "HR_ADMIN"})


class DayAlertOutcome(str, enum.Enum):
    """What one raise-attempt did. Exactly one per call."""

    RAISED = "RAISED"
    """A new immutable day-alert row was appended."""

    ALREADY_RAISED = "ALREADY_RAISED"
    """The alert exists for this exact day generation. Idempotent, not an
    error: the same authoritative evaluation raises one alert."""

    REFUSED = "REFUSED"
    """Nothing was written. The reason is always typed."""


class RefusalReason(str, enum.Enum):
    """Why a run recorded nothing. Never collapsed into one generic reason."""

    STATE_NOT_ALERTABLE = "STATE_NOT_ALERTABLE"
    """The day is not REQUIREMENT_UNRESOLVED. §24.3 attaches the alert to that
    state alone; SHORTFALL, NO_SHORTFALL, DEFERRED_PENDING_LEDGER,
    NOT_A_WORKING_DAY and every other state raise nothing."""

    SHORTFALL_WAS_COMPUTED = "SHORTFALL_WAS_COMPUTED"
    """The record claims REQUIREMENT_UNRESOLVED but also carries a computed
    shortfall. Contradictory evidence, so nothing is asserted about it: §24.3
    requires `shortfall_computed = false` for this state."""

    RECIPIENT_UNRESOLVED = "RECIPIENT_UNRESOLVED"
    """D-1: no PRIMARY ADMIN is designated for this Tenant. Recorded, never
    substituted — not the Worker, not a manager, not an HR seat, not an
    arbitrary administrator. The alert row is still written so the absence is
    itself evidence; only the dispatch has nobody to reach."""

    DAY_RECORD_SUPERSEDED = "DAY_RECORD_SUPERSEDED"
    """The day record has been superseded by a later generation. The alert
    pins an exact generation (§24.3 concerns one authoritative evaluation), so
    a stale generation raises nothing rather than alerting about a day the
    Tenant has already had re-evaluated."""


@dataclass(frozen=True)
class DayAlertResult:
    """The result of one raise-attempt. Returned, never raised, for every
    ordinary outcome."""

    outcome: DayAlertOutcome
    organization_id: int
    user_id: int
    work_date: object
    alert_type: DayAlertType = DayAlertType.REQUIREMENT_UNRESOLVED
    alert_id: Optional[int] = None
    refusal: Optional[RefusalReason] = None
    recipient_user_id: Optional[int] = None
    detail: Optional[str] = None


__all__ = [
    "AUTHORIZED_AUDIENCES",
    "FORBIDDEN_AUDIENCES",
    "DayAlertOutcome",
    "DayAlertResult",
    "DayAlertType",
    "RefusalReason",
]
