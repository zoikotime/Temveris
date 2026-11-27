"""
Temveris — Intraday leave foundation (Step 7)
==============================================

STATUS: IMPLEMENTED, NOT WIRED. No route, scheduler or existing service calls
into this package. Applying, approving, editing and cancelling leave continue to
work exactly as they do today.

WHAT THIS ADDS
--------------
Leave can now be represented at intraday granularity — explicit, timezone-aware
interval boundaries — instead of only as whole-day or half-day records.

WHAT IT DOES NOT CHANGE
-----------------------
``leave_requests`` remains authoritative for whether leave was granted, for how
many days it consumes, and for its approval state. Every interval set is bound
to a request by a NOT NULL foreign key and describes only WHEN, within the days
that request already covers, leave was taken. There is no second grant, no
second balance, and no second answer to "is this Worker on leave".

Existing whole-day and half-day requests are untouched and un-backfilled. A
request with no interval set means no intraday representation was captured —
which is the truth, and is distinguishable from one whose intervals were
recorded and later withdrawn.

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* **A derivation of boundaries from ``is_half_day`` / ``half_day_period``.**
  The platform has never mapped those labels to clock times, and deciding which
  hours a half-day consumes is a policy — often a legal — question that Steps
  3-4 remain blocked on. Only stated intervals are recorded.
* **Any inference from activity, screenshots, idle time, clock gaps, break
  records or worked time.** Asserted by an import-graph test.
* **Any worked-time calculation.** This layer says when leave was taken, never
  what work it displaced. The single §16.4 duration function is imported from
  the ledger rather than reimplemented, so the two cannot drift; a test pins
  that this is the ONLY thing taken from ``app.work_ledger``.
* **Any jurisdiction-specific classification** — paid/unpaid treatment,
  qualification, medical handling, thresholds. Those are policy interpretations
  resolved at evaluation against a versioned policy; freezing one into the
  evidence would break §17 historical reproducibility.
* **Any mutation.** Both tables are append-only. A correction records a new set
  version; nothing is ever updated or deleted.
"""

from .errors import (
    LeaveIntervalError,
    LeaveIntervalIntegrityFault,
    LeaveIntervalImmutable,
    LeaveIntervalOwnershipMismatch,
    LeaveRequestNotTenanted,
    StaleLeaveIntervalVersion,
)
from .models import LeaveInterval, LeaveIntervalSet
from .service import (
    RecordedSet,
    current_version,
    get_current_set,
    get_set_at_version,
    DayVersionHistory,
    day_version_history,
    intervals_for_day,
    read_intervals,
    record_leave_intervals,
    withdraw_leave_intervals,
)
from .validation import (
    CandidateLeaveInterval,
    LeaveIntervalFault,
    LeaveIntervalValidation,
    LeaveIntervalViolation,
    attributed_work_date,
    duration_seconds,
    order_leave_intervals,
    validate_leave_intervals,
)

__all__ = [
    # models
    "LeaveIntervalSet",
    "LeaveInterval",
    # validation
    "CandidateLeaveInterval",
    "LeaveIntervalFault",
    "LeaveIntervalValidation",
    "LeaveIntervalViolation",
    "validate_leave_intervals",
    "order_leave_intervals",
    "attributed_work_date",
    "duration_seconds",
    # service
    "RecordedSet",
    "record_leave_intervals",
    "withdraw_leave_intervals",
    "read_intervals",
    "intervals_for_day",
    "DayVersionHistory",
    "day_version_history",
    "current_version",
    "get_current_set",
    "get_set_at_version",
    # errors
    "LeaveIntervalError",
    "LeaveIntervalIntegrityFault",
    "LeaveIntervalImmutable",
    "LeaveIntervalOwnershipMismatch",
    "LeaveRequestNotTenanted",
    "StaleLeaveIntervalVersion",
]
