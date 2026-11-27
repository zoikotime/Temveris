"""
Temveris — the dedicated working-day classifier (owner ruling D2-A).
=====================================================================

STATUS: IMPLEMENTED, NOT WIRED TO ANY ROUTE. The producer calls it while
sealing; no scheduler, endpoint or existing service does. ECO v1 remains
permanently disabled and ECO v2 rollout remains OFF with zero rows.

WHAT IT IS
----------
The single semantic authority on one question:

    "was work expected from this Worker on this local date?"

    WORKING_DAY · NOT_A_WORKING_DAY · UNCLASSIFIED

It exists because that determination had no owner. The schedule resolver
answered it as one field among many, the producer froze the answer onto the
ledger, and no evaluator ever read it — so `LedgerState.NOT_A_WORKING_DAY` had
no writer and GV-28 could not execute.

WHY IT IS NOT THE PRODUCER, AND NOT THE RESOLVER
------------------------------------------------
D2-A. The producer is an evidence producer: it may request a classification,
freeze it, and pass it through to `seal_day`, but it must not decide what the
context MEANS. `schedule_policy` is a platform-wide scheduling resolver with
no ECO vocabulary; wrapping it here adds an authority boundary, a frozen
historical path and a typed refusal without touching its semantics.

D5-A ratified the resolver's existing precedence, so this package introduces
none of its own — assignment still overrides the weekly pattern, and the
pattern's source label is still whichever tier configured `working_days`.

THE TWO PATHS (D16-A)
---------------------
    classify_working_day(db, ...)       current configuration, at SEAL time
    classification_from_sealed(ledger)  frozen context, for HISTORICAL evaluation

Two functions rather than one with a flag, because D3-A's guarantee must not be
one forgotten keyword away from being lost: the frozen path takes no session,
so it cannot reach configuration.

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* **A new precedence, or a conflict state.** D5-A and D7-A: the ratified
  hierarchy resolves configuration-layer disagreement.
* **Worker Context.** D18-A: the ratified source contract does not require it.
  D10-B keeps WEC admissible as a future INPUT, never as the authority.
* **Any work evidence** — sessions, breaks, offline windows, intervals, leave,
  ECO measurements. D17-A: absence of work is never evidence of a non-working
  day, and being unable to see the work is what guarantees it.
* **A write of any kind**, and any legal reasoning: D1-B settled that this is
  an operational/calendar determination, not a Legal-Matrix question.
* **An `UNKNOWN` evaluation state.** R4's six values are locked; `UNCLASSIFIED`
  is a classifier outcome and is never persisted (D8-A, D17-A).
"""

from .classifier import (
    PERMITTED_WORKING_DAY_SOURCES,
    UNRESOLVED_WORKING_DAY_SOURCE,
    classification_from_sealed,
    classify_working_day,
)
from .outcomes import (
    ClassificationOutcome,
    ClassificationRefusal,
    WorkingDayClassification,
)

__all__ = [
    # the two paths (D16-A)
    "classify_working_day",
    "classification_from_sealed",
    # typed result
    "WorkingDayClassification",
    "ClassificationOutcome",
    "ClassificationRefusal",
    # the ratified source vocabulary (D5-A, D6-A)
    "PERMITTED_WORKING_DAY_SOURCES",
    "UNRESOLVED_WORKING_DAY_SOURCE",
]
