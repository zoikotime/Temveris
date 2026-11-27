"""
Temveris — EVAL-WEEK: the weekly attributable shortfall.
=========================================================

ZT-SPEC-ECO-002 v2.0. The third stage of the closed pipeline:

    EVAL-DAY  ->  EVAL-WEEK  ->  EVAL-EVENT  ->  NOTIF

STATUS: IMPLEMENTED, NOT WIRED. No route, service or scheduler calls it.

THE PUBLIC ENTRY POINT

    evaluate_workweek(db, organization_id=..., user_id=..., workweek_key=...)

Takes an IDENTITY, not a list of days, and derives the week's membership from
the persisted ledger. That is owner ruling 3, and the reason for it is precise:
when the caller supplied the days, a week with a day missing was
indistinguishable from a complete one, because omitting the inconvenient day
returned a confident total.

IT PRODUCES W, AND STOPS
------------------------
``W = SUM(contributes_to_accumulator_minutes)`` over the week's days, each value
already rounded once by EVAL-DAY (R-16.5). No second rounding, no seconds, no
mutable accumulator, integer addition so no order dependence.

``W >= T`` is EVAL-EVENT's, by a closed ruling. Nothing here reads a threshold,
decides a crossing, emits an event or sends a notification, and nothing names a
tier or a sequence.

MEMBERSHIP IS NOT PARTICIPATION
-------------------------------
``NOT_A_WORKING_DAY`` is a member of the week and contributes 0 (ruling 1). It
is not a working day, creates no shortfall, and does not make the week
unresolved — but it stays visible in the weekly state set. Excluding it
previously made every real calendar week unresolved, because every real week
contains one.

FAIL-CLOSED, AND SPECIFICALLY (ruling 5)
----------------------------------------
A week is unresolved — never zero, never partial — when it contains a deferred
day, a requirement-unresolved day, or a day whose authoritative
``EcoDayRecord`` is absent or ambiguous, or when its stored membership
contradicts its key. Each is a distinct ``WeeklyState``; none is collapsed into
a generic error, and none carries a weekly total.

An unsealed ledger day and a ledger-integrity-fault day both produce no
``EcoDayRecord`` at all, so both surface as ``DAY_RECORD_MISSING`` — which is
exactly the case a caller-supplied list could have hidden.

NO PERSISTENCE
--------------
A pure evaluator: no weekly table, no migration, no clock read, nothing
written. Identity is ``(organization_id, user_id, workweek_key)`` and
provenance is returned rather than stored (ruling 6). Whether an evaluation is
automatically triggered — the "completed workweek" question — remains deferred
to a later gate (ruling 8).

THE THREE MODULES
-----------------
``membership.py``  which days are in the week, and their day records
``fold.py``        the arithmetic over days already established as the week
``evaluator.py``   the public entry point, and the weekly conditions
"""

from .evaluator import BLOCKING_STATES, CONTRIBUTING_STATES, evaluate_workweek
from .fold import (
    PARTICIPATING_STATES,
    REASON_DEFERRED_DAY,
    REASON_NO_DAYS,
    REASON_PARTICIPATION_NOT_ESTABLISHED,
    REASON_UNRESOLVED_DAY,
    WeekFold,
    fold_week,
)
from .membership import (
    DAYS_IN_WORKWEEK,
    canonical_dates,
    current_ledger_days,
    day_records_for,
)
from .outcomes import BLOCKING_PRECEDENCE, WeekEvaluation, WeeklyState

__all__ = [
    # public entry point
    "evaluate_workweek",
    "WeekEvaluation",
    "WeeklyState",
    "BLOCKING_PRECEDENCE",
    "CONTRIBUTING_STATES",
    "BLOCKING_STATES",
    # membership
    "DAYS_IN_WORKWEEK",
    "canonical_dates",
    "current_ledger_days",
    "day_records_for",
    # the inner fold
    "WeekFold",
    "fold_week",
    "PARTICIPATING_STATES",
    "REASON_NO_DAYS",
    "REASON_DEFERRED_DAY",
    "REASON_UNRESOLVED_DAY",
    "REASON_PARTICIPATION_NOT_ESTABLISHED",
]
