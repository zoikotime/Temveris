"""
Temveris — EVAL-DAY: the deterministic day-level ECO evaluation
================================================================

STATUS: IMPLEMENTED, NOT WIRED, NOTHING EVALUATED. No route, scheduler or
existing service calls this. ECO v1 remains permanently disabled and ECO v2
rollout remains OFF with zero rows.

WHAT IT IS
----------
The first layer that computes anything. Steps 1-8 and Worker Context built
primitives; this folds one sealed Worker-day into one derived DayRecord:
qualifying minutes, gross shortfall, attributable shortfall.

    sealed ledger (authoritative input)  ->  DayRecord (derived output)

THE AUTHORISED SLICE, AND ONLY IT
---------------------------------
WRK-only days. Everything else fails closed:

* any non-WRK interval — TRV, ONC, LVE, OFF, UNK, BRK_M, BRK_R (R6);
* ledger integrity fault (R10);
* unresolved requirement, recorded with no minutes at all (R13);
* leave whose grant cannot be established.

MODEL-EXCLUSION IS AUTHORISED
-----------------------------
Approved-leave overlap reduces an attributable shortfall and never creates
credit. The resolved input is frozen into an immutable ``LeaveInputSnapshot``
whose key is the seventh component of the day record's snapshot identity
(Owner Ruling 2), so a historical evaluation is reproducible without re-reading
mutable leave state, and a changed leave input produces a NEW record rather
than a conflict.

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* **A break classifier.** No BRK_M/BRK_R logic, no jurisdiction, no legal
  treatment. Step 3 is separately gated on the Legal Matrix.
* **Weekly anything** — accumulation, thresholds, crossing, de-crossing,
  banking. EVAL-WEEK is not authorised.
* **Events or notifications.** No emission, no dedup, no advisory.
* **A second rounding site.** ``rounding.round_half_up_minutes`` is the only
  seconds-to-minutes conversion, and a test pins that.
* **Any use of ``_worked.py``**, raw clock/break sessions, screenshots,
  activity, application names, idle scoring or ML. Enforced by an import-graph
  test.
* **Any employment-action, recommendation, disciplinary or notification field on
  the DayRecord** (R3). INV-9: nothing here labels a Worker.

OPEN DECISIONS THIS INCREMENT DID NOT RESOLVE
---------------------------------------------
* ``EVALUABLE`` and ``DEFERRED_PENDING_LEDGER`` are declared by R4 but emitted
  by no code path — R4 gives the values, not the transitions, and R9/R18 forbid
  inventing them.
* ``MISSING_CLOCK_OUT`` and ``LEDGER_INTEGRITY_FAULT`` are ledger states with no
  evaluation counterpart. That shape is unchanged, but the conclusion drawn
  from it here was wrong for GV-16: the vector was satisfied WITHOUT adding a
  day state, by asserting the ledger-level fact beside the ECO-level outcome
  (``test_departure_resolution.py::TestGV16``), and it is promoted. GV-26
  remains blocked — on the platform-incident primitive and on which
  vocabulary its ``state`` field names, neither of which is settled here.
* ``policy_version`` is no longer always NULL. A distinct ``PolicyVersion``
  artifact now exists (``app.policy_version``), is resolved historically and is
  consumed at the evaluation boundary; §22.1's day-evaluation natural key
  names it, and GV-25 was promoted on exactly that basis. It IS still NULL
  wherever no version governs the date — that is resolution, not absence.
* ``schedule_variance_minutes`` is never populated: R12 forbids inventing
  schedule-variance mathematics.
"""

from .errors import (
    DayRecordAuthorityContention,
    DayRecordConflict,
    DayRecordImmutable,
    LeaveInputSnapshotRequired,
    EvalDayError,
    EvaluationRefused,
    REASON_EXCLUSION_REQUIRED,
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_MISSING_CLOCK_OUT,
    REASON_LEDGER_NOT_SEALED,
    REASON_NON_WRK_STATE_PRESENT,
    REASON_ORGANIZATION_REQUIRED,
)
from .evaluator import (
    DayEvaluation,
    QUALIFYING_STATES,
    evaluate_day,
    get_day_record,
    readiness,
    record_evaluation,
)
from .exclusion import (
    APPROVED_LEAVE_STATUSES,
    NON_APPROVED_LEAVE_STATUSES,
    REASON_LEAVE_AUTHORITY_UNRESOLVED,
    UNRULED_LEAVE_STATUSES,
    LeaveOverlap,
    LeaveOverlapOutcome,
    approved_leave_overlap,
)
from .leave_snapshot import (
    LeaveContribution,
    LeaveInputContribution,
    LeaveInputSnapshot,
    LeaveSnapshotImmutable,
    compute_leave_snapshot_hash,
    get_or_create_leave_snapshot,
    leave_interval_fingerprint,
    read_contributions,
    read_leave_snapshot,
)
from .models import EcoDayRecord, compute_result_hash
from .recording import (
    DayRecordingResult,
    REASON_NO_LEDGER_DAY,
    REASON_RESULT_CONFLICT,
    RecordingOutcome,
    evaluate_and_record_day,
)
from .rounding import round_half_up_minutes
from .states import (
    COMPUTED_STATES,
    DEFERRED_STATES,
    EcoDayState,
    PERSISTABLE_STATES,
    UNCOMPUTED_STATES,
    UNEMITTED_IN_THIS_INCREMENT,
    is_valid_state,
)

__all__ = [
    # states
    "EcoDayState",
    "COMPUTED_STATES",
    "UNCOMPUTED_STATES",
    "DEFERRED_STATES",
    "PERSISTABLE_STATES",
    "UNEMITTED_IN_THIS_INCREMENT",
    "is_valid_state",
    "readiness",
    # rounding
    "round_half_up_minutes",
    # model
    "EcoDayRecord",
    "compute_result_hash",
    # evaluator
    "DayEvaluation",
    "QUALIFYING_STATES",
    "evaluate_day",
    "record_evaluation",
    "get_day_record",
    # The immutable leave input snapshot (Owner Ruling 2)
    "LeaveInputSnapshot",
    "LeaveInputContribution",
    "LeaveContribution",
    "LeaveSnapshotImmutable",
    "compute_leave_snapshot_hash",
    "leave_interval_fingerprint",
    "get_or_create_leave_snapshot",
    "read_leave_snapshot",
    "read_contributions",
    # MODEL-EXCLUSION — approved-leave overlap
    "approved_leave_overlap",
    "LeaveOverlap",
    "LeaveOverlapOutcome",
    "APPROVED_LEAVE_STATUSES",
    "NON_APPROVED_LEAVE_STATUSES",
    "UNRULED_LEAVE_STATUSES",
    "REASON_LEAVE_AUTHORITY_UNRESOLVED",
    # recording boundary — sealed ledger -> EVAL-DAY -> immutable record
    "evaluate_and_record_day",
    "DayRecordingResult",
    "RecordingOutcome",
    "REASON_NO_LEDGER_DAY",
    "REASON_RESULT_CONFLICT",
    # errors
    "EvalDayError",
    "EvaluationRefused",
    "DayRecordConflict",
    "DayRecordImmutable",
    "LeaveInputSnapshotRequired",
    "DayRecordAuthorityContention",
    "REASON_LEDGER_INTEGRITY_FAULT",
    "REASON_LEDGER_MISSING_CLOCK_OUT",
    "REASON_LEDGER_NOT_SEALED",
    "REASON_NON_WRK_STATE_PRESENT",
    "REASON_EXCLUSION_REQUIRED",
    "REASON_ORGANIZATION_REQUIRED",
]
