"""
The day-alert dispatch pass — a SEPARATE pass, not a widened D-C.
=================================================================

    process_pending_day_alert_dispatches(db, organization_id=..., limit=...)

WHY A SEPARATE PASS RATHER THAN WIDENING D-C
---------------------------------------------
This choice was required to be made explicitly rather than silently, so here
is the reasoning.

``dispatch_processing.process_pending_notification_dispatches`` selects
``EcoNotification`` rows. Widening it to also select day alerts would mean one
function iterating two unrelated tables with two different identities, two
different recipient shapes and two different delivery-record targets — and
every future change to either would have to be proved harmless to the other.
Since GV-07 already runs through that function, "proved harmless to the other"
would mean re-proving GV-07 on every day-alert change. Ruling D-C: B asked for
GV-07 to stay isolated; sharing the processor is the opposite of that.

A separate pass keeps both contracts intact:

* **D-C is untouched.** Its selection, its batch bound, its SHADOW exclusion
  and its delivery recording are byte-for-byte what GV-07 was promoted on.
* **D-F is untouched in shape.** The trigger still takes only
  ``organization_id``, still consults the one rollout control, and still runs
  on demand. It now runs two passes per invocation — one per primitive —
  which is the documented adjustment this design requires, and each pass
  keeps its own hard ceiling of 50.

WHAT THIS PASS SHARES WITH D-C, DELIBERATELY
---------------------------------------------
The *shape*, not the code: eligibility derived from persisted facts by a NOT
EXISTS against delivery records, SHADOW excluded before the boundary, a hard
ceiling, no loop, and the platform left to own everything below the handoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.notification_platform import (
    DispatchMode,
    DispatchOutcome,
    NotificationHandoff,
    dispatch,
)

from .models import EcoDayAdministratorAlert, EcoDayAlertDeliveryAttempt
from .outcomes import DayAlertType
from .service import RECIPIENT_RESOLVED

_log = logging.getLogger("zoiko.eco_day_alert.dispatch")

#: The same hard ceiling D-C carries, for the same reason: a pass that found
#: more work stops here and leaves the rest for the next explicit invocation.
MAX_HANDOFFS_PER_PASS = 50

#: The outcome that makes an alert ineligible for further handoff. ``failed``
#: does not exclude — the platform decides what a repeat means (D-4), and ECO
#: implements no retry policy of its own.
COMPLETED_OUTCOME = DispatchOutcome.DISPATCHED.value


@dataclass(frozen=True)
class DayAlertDispatchOutcome:
    alert_id: int
    organization_id: int
    recipient_user_id: int
    outcome: str
    reason_code: str
    channel: Optional[str]
    correlation_id: str
    recorded: bool


@dataclass(frozen=True)
class DayAlertDispatchResult:
    handoffs: int
    recorded: int
    skipped_unresolved_recipient: int
    truncated: bool
    outcomes: Sequence[DayAlertDispatchOutcome] = field(default_factory=tuple)


def _select_eligible(db: Session, *, organization_id: Optional[int],
                     limit: int) -> List[EcoDayAdministratorAlert]:
    """Alerts with a resolved recipient and no completed dispatch.

    Eligibility is a QUESTION asked of persisted facts, never a stored flag —
    so repeated passes are safe and no second source of truth about what has
    been sent can exist.
    """
    completed = (
        select(EcoDayAlertDeliveryAttempt.id)
        .where(and_(
            EcoDayAlertDeliveryAttempt.alert_id == EcoDayAdministratorAlert.id,
            EcoDayAlertDeliveryAttempt.outcome == COMPLETED_OUTCOME,
        ))
    )
    stmt = (
        select(EcoDayAdministratorAlert)
        .where(~completed.exists())
        # SHADOW alerts are excluded HERE, before anything is handed anywhere.
        # ECO owns the rollout gate; the platform's SHADOW refusal is a
        # backstop, never what keeps a shadow row from dispatching.
        .where(EcoDayAdministratorAlert.mode != DispatchMode.SHADOW.value)
        .order_by(EcoDayAdministratorAlert.id)
        .limit(int(limit) + 1)
    )
    if organization_id is not None:
        stmt = stmt.where(
            EcoDayAdministratorAlert.organization_id == int(organization_id))
    return list(db.execute(stmt).scalars().all())


def _next_attempt_seq(db: Session, alert_id: int) -> int:
    current = db.execute(
        select(func.max(EcoDayAlertDeliveryAttempt.attempt_seq))
        .where(EcoDayAlertDeliveryAttempt.alert_id == int(alert_id))
    ).scalar()
    return int(current or 0) + 1


def _record_attempt(db: Session, alert: EcoDayAdministratorAlert,
                    result) -> bool:
    """Append the platform's result. Returns False when no channel was used.

    A result that never reached channel selection describes no delivery, so
    recording one would manufacture an attempt that did not happen.
    """
    if result.channel is None:
        return False

    attempt = EcoDayAlertDeliveryAttempt(
        alert_id=int(alert.id),
        attempt_seq=_next_attempt_seq(db, alert.id),
        recipient_user_id=int(alert.recipient_user_id),
        channel=result.channel.value,
        mode=alert.mode,
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
        # A concurrent pass took this number first. Benign: an extra attempt is
        # legitimate, and D-4 guarantees both passes received the same result.
        db.expunge(attempt)
        attempt.attempt_seq = _next_attempt_seq(db, alert.id)
        db.add(attempt)
        db.flush()
    return True


def _build_handoff(alert: EcoDayAdministratorAlert) -> NotificationHandoff:
    """Everything from the FROZEN alert. Nothing re-resolved.

    ``eco_event_id`` carries the ALERT's id, not a threshold event's — there is
    no threshold event, and fabricating one is exactly what ruling D-C: B
    forbids. The platform treats the value as opaque, so D-4's request key
    ``(eco_event_id, recipient_user_id)`` is well-formed and unique per alert.
    """
    return NotificationHandoff(
        organization_id=int(alert.organization_id),
        eco_event_id=int(alert.id),
        recipient_user_id=int(alert.recipient_user_id),
        audience=str(alert.audience),
        notification_type=DayAlertType.REQUIREMENT_UNRESOLVED.value,
        content_inputs={"body": alert.rendered_body,
                        "copy_id": alert.rendered_copy_id},
        recipient_snapshot=dict(alert.recipient_snapshot or {}),
        mode=DispatchMode.OPERATIONAL,
    )


def process_pending_day_alert_dispatches(
    db: Session, *, organization_id: Optional[int] = None,
    limit: int = MAX_HANDOFFS_PER_PASS,
) -> DayAlertDispatchResult:
    """One on-demand pass over day alerts. NOT a loop.

    Operates only on committed alerts and never creates or mutates one, so a
    transport failure cannot corrupt the record of what a Tenant was told.
    """
    bound = min(int(limit), MAX_HANDOFFS_PER_PASS)
    if bound <= 0:
        return DayAlertDispatchResult(0, 0, 0, False, ())

    candidates = _select_eligible(db, organization_id=organization_id,
                                  limit=bound)
    truncated = len(candidates) > bound
    candidates = candidates[:bound]

    outcomes: List[DayAlertDispatchOutcome] = []
    recorded = 0
    skipped_unresolved = 0

    for alert in candidates:
        if alert.recipient_outcome != RECIPIENT_RESOLVED \
                or alert.recipient_user_id is None:
            # D-1: no PRIMARY ADMIN was designated. The alert stands as the
            # record of that; there is simply nobody to hand it to, and no
            # substitute is ever chosen.
            skipped_unresolved += 1
            continue

        result = dispatch(db, _build_handoff(alert))
        written = _record_attempt(db, alert, result)
        recorded += 1 if written else 0
        outcomes.append(DayAlertDispatchOutcome(
            alert_id=int(alert.id),
            organization_id=int(alert.organization_id),
            recipient_user_id=int(alert.recipient_user_id),
            outcome=result.outcome.value,
            reason_code=result.reason_code.value,
            channel=result.channel.value if result.channel else None,
            correlation_id=result.correlation_id,
            recorded=written))

    return DayAlertDispatchResult(
        handoffs=len(outcomes), recorded=recorded,
        skipped_unresolved_recipient=skipped_unresolved,
        truncated=truncated, outcomes=tuple(outcomes))


__all__ = [
    "COMPLETED_OUTCOME",
    "MAX_HANDOFFS_PER_PASS",
    "DayAlertDispatchOutcome",
    "DayAlertDispatchResult",
    "process_pending_day_alert_dispatches",
]
