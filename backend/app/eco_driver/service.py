"""
The ECO v2 calculation driver — orchestration, and nothing else.
================================================================

ZT-SPEC-ECO-002 v2.0 §44.2 ("full evaluation, full event emission, no
notifications and no Worker-facing surfaces"); owner ruling D8 (the durable
rollout control is the one authority); D-F (the operational-command shape).

WHY THIS EXISTS
---------------
Every stage of the ECO v2 chain was built and is covered by executable golden
vectors, but nothing in the running application invoked any of it. A control
that is never invoked produces no shadow data, and §44.2's two complete
workweeks cannot begin. This module is the missing invocation and is deliberately
the thinnest thing that can be: it selects, calls in order, and reports.

WHAT IT MUST NEVER DO
---------------------
It computes nothing. There is no duration arithmetic here, no break
subtraction, no requirement resolution, no leave resolution, no classification,
no shortfall, no weekly total and no threshold comparison. Search this module
for a subtraction and you will not find one. Every number it reports was
produced by the component that owns it and is carried verbatim.

It also does not re-derive identity. ``workweek_key`` is READ from the sealed
ledger row rather than recomputed: W4-1 and owner Ruling 4 make the persisted
key the day's permanent workweek identity, and a second derivation here could
disagree with it after a boundary change.

WHY EVAL-WEEK IS NOT CALLED
---------------------------
``evaluate_threshold_event`` folds the week itself (``eval_event/service.py``,
step 1) and refuses anything that is not COMPLETE. Calling ``evaluate_workweek``
here as well would fold the same week twice per day and give the driver an
opinion about week state that EVAL-EVENT already owns. The established
implementation order is therefore:

    producer -> EVAL-DAY -> (§24.3 alert) -> EVAL-EVENT -> ALERT

and the conceptual "→ evaluate_workweek →" step is satisfied INSIDE EVAL-EVENT.

WHY THERE IS NO ORCHESTRATION-LEVEL IDEMPOTENCY RECORD
------------------------------------------------------
None is needed, and adding one would create a second source of truth about what
has been processed. Every stage is already idempotent against its own
authoritative identity:

* the producer returns ALREADY_SEALED on an unchanged day and supersedes only
  on a genuine source difference (D18-F3-S);
* EVAL-DAY is keyed on (org, worker, date, generation) and a re-evaluation of
  identical evidence does not invent a generation;
* §24.3's alert is unique on (org, worker, date, generation, type, audience,
  mode);
* EVAL-EVENT returns ALREADY_CROSSED against the §21.2 natural key;
* the notification is unique on its six-field episode identity;
* the §24.15 incident is unique on the ledger fault version.

So a repeated invocation re-asks the same questions and gets the same answers,
writing nothing new. That is the whole idempotency story.

TRANSACTIONS
------------
Every component below documents "the caller owns the transaction: this flushes
and never commits". This module is that caller, and it commits **after each
stage**, per Worker-day.

Per stage rather than per pass, because a pass spanning a whole tenant would
hold locks across every Worker and couple unrelated days into one failure. Per
stage rather than per Worker-day, because a failure at stage N must not discard
stages 1..N-1: a sealed ledger is authoritative evidence in its own right, and
rolling it back to "tidy up" a failed evaluation would destroy the very thing
the evaluation was refused over. Prefer resumable over compensating — a retry
re-enters at the first incomplete stage and every earlier stage answers
"already done".

SCOPE
-----
One organization, one local work date, optionally one Worker. There is no
``--all``, no date range and no tenant wildcard, for the reason D-F gave: an
organization cannot be inferred, defaulted or wildcarded.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from app import _tz
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState, rollout_state

from .outcomes import (
    STAGE_ORDER, CalculationPassResult, DriverStage, WorkerDayResult,
)

_log = logging.getLogger(__name__)

#: The actor every stage is attributed to. One label, so a row produced by this
#: driver is distinguishable from one produced by a test or a manual call.
DRIVER_ACTOR = "ECO_CALCULATION_DRIVER"

#: Rollout state -> the event mode every stage runs under. Derived ONCE per
#: pass and passed down; no stage below reads the rollout control itself, which
#: is what keeps D8's "one authority" true.
_MODE_FOR_STATE = {
    RolloutState.SHADOW: "SHADOW",
    RolloutState.ENABLED: "OPERATIONAL",
}


def select_workers(
    db: Session, *, organization_id: int, work_date: date,
) -> Sequence[int]:
    """Workers with clock evidence inside the organization-local work date.

    A SELECTION, not a calculation: it answers "whom should the producer be
    asked about", and the producer then resolves its own authoritative context,
    its own day bounds and its own admissibility for each one. An over-broad
    answer here is harmless — every extra Worker is simply refused — and an
    under-broad one is not, so the window is the org-local day and the filter
    is the existence of a session, not its shape.

    ``local_day_bounds`` is the same DST-correct helper the producer's context
    resolver uses, so the window cannot drift from the one that will be applied.
    """
    from database import models

    day_start_utc, day_end_utc = _tz.local_day_bounds(
        db, organization_id, work_date)

    rows = (
        db.query(models.ClockSession.user_id)
        .filter(
            models.ClockSession.organization_id == organization_id,
            models.ClockSession.clock_in_time >= day_start_utc,
            models.ClockSession.clock_in_time < day_end_utc,
        )
        .distinct()
        .all()
    )
    return tuple(sorted(int(r[0]) for r in rows))


def _ledger_workweek_key(db: Session, *, organization_id: int, user_id: int,
                         work_date: date) -> Optional[str]:
    """The persisted workweek identity of the day, read and never re-derived."""
    from app.work_ledger import DayKey, get_current_ledger

    row = get_current_ledger(db, DayKey(
        organization_id=organization_id, user_id=user_id, work_date=work_date))
    return None if row is None else str(row.workweek_key)


def run_worker_day(
    db: Session, *, organization_id: int, user_id: int, work_date: date,
    mode: str,
) -> WorkerDayResult:
    """Drive the whole chain for ONE Worker-day. Commits after each stage.

    Returns, never raises, for every ordinary outcome — including every stage
    refusal, which is an answer rather than an error. A stage that RAISES is
    caught, rolled back to the last committed stage, and reported as FAILED
    with the stage named, so one Worker-day cannot abort a tenant's pass.
    """
    from app.eval_day import evaluate_and_record_day
    from app.eval_day.recording import RecordingOutcome
    from app.eval_event import EventMode, EventOutcome, evaluate_threshold_event
    from app.work_ledger_producer import produce_and_seal_work_day

    base = dict(organization_id=organization_id, user_id=user_id,
                work_date=work_date)
    stage = DriverStage.LEDGER
    fields: dict = {}

    try:
        # ── 1. LEDGER ───────────────────────────────────────────────────────
        # The producer owns admissibility, the day-closure gate, the frozen
        # context and supersession. §24.15's platform incident is raised INSIDE
        # the ledger writer when a fault is established, so the driver never
        # calls the incident path and cannot forget to.
        produced = produce_and_seal_work_day(db, **base)
        db.commit()
        fields["ledger_outcome"] = produced.outcome.value
        fields["ledger_version"] = produced.ledger_version
        fields["detail"] = produced.detail

        if not produced.has_ledger:
            # Refused, or a fault. Either way there is no ledger to evaluate
            # and the chain legitimately stops. Nothing here repairs it.
            return WorkerDayResult(stage=DriverStage.LEDGER, **base, **fields)

        # ── 2. DAY ──────────────────────────────────────────────────────────
        stage = DriverStage.DAY
        recorded = evaluate_and_record_day(db, **base)
        db.commit()
        fields["day_outcome"] = recorded.outcome.value
        fields["detail"] = recorded.reason or recorded.detail

        if recorded.outcome is not RecordingOutcome.RECORDED \
                or recorded.record is None:
            return WorkerDayResult(stage=DriverStage.DAY, **base, **fields)

        day_record = recorded.record
        fields["day_state"] = str(day_record.state)
        fields["detail"] = None

        # ── 3. DAY ALERT (§24.3 / GV-18) ────────────────────────────────────
        # One state only. The alert primitive enforces that itself; the driver
        # asks only when the recorded state is the one §24.3 attaches to, so a
        # healthy day never reaches the alert path at all.
        from app.eco_day_alert import ALERTABLE_STATE, raise_day_administrator_alert

        if str(day_record.state) == ALERTABLE_STATE:
            stage = DriverStage.DAY_ALERT
            alert = raise_day_administrator_alert(
                db, day_record=day_record, mode=mode, raised_by=DRIVER_ACTOR)
            db.commit()
            # ALREADY_RAISED counts: the alert exists, which is what the field
            # claims. Only a REFUSED leaves no alert at all.
            fields["day_alert_raised"] = alert.alert_id is not None
            fields["detail"] = (alert.refusal.value
                                if alert.refusal is not None else None)
            # A day whose requirement never resolved has no shortfall and no
            # threshold to compare, so the week cannot cross on it. EVAL-EVENT
            # would refuse anyway; stopping here says so honestly.
            return WorkerDayResult(stage=DriverStage.DAY_ALERT, **base, **fields)

        # ── 4. EVENT (EVAL-EVENT, which folds the week itself) ──────────────
        stage = DriverStage.EVENT
        workweek_key = _ledger_workweek_key(db, **base)
        fields["workweek_key"] = workweek_key
        if workweek_key is None:
            fields["detail"] = "no current ledger version carries a workweek key"
            return WorkerDayResult(stage=DriverStage.DAY, **base, **fields)

        event = evaluate_threshold_event(
            db, organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key, mode=EventMode(mode),
            created_by=DRIVER_ACTOR)
        db.commit()
        fields["event_outcome"] = event.outcome.value
        fields["detail"] = (event.refusal.value
                            if event.refusal is not None else None)

        if event.event_id is None:
            # NOT_CROSSED, or refused because the week is not COMPLETE — the
            # normal answer on every day but the last of a week.
            return WorkerDayResult(stage=DriverStage.EVENT, **base, **fields)

        # ── 5. NOTIFICATION (GV-07) ─────────────────────────────────────────
        # Recording only. Delivery is a separate, separately-gated pass
        # (`run_eco_dispatch_pass`), and this driver never transports anything.
        # The audience is left to the default: WORKER is the only audience
        # `AUTHORIZED_AUDIENCES` admits today, and naming a second one here
        # would be the driver asserting an authority it does not have.
        stage = DriverStage.NOTIFICATION
        from app.alert import record_threshold_notification

        note = record_threshold_notification(
            db, event_id=int(event.event_id), created_by=DRIVER_ACTOR)
        db.commit()
        fields["notification_outcome"] = note.outcome.value
        fields["detail"] = (note.refusal.value
                            if note.refusal is not None else None)
        return WorkerDayResult(stage=DriverStage.NOTIFICATION, **base, **fields)

    except Exception as exc:  # noqa: BLE001 - one Worker-day must not abort a pass
        # Roll back to the last COMMITTED stage. Nothing authoritative that was
        # already committed is touched: there is no compensating write here,
        # which would violate the append-only contract the evidence relies on.
        db.rollback()
        _log.exception("eco driver stage failed org=%s user=%s date=%s stage=%s",
                       organization_id, user_id, work_date, stage.value)
        return WorkerDayResult(
            stage=DriverStage.FAILED, **base, **fields,
            failure_stage=stage, failure_type=type(exc).__name__,
        )


def run_eco_calculation_pass(
    db: Session, *, organization_id: int, work_date: date,
    user_id: Optional[int] = None,
) -> CalculationPassResult:
    """Run ONE ECO calculation pass for ONE organization and ONE local date.

    The rollout control is read ONCE, here, and decides two things: whether to
    run at all, and which event mode every stage runs under. No stage below
    reads it again (D8).

    * **OFF** — and OFF is also the answer to every uncertainty, because
      ``rollout_state`` resolves a missing, unreadable or unrecognised control
      to OFF rather than raising. Nothing is invoked and nothing is written.
    * **SHADOW** — the full chain runs and every artifact is produced in SHADOW
      mode. §44.2's "no notifications": SHADOW notifications are refused by
      ``app.alert`` and SHADOW rows are excluded by both dispatch passes, so
      nothing reaches a recipient. §44.2's "no Worker-facing surfaces": the
      read surface admits only ADMINISTRATOR under SHADOW.
    * **ENABLED** — the same chain in OPERATIONAL mode.
    """
    if organization_id is None:
        raise ValueError("organization_id is required: it is the tenant scope")
    organization_id = int(organization_id)
    if isinstance(work_date, datetime):
        work_date = work_date.date()

    state = rollout_state(db, organization_id=organization_id,
                          control_key=CONTROL_EARLY_CLOCK_OUT_V2)

    if state is RolloutState.OFF:
        return CalculationPassResult(
            organization_id=organization_id, work_date=work_date,
            rollout_state=state.value, processed=False,
            detail="rollout is OFF for this organization: no ECO calculation")

    mode = _MODE_FOR_STATE[state]

    workers: Sequence[int] = (
        (int(user_id),) if user_id is not None
        else select_workers(db, organization_id=organization_id,
                            work_date=work_date))

    results: List[WorkerDayResult] = []
    for worker in workers:
        results.append(run_worker_day(
            db, organization_id=organization_id, user_id=worker,
            work_date=work_date, mode=mode))

    _log.info("eco calculation pass org=%s date=%s state=%s workers=%s failed=%s",
              organization_id, work_date.isoformat(), state.value,
              len(workers), sum(1 for r in results if r.failed))

    return CalculationPassResult(
        organization_id=organization_id, work_date=work_date,
        rollout_state=state.value, processed=True, mode=mode,
        workers_selected=len(workers), results=tuple(results))


__all__ = [
    "DRIVER_ACTOR",
    "STAGE_ORDER",
    "run_eco_calculation_pass",
    "run_worker_day",
    "select_workers",
]
