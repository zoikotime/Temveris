"""
Closed vocabulary for the ECO v2 calculation driver.
====================================================

ZT-SPEC-ECO-002 v2.0 §44.2; owner rulings D8 (rollout), D-F (operational
command convention).

WHAT A DRIVER OUTCOME IS
------------------------
A record of WHICH STAGE was reached and WHAT THAT STAGE ANSWERED. Nothing here
is a calculation result: every value below was produced by the component that
owns it and is carried verbatim. The driver adds no number, no state and no
judgement of its own.

WHY "STAGE REACHED" IS THE UNIT
-------------------------------
The pipeline has five authoritative stages and each one legitimately stops the
chain:

* the producer refuses a day that has not ended, has an open session, or has
  inadmissible evidence;
* EVAL-DAY refuses a ledger it may not evaluate;
* EVAL-EVENT refuses a week that is not COMPLETE — which is the NORMAL answer
  on six days out of seven;
* the notification layer refuses under a SHADOW rollout.

None of those is a failure. A driver that reported a single boolean would have
to call a refused day "failed" and a not-yet-complete week "failed", which
would make a healthy tenant look broken for six days and then healthy on the
seventh. So the unit of reporting is the stage reached, and the reason it
stopped there — the same shape every component below already uses.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence


class DriverStage(str, enum.Enum):
    """How far one Worker-day got. Ordered; each stage implies the ones before.

    Closed. A new stage is a change to the pipeline, not to the driver.
    """

    ROLLOUT_REFUSED = "ROLLOUT_REFUSED"
    """The control is OFF. Nothing was invoked at all (D8)."""

    LEDGER = "LEDGER"
    """The producer answered. Sealed, already sealed, superseded, refused, or
    an integrity fault — its own vocabulary says which."""

    DAY = "DAY"
    """EVAL-DAY answered: a day record was recorded, or the evaluation was
    refused for a reason EVAL-DAY owns."""

    DAY_ALERT = "DAY_ALERT"
    """§24.3's administrator alert was raised for a REQUIREMENT_UNRESOLVED day
    (GV-18). Reached only for that one state."""

    EVENT = "EVENT"
    """EVAL-EVENT answered. It folds the week itself; the driver never calls
    the weekly evaluator directly."""

    NOTIFICATION = "NOTIFICATION"
    """The threshold notification was recorded for the authorized audience
    (GV-07). Recording only — nothing is delivered here."""

    FAILED = "FAILED"
    """A stage raised. This is the ONLY value that means something went wrong;
    every other stage value is an ordinary, expected stopping point."""


#: The stages, in the order the pipeline runs them. A test pins this against
#: the order the service actually calls, so the two cannot drift.
STAGE_ORDER: Sequence[DriverStage] = (
    DriverStage.LEDGER,
    DriverStage.DAY,
    DriverStage.DAY_ALERT,
    DriverStage.EVENT,
    DriverStage.NOTIFICATION,
)


@dataclass(frozen=True)
class WorkerDayResult:
    """What the pipeline did for ONE Worker-day.

    Every optional field below is the verbatim answer of the component that
    owns that stage. ``detail`` carries a component's own reason string and is
    operator-facing: it is never a Worker's name, address, contact detail,
    notification body or reason-for-absence.
    """

    organization_id: int
    user_id: int
    work_date: date
    stage: DriverStage

    #: The producer's own outcome value, e.g. "SEALED", "ALREADY_SEALED",
    #: "REFUSED", "LEDGER_INTEGRITY_FAULT".
    ledger_outcome: Optional[str] = None
    ledger_version: Optional[int] = None

    #: EVAL-DAY's own outcome ("RECORDED" / "REFUSED") and the recorded state.
    day_outcome: Optional[str] = None
    day_state: Optional[str] = None

    #: True only when §24.3's alert was actually raised for this day.
    day_alert_raised: bool = False

    #: EVAL-EVENT's own outcome, e.g. "CROSSED", "ALREADY_CROSSED",
    #: "NOT_CROSSED", "WITHDRAWN", "REFUSED".
    event_outcome: Optional[str] = None
    workweek_key: Optional[str] = None

    #: ALERT's own outcome for the authorized audience.
    notification_outcome: Optional[str] = None

    #: Set ONLY on DriverStage.FAILED — the exception type name, so an operator
    #: can tell a defect from an expected refusal without a traceback.
    failure_stage: Optional[DriverStage] = None
    failure_type: Optional[str] = None

    detail: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.stage is DriverStage.FAILED


@dataclass(frozen=True)
class CalculationPassResult:
    """What ONE invocation did, for ONE organization, for ONE local work date.

    ``processed`` is False only when the rollout control declined. It is NOT a
    success flag: a pass that ran and refused every day is ``processed=True``,
    because the refusals are the answer.
    """

    organization_id: int
    work_date: date
    rollout_state: str
    processed: bool

    #: The event mode every stage ran under, derived ONCE from the rollout
    #: state. SHADOW rollouts produce SHADOW events, which no operational read
    #: returns and no delivery pass selects.
    mode: Optional[str] = None

    workers_selected: int = 0
    results: Sequence[WorkerDayResult] = field(default_factory=tuple)
    detail: Optional[str] = None

    @property
    def failures(self) -> Sequence[WorkerDayResult]:
        """The Worker-days where a stage raised. Empty on a clean pass."""
        return tuple(r for r in self.results if r.failed)

    @property
    def any_failed(self) -> bool:
        return bool(self.failures)


__all__ = [
    "CalculationPassResult",
    "DriverStage",
    "STAGE_ORDER",
    "WorkerDayResult",
]
