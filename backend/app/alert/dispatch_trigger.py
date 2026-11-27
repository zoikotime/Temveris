"""
The authorized D-C trigger — the rollout gate in front of the processing step.
==============================================================================

    run_eco_dispatch_pass(db, *, organization_id=...) -> TriggerResult

WHY A TRIGGER EXISTS AT ALL, GIVEN THE PROCESSOR IS ALREADY CALLABLE
--------------------------------------------------------------------
Because the processor deliberately does not know about rollout. D-C forbids a
second rollout authority, so ``dispatch_processing`` imports nothing from
``app.rollout`` and a test pins that. Something therefore has to consult the
one authority before a pass runs, and this is that something. **The rollout
gate is the entire content of this module** — everything else it does is call
the processor once and hand back what it returned.

WHY THIS IS NOT AN HTTP ROUTE
-----------------------------
The authorization says to find the narrowest existing ECO-owned execution
boundary, and to create a route only if one is genuinely required. There is no
ECO execution path to reuse: the single ECO router exposes one GET
(``/api/eco/day``), a read surface guarded by VIEW-AUTHORITY. Adding an
endpoint would mean inventing a public surface, choosing its authorization
model, and creating a way to invoke notification transport over the network —
none of which the ruling asked for, and the last of which the authorization
explicitly warns against.

So the trigger is a plain, tenant-scoped callable: explicit, deterministic,
internally invocable, and reachable only by code that already holds a
``Session`` and an organization. A route can be added later on top of it
without changing anything here.

CADENCE
-------
ON DEMAND ONLY. One invocation is one pass. There is no loop around the
processor to drain pending work, no scheduler, no startup task and no
``run_forever`` worker — the ruling withheld all of them, and a test asserts
each absence.

WHAT THIS MODULE MUST NEVER DO
------------------------------
Select work · parse a snapshot · resolve a recipient · choose a channel ·
suppress · retry · transport · record a delivery · touch a threshold event.
Every one of those belongs to a layer below it, and duplicating any would
create the second source of truth D-C exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState, rollout_state

from .dispatch_processing import (
    MAX_HANDOFFS_PER_PASS,
    DispatchPassResult,
    process_pending_notification_dispatches,
)

_log = logging.getLogger("zoiko.alert.dispatch_trigger")


@dataclass(frozen=True)
class TriggerResult:
    """What one invocation did, and under which rollout state.

    ``processed`` is False when the gate declined, and ``result`` is then None:
    a pass that never ran has no pass result, and returning an empty one would
    make "declined" indistinguishable from "ran and found nothing".
    """

    organization_id: int
    rollout_state: str
    processed: bool
    result: Optional[DispatchPassResult] = None
    #: The §24.3 day-alert pass's result. A SEPARATE field, not merged into
    #: ``result``: the two primitives have different identities and different
    #: delivery histories, and collapsing them would hide which one did what.
    day_alert_result: Optional[object] = None
    detail: Optional[str] = None

    @property
    def handoffs(self) -> int:
        return self.result.handoffs if self.result else 0


def run_eco_dispatch_pass(
    db: Session, *, organization_id: int,
) -> TriggerResult:
    """Run at most one processing pass for one tenant, if rollout permits.

    ``organization_id`` is REQUIRED and is the tenant scope. It is not an
    optional filter: ``rollout_state`` is per-organization, so a pass without
    one could not be gated, and a trigger that cannot be gated is not a
    trigger. It is passed straight through to the processor, which scopes its
    selection to the same tenant — one tenant resolution, not two.

    Callers supply NOTHING else. There is no parameter for a recipient, a
    channel, a provider, a body, a correlation id, a suppression decision or a
    batch size, so none can be injected: recipients come from the frozen
    snapshot and the batch ceiling lives in the processor.

    Unexpected exceptions PROPAGATE. §34 makes an ordinary delivery problem a
    ``failed`` result rather than an error, so anything that still reaches here
    is a genuine fault, and swallowing it would hide a broken pass behind a
    report of zero work.
    """
    if organization_id is None:
        raise ValueError("organization_id is required: the rollout gate and "
                         "the tenant scope both depend on it")
    organization_id = int(organization_id)

    state = rollout_state(db, organization_id=organization_id,
                          control_key=CONTROL_EARLY_CLOCK_OUT_V2)

    # OFF — and OFF is also the answer to every uncertainty, because
    # `rollout_state` resolves a missing, unreadable or unrecognised control to
    # OFF rather than raising. Nothing is processed, nothing is dispatched, and
    # no delivery record can come into existence.
    if state is RolloutState.OFF:
        return TriggerResult(
            organization_id=organization_id, rollout_state=state.value,
            processed=False, result=None,
            detail="rollout is OFF for this organization: no ECO notification "
                   "dispatch processing")

    # SHADOW — observation only. The processor already excludes SHADOW-mode
    # rows, so this second refusal is defence in depth rather than the
    # guarantee; it is here so a pass cannot reach `dispatch()` even if a row
    # were mis-moded. It is NOT a second authority: it reads the one control
    # and obeys it.
    if state is RolloutState.SHADOW:
        return TriggerResult(
            organization_id=organization_id, rollout_state=state.value,
            processed=False, result=None,
            detail="rollout is SHADOW: work skipped, nothing handed to the "
                   "notification platform and no recipient reported notified")

    # ENABLED — exactly one pass PER PRIMITIVE. No loop, no drain, no retry.
    result = process_pending_notification_dispatches(
        db, organization_id=organization_id, limit=MAX_HANDOFFS_PER_PASS)

    # The §24.3 day alert is a SEPARATE primitive with its own table, its own
    # day-scoped identity and its own delivery history (ruling D-C: B). It gets
    # its own pass rather than widening the one above, so that GV-07's
    # processor stays byte-for-byte what it was promoted on. Both passes share
    # this one rollout gate, which is the point of the gate living here.
    #
    # Imported inside the function: `app.eco_day_alert` imports the
    # notification platform, and a module-level import would make `app.alert`'s
    # import graph depend on it in both directions.
    from app.eco_day_alert import process_pending_day_alert_dispatches

    day_alerts = process_pending_day_alert_dispatches(
        db, organization_id=organization_id)

    _log.info("eco dispatch pass org=%s handoffs=%s recorded=%s truncated=%s "
              "day_alert_handoffs=%s",
              organization_id, result.handoffs, result.recorded,
              result.truncated, day_alerts.handoffs)
    return TriggerResult(
        organization_id=organization_id, rollout_state=state.value,
        processed=True, result=result, day_alert_result=day_alerts)


__all__ = ["TriggerResult", "run_eco_dispatch_pass"]
