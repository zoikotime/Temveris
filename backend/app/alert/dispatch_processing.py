"""
ALERT-DELIVERY — the ECO-owned persist-first processing step (ruling D-C: B).
============================================================================

    process_pending_notification_dispatches(db, ...) -> DispatchPassResult

THE RULING THIS IMPLEMENTS
--------------------------
D-C: B. Threshold-event creation commits with NO dispatch; a separate
ECO-owned step later hands committed work to the notification platform.

    Cadence            on demand only. No recurring loop is authorized.
    Selection          committed events with a frozen recipient snapshot and no
                       completed ECO delivery result for that recipient.
    Batch bound        at most 50 recipient handoffs per pass.
    Execution          no run_forever loop; invoked only from a separately
                       authorized trigger, which does NOT exist yet.

WHY THIS IS SAFE TO CALL REPEATEDLY
-----------------------------------
Because ECO implements no retry, no queue and no dispatch state of its own.
Eligibility is DERIVED on every pass from three persisted facts — the event,
its frozen snapshot, and the delivery records — so there is no flag to get out
of step with reality and no second source of truth about what has been sent.
Re-handing a recipient is harmless because D-4 makes the platform return the
original result without transporting again.

THE UNIT OF WORK IS A NOTIFICATION ROW, NOT AN EVENT
-----------------------------------------------------
``EcoNotification`` already carries everything a handoff needs — the
originating event, the recipient identity, the frozen snapshot, the rendered
body and the mode — and its own docstring says so: *"Delivery, whenever it is
built, sends THIS text."* So this step delivers notifications that ALERT has
already recorded; it never decides that a notification should exist. That
division is exactly the ALERT / ALERT-DELIVERY boundary: ALERT is the authority
on WHETHER, this module on WHETHER IT WENT OUT.

WHAT THIS MODULE MUST NEVER DO
------------------------------
Re-resolve recipient authority · select a channel · resolve preferences ·
suppress · retry · send email · mutate the threshold event · create a
dispatch-pending flag, queue column or second idempotency system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.notification_platform import (
    DispatchMode,
    DispatchOutcome,
    NotificationHandoff,
    dispatch,
)

from .models import EcoNotification, EcoNotificationDeliveryAttempt

_log = logging.getLogger("zoiko.alert.dispatch_processing")

#: D-C, ruled: a HARD maximum, never a default a caller may raise. A pass that
#: finds more eligible recipients stops at this bound and leaves the rest for
#: the next separately authorized invocation.
MAX_HANDOFFS_PER_PASS = 50

#: The outcome that makes a recipient ineligible for further handoff.
#:
#: ``failed`` deliberately does NOT exclude: D-C's replay example states that a
#: failed recipient "may be handed to the platform again according to the
#: existing platform idempotency/retry semantics". ECO therefore re-hands it
#: and lets the platform decide — which under D-4 means replaying the original
#: result without transporting. ECO implements no retry policy of its own.
COMPLETED_OUTCOME = DispatchOutcome.DISPATCHED.value


@dataclass(frozen=True)
class DispatchPassOutcome:
    """What one recipient handoff did."""

    notification_id: int
    eco_event_id: int
    recipient_user_id: int
    outcome: str
    reason_code: str
    channel: Optional[str]
    correlation_id: str
    #: False when the result carried no channel, so no ECO delivery record was
    #: written. Not a failure of this step — see ``_record_attempt``.
    recorded: bool


@dataclass(frozen=True)
class DispatchPassResult:
    """One pass. Deterministic and fully described."""

    handoffs: int
    recorded: int
    skipped_shadow: int
    #: True when the pass stopped at ``MAX_HANDOFFS_PER_PASS`` with eligible
    #: work remaining. The caller may invoke again; this module never loops.
    truncated: bool
    outcomes: Sequence[DispatchPassOutcome] = field(default_factory=tuple)


# ═════════════════════════════════════════════════════════════════════════════
# Selection — derived, never stored
# ═════════════════════════════════════════════════════════════════════════════

def _has_frozen_snapshot(notification: EcoNotification) -> bool:
    """A usable frozen snapshot naming this recipient.

    Not merely "the column is non-empty": the snapshot must actually name the
    recipient identity, because that identity is what the handoff carries and
    D-3.7 makes the snapshot — not today's designations — authoritative.
    """
    snapshot = notification.recipient_snapshot or {}
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    recipient = notification.recipient_user_id
    if recipient is None:
        return False
    if snapshot.get("worker_user_id") == recipient:
        return True
    for entry in snapshot.get("audiences") or ():
        if not isinstance(entry, dict):
            continue
        if recipient in [int(u) for u in entry.get("user_ids") or ()
                         if isinstance(u, (int, str)) and str(u).isdigit()]:
            return True
    return False


def _select_eligible(
    db: Session, *, organization_id: Optional[int], limit: int,
) -> List[EcoNotification]:
    """Notifications whose recipient has no completed delivery result.

    One statement, evaluated by the database: a NOT EXISTS against the delivery
    records. Eligibility is therefore a QUESTION asked of persisted facts, not
    a flag somebody remembered to clear — which is what makes repeated passes
    safe and what keeps a second "sent" source of truth from existing.

    ``limit + 1`` is fetched so the caller can tell "exactly at the bound" from
    "more work remains" without a second COUNT.
    """
    completed = (
        select(EcoNotificationDeliveryAttempt.id)
        .where(and_(
            EcoNotificationDeliveryAttempt.notification_id
            == EcoNotification.id,
            EcoNotificationDeliveryAttempt.recipient_user_id
            == EcoNotification.recipient_user_id,
            EcoNotificationDeliveryAttempt.outcome == COMPLETED_OUTCOME,
        ))
    )
    stmt = (
        select(EcoNotification)
        .where(~completed.exists())
        # SHADOW rows are excluded HERE, before anything is handed anywhere.
        # ECO owns the rollout gate; the platform's SHADOW refusal is a
        # backstop, never the thing that keeps a shadow row from dispatching.
        .where(EcoNotification.mode != DispatchMode.SHADOW.value)
        .where(func.coalesce(EcoNotification.recipient_user_id, 0) > 0)
        .order_by(EcoNotification.id)
        .limit(int(limit) + 1)
    )
    if organization_id is not None:
        stmt = stmt.where(
            EcoNotification.organization_id == int(organization_id))
    return list(db.execute(stmt).scalars().all())


# ═════════════════════════════════════════════════════════════════════════════
# Recording
# ═════════════════════════════════════════════════════════════════════════════

def _next_attempt_seq(db: Session, notification_id: int) -> int:
    current = db.execute(
        select(func.max(EcoNotificationDeliveryAttempt.attempt_seq))
        .where(EcoNotificationDeliveryAttempt.notification_id
               == int(notification_id))
    ).scalar()
    return int(current or 0) + 1


def _record_attempt(db: Session, notification: EcoNotification, result) -> bool:
    """Append the platform's result to the ECO delivery record.

    Returns False when nothing was written because the result carried NO
    CHANNEL. That is the locked contract, not an omission: D-5.6 makes the ECO
    record identity ``(threshold_event_id, recipient_id, channel)``, so a
    result that never reached channel selection — ``INVALID_HANDOFF``,
    ``NO_ELIGIBLE_CHANNEL``, ``SHADOW_REFUSED`` — has no identity to be
    recorded under. Fabricating a channel to make one would manufacture a
    delivery attempt for a dispatch that never happened.

    Every value written comes from the platform verbatim. ECO interprets none
    of them, and in particular does not re-derive ``outcome`` from
    ``reason_code`` or vice versa.
    """
    if result.channel is None:
        return False

    attempt = EcoNotificationDeliveryAttempt(
        notification_id=int(notification.id),
        attempt_seq=_next_attempt_seq(db, notification.id),
        recipient_kind=notification.recipient_kind,
        recipient_user_id=int(notification.recipient_user_id),
        channel=result.channel.value,
        # NULL, always. Under the handoff model ECO never learns the address:
        # the platform resolves it below the boundary and returns no such
        # field. A destination here could only be a re-resolution.
        destination=None,
        mode=notification.mode,
        outcome=result.outcome.value,
        reason_code=result.reason_code.value,
        correlation_id=result.correlation_id,
        failure_detail=result.failure_detail,
    )
    db.add(attempt)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # A concurrent pass took this attempt number first. Benign, and the
        # existing model says so: an extra attempt is legitimate, so the loser
        # simply takes the next number. The two rows cannot CONFLICT, because
        # D-4 guarantees both passes received the same platform result.
        db.expunge(attempt)
        _log.info("delivery attempt race on notification %s; retrying seq",
                  notification.id)
        attempt.attempt_seq = _next_attempt_seq(db, notification.id)
        db.add(attempt)
        db.flush()
    return True


# ═════════════════════════════════════════════════════════════════════════════
# The processing step
# ═════════════════════════════════════════════════════════════════════════════

def _build_handoff(notification: EcoNotification) -> NotificationHandoff:
    """Everything from the FROZEN notification. Nothing re-resolved.

    ``content_inputs`` carries the body ALERT rendered and froze at creation,
    never a re-render from current copy and never a channel-specific document.
    No subject is supplied: ECO has no locked subject copy, and authoring one
    here would be authoring §31 copy. The platform owns the channel envelope.
    """
    return NotificationHandoff(
        organization_id=int(notification.organization_id),
        eco_event_id=int(notification.originating_event_id),
        recipient_user_id=int(notification.recipient_user_id),
        audience=str(notification.audience),
        notification_type=str(notification.notification_type),
        content_inputs={
            "body": notification.rendered_body,
            "copy_keys": list(notification.rendered_copy_keys or ()),
        },
        recipient_snapshot=dict(notification.recipient_snapshot or {}),
        mode=DispatchMode.OPERATIONAL,
    )


def process_pending_notification_dispatches(
    db: Session, *, organization_id: Optional[int] = None,
    limit: int = MAX_HANDOFFS_PER_PASS,
) -> DispatchPassResult:
    """One on-demand pass. NOT a loop, and NOT invoked by anything yet.

    ``limit`` may only narrow: it is clamped to ``MAX_HANDOFFS_PER_PASS``, so a
    caller cannot raise the ruled bound by passing a larger number.

    Operates only on COMMITTED work. It never creates or mutates a threshold
    event, so a transport failure cannot roll back, corrupt or partially
    rewrite one — which is the property D-C exists to guarantee, made
    structural rather than left to careful transaction handling.
    """
    bound = min(int(limit), MAX_HANDOFFS_PER_PASS)
    if bound <= 0:
        return DispatchPassResult(handoffs=0, recorded=0, skipped_shadow=0,
                                  truncated=False, outcomes=())

    candidates = _select_eligible(db, organization_id=organization_id,
                                  limit=bound)
    truncated = len(candidates) > bound
    candidates = candidates[:bound]

    outcomes: List[DispatchPassOutcome] = []
    recorded = 0
    skipped_shadow = 0

    for notification in candidates:
        # Defence in depth. The selection already excludes SHADOW rows; this
        # catches one that reached here by another route. It is NOT a second
        # rollout authority — it refuses, it does not decide.
        if str(notification.mode) == DispatchMode.SHADOW.value:
            skipped_shadow += 1
            continue

        if not _has_frozen_snapshot(notification):
            # No frozen snapshot naming this recipient, so there is nothing
            # authoritative to hand off. Skipped, never re-resolved.
            _log.info("notification %s has no frozen snapshot naming its "
                      "recipient; not dispatched", notification.id)
            continue

        result = dispatch(db, _build_handoff(notification))
        written = _record_attempt(db, notification, result)
        recorded += 1 if written else 0
        outcomes.append(DispatchPassOutcome(
            notification_id=int(notification.id),
            eco_event_id=int(notification.originating_event_id),
            recipient_user_id=int(notification.recipient_user_id),
            outcome=result.outcome.value,
            reason_code=result.reason_code.value,
            channel=result.channel.value if result.channel else None,
            correlation_id=result.correlation_id,
            recorded=written,
        ))

    return DispatchPassResult(
        handoffs=len(outcomes), recorded=recorded,
        skipped_shadow=skipped_shadow, truncated=truncated,
        outcomes=tuple(outcomes))


__all__ = [
    "COMPLETED_OUTCOME",
    "MAX_HANDOFFS_PER_PASS",
    "DispatchPassOutcome",
    "DispatchPassResult",
    "process_pending_notification_dispatches",
]
