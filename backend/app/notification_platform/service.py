"""
dispatch() — the platform boundary ECO hands off to.
====================================================

ZT-SPEC-ECO-002 §34, owner rulings D-1…D-4, architecture decision D-A.

THE ORDER IS THE CONTRACT (D-2)
-------------------------------
    0. accept the handoff        -> correlation_id minted, key claimed
    1. CHANNEL SELECTION
    2. PREFERENCE / SUPPRESSION
    3. TRANSPORT
    4. DispatchResult

Step 1 precedes step 2, and that is not a stylistic preference. It is what
guarantees a channel exists on a SUPPRESSED result — which is in turn what lets
ECO form its three-part record identity ``(threshold_event_id, recipient_id,
channel)`` for a suppressed notification as well as a transmitted one. Running
suppression first would leave nothing to key the record on.

A SUPPRESSED NOTIFICATION IS ``dispatched`` (D-2)
--------------------------------------------------
Not a third outcome. The handoff completed; the platform's policy decided
against transmission, and the ``reason_code`` says so. A reader asking "was
this recipient actually informed?" must consult ``reason_code`` as well as
``outcome`` — ``DispatchResult.suppressed`` is the supported way to ask.

SHADOW
------
ECO must not call this in SHADOW. The refusal here is DEFENCE IN DEPTH: it
exists so an ECO-side bug fails closed instead of sending, and it is not a
second rollout authority. A SHADOW call touches no store and creates no
idempotency row — it is as though it never happened, which is what makes it a
backstop rather than a parallel code path with its own state.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.notifications import classification as _classes

from . import idempotency
from .channels import notification_class_for, select_channel
from .contracts import (
    Channel,
    DispatchMode,
    DispatchOutcome,
    DispatchResult,
    NotificationHandoff,
    ReasonCode,
)
from .correlation import new_correlation_id

_log = logging.getLogger("zoiko.notification_platform")

#: Suppression reasons ``evaluate_send`` can return, mapped onto the platform's
#: CLOSED vocabulary. A reason this map does not know becomes the generic
#: preference code rather than leaking an internal string — D-5.5 requires an
#: enumerated service reason code, and passing through an unrecognised token
#: would defeat that for the one case nobody anticipated.
_SUPPRESSION_REASONS = {
    "quiet_hours": ReasonCode.SUPPRESSED_QUIET_HOURS,
    "unsubscribed": ReasonCode.SUPPRESSED_UNSUBSCRIBED,
    "email_channel_off": ReasonCode.SUPPRESSED_BY_PREFERENCE,
    "posture_essential": ReasonCode.SUPPRESSED_BY_PREFERENCE,
    "type_disabled": ReasonCode.SUPPRESSED_BY_PREFERENCE,
}


def _result(outcome: DispatchOutcome, channel: Optional[Channel],
            reason: ReasonCode, correlation_id: str,
            detail: Optional[str] = None) -> DispatchResult:
    return DispatchResult(outcome=outcome, channel=channel, reason_code=reason,
                          correlation_id=correlation_id, failure_detail=detail)


def _validate(handoff: NotificationHandoff, db: Session) -> Optional[str]:
    """Structural and tenant checks. Returns a sanitised reason, or None.

    The returned string names only field NAMES and ids — never a recipient
    address, a Worker name, or any content (D-5.5).
    """
    for name in ("organization_id", "eco_event_id", "recipient_user_id"):
        value = getattr(handoff, name, None)
        if value is None or not isinstance(value, int) or value <= 0:
            return f"handoff field {name} is missing or not a positive integer"
    if not str(handoff.audience or "").strip():
        return "handoff field audience is empty"
    if not str(handoff.notification_type or "").strip():
        return "handoff field notification_type is empty"
    if notification_class_for(handoff) is None:
        return f"audience {handoff.audience!r} is not a dispatchable audience"

    # Tenant isolation. The platform must never deliver across a tenant
    # boundary, and no foreign key expresses "this user belongs to this org".
    from database import models as db_models

    row = db.query(db_models.User.organization_id).filter(
        db_models.User.id == int(handoff.recipient_user_id)).first()
    if row is None:
        return f"recipient {handoff.recipient_user_id} does not exist"
    if row[0] is None or int(row[0]) != int(handoff.organization_id):
        return (f"recipient {handoff.recipient_user_id} does not belong to "
                f"organization {handoff.organization_id}")
    return None


def _evaluate_suppression(
    db: Session, *, handoff: NotificationHandoff, notification_class: str,
) -> Tuple[bool, Optional[ReasonCode]]:
    """D-2 step 2. Returns ``(allowed, suppression_reason)``.

    A class outside ``SUPPRESSIBLE`` short-circuits: ECO's administrator class
    (D-B) is not a member, so no preference or quiet-hours evaluation can
    decline it. That is §33.1's "Always", enforced by the vocabulary rather
    than by an exemption branch.
    """
    if notification_class not in _classes.SUPPRESSIBLE:
        return True, None

    from app.notifications import suppression as _suppression
    from app.notifications.router import evaluate_send

    destination = None
    try:
        from .email_adapter import resolve_destination
        destination = resolve_destination(db, handoff.recipient_user_id)
    except Exception:  # pragma: no cover - defensive
        _log.warning("destination lookup failed during suppression evaluation",
                     exc_info=True)

    allowed, reason = evaluate_send(
        to_email=destination or "",
        classification=notification_class,
        db=db,
        recipient_user_id=int(handoff.recipient_user_id),
        family=None,
    )
    if allowed and destination:
        if _suppression.is_suppressed(db, destination, notification_class):
            allowed, reason = False, "unsubscribed"
    if allowed:
        return True, None
    return False, _SUPPRESSION_REASONS.get(
        str(reason), ReasonCode.SUPPRESSED_BY_PREFERENCE)


def _render(handoff: NotificationHandoff) -> Tuple[str, str]:
    """Turn the caller's content inputs into a transportable body.

    The platform renders for the CHANNEL; the caller supplies WHAT to say. ECO
    passes locked §31 copy values in ``content_inputs`` and never a
    channel-specific body, which is why this lives here.
    """
    inputs = dict(handoff.content_inputs or {})
    subject = str(inputs.get("subject") or "Temveris notification")
    body = str(inputs.get("body") or "")
    html = body if body.lstrip().startswith("<") else f"<p>{body}</p>"
    return subject, html


def dispatch(db: Session, handoff: NotificationHandoff) -> DispatchResult:
    """Hand off one notification for one recipient. The platform entry point.

    Never raises for an ordinary delivery problem: §34 makes the record
    authoritative and delivery best-effort, so a transport or platform fault
    becomes a ``failed`` result rather than an exception the caller must catch
    to protect its own record.

    ``idempotency.DispatchInProgress`` and ``idempotency.RequestHashConflict``
    DO propagate: neither is a delivery outcome, and representing them as one
    would be a false statement about what happened.
    """
    # ── SHADOW: defence in depth. Touches nothing. ──────────────────────────
    if handoff.mode is DispatchMode.SHADOW:
        return _result(
            DispatchOutcome.FAILED, None, ReasonCode.SHADOW_REFUSED,
            new_correlation_id(),
            "handoff carried SHADOW mode; ECO must not dispatch in SHADOW")

    # ── validate before claiming a key ──────────────────────────────────────
    invalid = _validate(handoff, db)
    if invalid is not None:
        return _result(DispatchOutcome.FAILED, None,
                       ReasonCode.INVALID_HANDOFF, new_correlation_id(),
                       invalid)

    # ── 0. accept: mint the correlation id, claim the request key (D-3, D-4) ─
    row, replayed = idempotency.begin(db, handoff)
    if replayed is not None:
        return replayed
    correlation_id = row.correlation_id

    try:
        # ── 1. CHANNEL SELECTION ────────────────────────────────────────────
        channel = select_channel(handoff)
        if channel is None:
            return idempotency.complete(db, row, _result(
                DispatchOutcome.FAILED, None, ReasonCode.NO_ELIGIBLE_CHANNEL,
                correlation_id,
                "channel selection produced no eligible channel"))

        notification_class = notification_class_for(handoff)

        # ── 2. PREFERENCE / SUPPRESSION ─────────────────────────────────────
        allowed, suppression = _evaluate_suppression(
            db, handoff=handoff, notification_class=notification_class)
        if not allowed:
            # D-2: DISPATCHED, carrying the suppression reason. The channel is
            # reported because one WAS selected — step 1 ran.
            return idempotency.complete(db, row, _result(
                DispatchOutcome.DISPATCHED, channel, suppression,
                correlation_id))

        # ── 3. TRANSPORT ────────────────────────────────────────────────────
        from .email_adapter import send as email_send

        subject, html = _render(handoff)
        ok, reason, detail = email_send(
            db, user_id=int(handoff.recipient_user_id),
            subject=subject, html=html)

        # ── 4. DispatchResult ───────────────────────────────────────────────
        return idempotency.complete(db, row, _result(
            DispatchOutcome.DISPATCHED if ok else DispatchOutcome.FAILED,
            channel, reason, correlation_id, detail))

    except Exception as exc:
        # A platform fault must not leave the key claimed but never completed:
        # the next attempt would raise DispatchInProgress forever. Close it as
        # failed, recording the exception TYPE only.
        _log.exception("notification dispatch failed")
        try:
            return idempotency.complete(db, row, _result(
                DispatchOutcome.FAILED, None, ReasonCode.PLATFORM_ERROR,
                correlation_id,
                f"platform raised {type(exc).__name__} during dispatch"))
        except Exception:  # pragma: no cover - the store itself is broken
            return _result(
                DispatchOutcome.FAILED, None, ReasonCode.PLATFORM_ERROR,
                correlation_id,
                f"platform raised {type(exc).__name__} during dispatch")


__all__ = ["dispatch"]
