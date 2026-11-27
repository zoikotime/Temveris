"""
The ALERT entry point: record one immutable ECO notification.
=============================================================

    record_threshold_notification(db, event_id=...)

Takes an ``eco_threshold_event.id`` — a SOURCE FACT that already exists — and
appends at most one immutable notification row per authorized audience.

ALERT NEVER CREATES A THRESHOLD EVENT
-------------------------------------
The entry point cannot: it accepts an id, not a week. EVAL-EVENT remains the
sole authority for the threshold lifecycle, and an AST guard in the test suite
pins that this module imports no evaluator and calls nothing that could append
one.

WHAT THIS MODULE DOES NOT DO
----------------------------
No email, no SMTP/Resend, no websocket, no push, no outbox, no retry
scheduler, no backoff, no retry limits, no dead-letter queue, no inbox or UI,
no administrator recipient resolution HERE, no reviewer resolution. The
delivery-attempt table exists (R5) and is deliberately INERT: nothing here
writes to it.

MODEL-HORIZON shipped under Stage 4 and RECIPIENT-AUTHORITY under Stage 5;
neither is consulted by this module, which still records WORKER rows only.

WORKER ONLY
-----------
``AUTHORIZED_AUDIENCES`` holds WORKER and nothing else. An administrator row is
not withheld by the schema — the ``audience`` column is open so a future
audience needs no migration — but by the absence of an authoritative recipient
contract (R2). Fail closed: no admin, HR, owner or notification-list address is
ever substituted for the designated reviewer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.eval_event import EcoThresholdEvent, EventType
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState, rollout_state

from .models import EcoNotification
from .outcomes import (
    AUTHORIZED_AUDIENCES,
    NotificationAudience,
    NotificationOutcome,
    NotificationResult,
    NotificationType,
    RecipientIdentity,
    RecipientKind,
    RefusalReason,
)
from .rendering import render_body

#: The actor recorded on rows this module writes. Follows the established
#: durable-attribution convention — an id where one exists, a system label
#: otherwise (``ConfigVersion.captured_by_label``, ``EVAL_EVENT_ACTOR``).
ALERT_ACTOR = "ALERT"

#: EVAL-EVENT event type -> the notification purpose it justifies.
#: A deliberate, explicit mapping rather than a reuse of ``event_type``: R3
#: forbids substituting one vocabulary for the other on the strength of
#: today's one-to-one coincidence. A test pins this table.
NOTIFICATION_TYPE_FOR_EVENT = {
    EventType.ECO_THRESHOLD_REACHED.value: NotificationType.THRESHOLD_REACHED,
    EventType.ECO_THRESHOLD_WITHDRAWN.value:
        NotificationType.THRESHOLD_WITHDRAWN,
}


class NotificationContentConflict(Exception):
    """One identity, two materially different immutable notifications.

    Raised rather than returned. An ALREADY_RECORDED result means "the same
    thing is already recorded"; if the content differs, returning the stored
    row would silently discard what this run computed, and overwriting it
    would break immutability. Neither is acceptable, so the caller is told.
    """


def _content_hash(
    *, organization_id: int, user_id: int, workweek_key: str, tier: str,
    mode: str, sequence: int, notification_type: str, audience: str,
    originating_event_id: int, recipient_snapshot: dict, rendered_body: str,
    eligibility_outcome: str,
) -> str:
    """SHA-256 over the immutable content. Order-stable, so two runs agree."""
    payload = json.dumps(
        {
            "organization_id": organization_id,
            "user_id": user_id,
            "workweek_key": workweek_key,
            "tier": tier,
            "mode": mode,
            "sequence": sequence,
            "notification_type": notification_type,
            "audience": audience,
            "originating_event_id": originating_event_id,
            "recipient_snapshot": recipient_snapshot,
            "rendered_body": rendered_body,
            "eligibility_outcome": eligibility_outcome,
        },
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _existing(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str,
    tier: str, mode: str, sequence: int, notification_type: str,
    audience: str,
) -> Optional[EcoNotification]:
    """The row at this eight-column identity, if one is already recorded.

    Carries the full identity rather than matching on a shortcut: every other
    read in the ECO packages does, and a boundary resting on one column has
    one way to fail.
    """
    return db.execute(
        select(EcoNotification).where(and_(
            EcoNotification.organization_id == organization_id,
            EcoNotification.user_id == user_id,
            EcoNotification.workweek_key == workweek_key,
            EcoNotification.tier == tier,
            EcoNotification.mode == mode,
            EcoNotification.sequence == sequence,
            EcoNotification.notification_type == notification_type,
            EcoNotification.audience == audience,
        ))
    ).scalars().first()


def _resolve_worker(event: EcoThresholdEvent) -> Optional[RecipientIdentity]:
    """The Worker identity, from the event's FROZEN snapshot.

    Never re-resolved from current data: the recipient a notification concerns
    is the one the event named, whatever has changed since. Returns None rather
    than guessing, so the caller fails closed.
    """
    snapshot = event.recipient_snapshot or {}
    worker_id = snapshot.get("worker_user_id")
    if worker_id is None:
        # Do NOT fall back to ``event.user_id``: the snapshot is the
        # authoritative record of who was named, and a silent fallback would
        # manufacture a recipient the event never froze.
        return None
    try:
        return RecipientIdentity(kind=RecipientKind.WORKER,
                                 user_id=int(worker_id))
    except (TypeError, ValueError):
        return None


def _refused(reason: RefusalReason, detail: str, *, event: EcoThresholdEvent
             ) -> NotificationResult:
    return NotificationResult(
        outcome=NotificationOutcome.REFUSED,
        organization_id=event.organization_id, user_id=event.user_id,
        workweek_key=event.workweek_key, tier=event.tier, mode=event.mode,
        sequence=event.sequence, refusal=reason, detail=detail,
    )


def record_threshold_notification(
    db: Session,
    *,
    event_id: int,
    audience: NotificationAudience = NotificationAudience.WORKER,
    created_by: str = ALERT_ACTOR,
) -> NotificationResult:
    """Record the notification for one ECO threshold event and one audience.

    Returns, never raises, for every ordinary outcome. Two exceptions are
    deliberate: ``NotificationContentConflict`` (one identity, two different
    immutable bodies) and ``ProhibitedTerminology`` (a §31.1 failure) — both
    are defects a caller must not be able to branch past.

    The caller owns the transaction: this flushes and never commits.
    """
    event = db.get(EcoThresholdEvent, int(event_id))
    if event is None:
        return NotificationResult(
            outcome=NotificationOutcome.REFUSED, organization_id=0, user_id=0,
            workweek_key="", tier="", mode="", sequence=1,
            refusal=RefusalReason.EVENT_NOT_FOUND,
            detail=f"no eco_threshold_event with id={event_id}")

    # Coerced defensively rather than through ``NotificationAudience(...)``:
    # a caller naming an audience the enum does not know must be REFUSED with
    # a typed outcome, not met with a ValueError. Otherwise the
    # AUTHORIZED_AUDIENCES gate below would be unreachable decoration, because
    # the enum would have rejected everything first — and the gate is the
    # thing that keeps an administrator row from being created.
    audience_value = getattr(audience, "value", None) or str(audience)

    # ── 1. Is this audience authorized at all? ──────────────────────────────
    if audience_value not in AUTHORIZED_AUDIENCES:
        return _refused(
            RefusalReason.AUDIENCE_NOT_AUTHORIZED,
            f"audience {audience_value} has no authoritative recipient "
            f"contract; only {sorted(AUTHORIZED_AUDIENCES)} is authorized "
            f"(owner ruling R2)",
            event=event)

    # ── 2. Does this event type justify a notification? ─────────────────────
    notification_type = NOTIFICATION_TYPE_FOR_EVENT.get(event.event_type)
    if notification_type is None:
        return _refused(
            RefusalReason.EVENT_TYPE_NOT_NOTIFIABLE,
            f"event type {event.event_type!r} has no notification purpose",
            event=event)

    # ── 3. Rollout (D8). The first consumer of the durable control. ─────────
    state = rollout_state(
        db, organization_id=event.organization_id,
        control_key=CONTROL_EARLY_CLOCK_OUT_V2)

    if state is RolloutState.OFF:
        # OFF is also the answer to every uncertainty: a missing, malformed or
        # unreadable control row resolves to OFF inside ``rollout_state``.
        return _refused(
            RefusalReason.ROLLOUT_OFF,
            "rollout is OFF for this organization: no operational ECO "
            "notification processing (D8)",
            event=event)

    if state is RolloutState.SHADOW and event.mode != "SHADOW":
        # SHADOW must have no operational effect. Recording an OPERATIONAL
        # notification under a SHADOW rollout would be exactly that.
        return _refused(
            RefusalReason.OPERATIONAL_UNDER_SHADOW_ROLLOUT,
            f"rollout is SHADOW but the event mode is {event.mode!r}; a "
            f"SHADOW rollout may have no operational effect (D8)",
            event=event)

    # ── 4. Recipient, from the frozen snapshot. Fail closed. ────────────────
    recipient = _resolve_worker(event)
    if recipient is None:
        return _refused(
            RefusalReason.RECIPIENT_UNRESOLVED,
            "the event's frozen recipient snapshot names no usable Worker "
            "identity; no substitute recipient is ever chosen",
            event=event)

    # ── 5. Render, and gate the FINAL body (R7). ────────────────────────────
    rendered_body, copy_keys = render_body(
        audience=NotificationAudience(audience_value),
        notification_type=notification_type,
        workweek_key=event.workweek_key,
        weekly_total_minutes=int(event.weekly_total_minutes),
        threshold_minutes=int(event.threshold_minutes),
        is_retroactive=bool(event.is_retroactive),
        caused_by=dict(event.caused_by or {}),
    )

    recipient_snapshot = dict(event.recipient_snapshot or {})
    eligibility_outcome = "ELIGIBLE_TO_RECORD"
    identity = dict(
        organization_id=int(event.organization_id),
        user_id=int(event.user_id),
        workweek_key=event.workweek_key,
        tier=event.tier,
        mode=event.mode,
        sequence=int(event.sequence),
        notification_type=notification_type.value,
        audience=audience_value,
    )
    digest = _content_hash(
        **identity,
        originating_event_id=int(event.id),
        recipient_snapshot=recipient_snapshot,
        rendered_body=rendered_body,
        eligibility_outcome=eligibility_outcome,
    )

    def _already(row: EcoNotification) -> NotificationResult:
        if row.content_hash != digest:
            raise NotificationContentConflict(
                f"ECO notification {identity} is already recorded with "
                f"different immutable content (stored {row.content_hash[:12]}…"
                f", computed {digest[:12]}…). The record is immutable (R5), so "
                f"neither returning nor overwriting it would be truthful."
            )
        return NotificationResult(
            outcome=NotificationOutcome.ALREADY_RECORDED,
            organization_id=row.organization_id, user_id=row.user_id,
            workweek_key=row.workweek_key, tier=row.tier, mode=row.mode,
            sequence=row.sequence,
            notification_type=notification_type,
            audience=NotificationAudience(audience_value),
            notification_id=int(row.id),
            originating_event_id=int(row.originating_event_id))

    # ── 6. Append. The database is the authority on identity. ───────────────
    found = _existing(db, **identity)
    if found is not None:
        return _already(found)

    row = EcoNotification(
        **identity,
        originating_event_id=int(event.id),
        originating_event_type=event.event_type,
        event_provenance={
            "week_provenance": dict(event.week_provenance or {}),
            "caused_by": dict(event.caused_by or {}),
            "weekly_total_minutes": int(event.weekly_total_minutes),
            "threshold_minutes": int(event.threshold_minutes),
            "threshold_config_version": event.threshold_config_version,
            "is_retroactive": bool(event.is_retroactive),
        },
        recipient_kind=recipient.kind.value,
        recipient_user_id=recipient.user_id,
        recipient_snapshot=recipient_snapshot,
        rendered_body=rendered_body,
        rendered_copy_keys=list(copy_keys),
        eligibility_outcome=eligibility_outcome,
        eligibility_reason=None,
        rollout_state=state.value,
        content_hash=digest,
        created_by=created_by,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # A concurrent run won the race on the eight-column identity. The
        # database is the authority: roll back this savepoint alone, re-read,
        # and report the winner. An IntegrityError that is NOT this conflict is
        # re-raised rather than swallowed.
        winner = _existing(db, **identity)
        if winner is None:
            raise
        return _already(winner)

    return NotificationResult(
        outcome=NotificationOutcome.RECORDED,
        organization_id=row.organization_id, user_id=row.user_id,
        workweek_key=row.workweek_key, tier=row.tier, mode=row.mode,
        sequence=row.sequence,
        notification_type=notification_type,
        audience=NotificationAudience(audience_value),
        notification_id=int(row.id),
        originating_event_id=int(event.id))


def read_notifications(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str,
    mode: str,
) -> Tuple[EcoNotification, ...]:
    """The notification history for one Worker-week, in ONE mode.

    Named explicitly by mode so a caller cannot accidentally read SHADOW rows
    into an operational surface (D8).
    """
    return tuple(db.execute(
        select(EcoNotification).where(and_(
            EcoNotification.organization_id == organization_id,
            EcoNotification.user_id == user_id,
            EcoNotification.workweek_key == workweek_key,
            EcoNotification.mode == mode,
        )).order_by(EcoNotification.id)
    ).scalars().all())


__all__ = [
    "record_threshold_notification",
    "read_notifications",
    "NotificationContentConflict",
    "NOTIFICATION_TYPE_FOR_EVENT",
    "ALERT_ACTOR",
]
