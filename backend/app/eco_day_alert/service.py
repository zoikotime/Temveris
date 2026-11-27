"""
Raising the §24.3 administrator alert from an authoritative day evaluation.
===========================================================================

    raise_day_administrator_alert(db, day_record=..., mode=...)

WHAT THIS MODULE MUST NEVER DO
------------------------------
Create or mutate an ``EcoThresholdEvent`` · call ``app.alert``'s entry point ·
touch any GV-07 code path · notify the Worker, a manager or an HR seat ·
substitute a recipient · select a channel · suppress · transport · retry.

Every one of those is either forbidden by the authorization or owned by a
layer below. An AST guard asserts the imports.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.recipient_authority import (
    AudienceState,
    EcoAudience,
    build_recipient_snapshot,
    resolve_eco_recipients,
)

from .copy import (
    REQUIREMENT_UNRESOLVED_BODY_ID,
    render_requirement_unresolved_body,
)
from .models import EcoDayAdministratorAlert
from .outcomes import (
    DayAlertOutcome,
    DayAlertResult,
    DayAlertType,
    RefusalReason,
)

_log = logging.getLogger("zoiko.eco_day_alert")

#: The one state §24.3 attaches this alert to.
ALERTABLE_STATE = "REQUIREMENT_UNRESOLVED"

RECIPIENT_RESOLVED = "RESOLVED"
RECIPIENT_UNRESOLVED = "RECIPIENT_UNRESOLVED"

ALERT_ACTOR = "ECO_DAY_ALERT"


def _refused(reason: RefusalReason, record, detail: str) -> DayAlertResult:
    return DayAlertResult(
        outcome=DayAlertOutcome.REFUSED,
        organization_id=int(record.organization_id),
        user_id=int(record.user_id), work_date=record.work_date,
        refusal=reason, detail=detail,
    )


def _existing(db: Session, record, *, mode: str
              ) -> Optional[EcoDayAdministratorAlert]:
    return db.execute(
        select(EcoDayAdministratorAlert).where(and_(
            EcoDayAdministratorAlert.organization_id
            == int(record.organization_id),
            EcoDayAdministratorAlert.user_id == int(record.user_id),
            EcoDayAdministratorAlert.work_date == record.work_date,
            EcoDayAdministratorAlert.evaluation_generation
            == int(record.evaluation_generation),
            EcoDayAdministratorAlert.alert_type
            == DayAlertType.REQUIREMENT_UNRESOLVED.value,
            EcoDayAdministratorAlert.audience
            == EcoAudience.PRIMARY_ADMIN.value,
            EcoDayAdministratorAlert.mode == mode,
        ))
    ).scalars().first()


def raise_day_administrator_alert(
    db: Session, *, day_record, mode: str = "OPERATIONAL",
    raised_by: str = ALERT_ACTOR,
) -> DayAlertResult:
    """Raise the §24.3 alert for one authoritative day evaluation.

    Returns, never raises, for every ordinary outcome — including "no PRIMARY
    ADMIN is designated", which is a recorded fact rather than an error.

    Nothing about the day record is written or changed. §24.3's alert is a
    statement ABOUT an evaluation, and an evaluation that could be edited by
    the act of alerting on it would no longer be the thing alerted about.
    """
    # ── 1. §24.3 attaches this alert to exactly one state ───────────────────
    state = getattr(day_record, "state", None)
    if state != ALERTABLE_STATE:
        return _refused(
            RefusalReason.STATE_NOT_ALERTABLE, day_record,
            f"day state is {state!r}; §24.3 raises this alert only for "
            f"{ALERTABLE_STATE}")

    # A record claiming this state while carrying a computed shortfall is
    # contradictory evidence. §24.3 requires shortfall_computed false, so
    # nothing is asserted about it rather than asserting the wrong thing.
    if bool(getattr(day_record, "shortfall_computed", False)):
        return _refused(
            RefusalReason.SHORTFALL_WAS_COMPUTED, day_record,
            "the record is REQUIREMENT_UNRESOLVED but reports a computed "
            "shortfall; §24.3 requires shortfall_computed to be false")

    # A superseded generation is a day the Tenant has already had
    # re-evaluated. Alerting on it would describe a past answer as current.
    if getattr(day_record, "superseded_by", None) is not None:
        return _refused(
            RefusalReason.DAY_RECORD_SUPERSEDED, day_record,
            "this generation has been superseded by a later evaluation")

    # ── 2. idempotency: one authoritative evaluation, one alert ─────────────
    already = _existing(db, day_record, mode=mode)
    if already is not None:
        return DayAlertResult(
            outcome=DayAlertOutcome.ALREADY_RAISED,
            organization_id=int(day_record.organization_id),
            user_id=int(day_record.user_id), work_date=day_record.work_date,
            alert_id=int(already.id),
            recipient_user_id=already.recipient_user_id)

    # ── 3. recipient: PRIMARY ADMIN only, via the existing authority ────────
    # The Stage 5 resolver is reused rather than reimplemented, so D-3.7's
    # frozen-snapshot semantics and the cross-tenant refusal come with it.
    resolved = resolve_eco_recipients(
        db, organization_id=int(day_record.organization_id),
        worker_user_id=int(day_record.user_id))
    primary = resolved.by_audience(EcoAudience.PRIMARY_ADMIN)

    if primary.state is AudienceState.RESOLVED and primary.user_ids:
        # §33.1 names ONE administrator audience. Where a Tenant designates
        # several holders the first in the resolver's deterministic id order
        # is recorded; the snapshot below preserves all of them, so nothing is
        # lost and the choice is inspectable rather than arbitrary.
        recipient_user_id = int(primary.user_ids[0])
        recipient_outcome = RECIPIENT_RESOLVED
    else:
        # D-1: recorded, never substituted. Not the Worker, not a manager, not
        # an HR seat, not an arbitrary administrator. The row is still written
        # so the absence is itself evidence.
        recipient_user_id = None
        recipient_outcome = RECIPIENT_UNRESOLVED

    snapshot = build_recipient_snapshot(resolved)

    alert = EcoDayAdministratorAlert(
        organization_id=int(day_record.organization_id),
        user_id=int(day_record.user_id),
        work_date=day_record.work_date,
        day_record_id=int(day_record.id),
        evaluation_generation=int(day_record.evaluation_generation),
        day_state=ALERTABLE_STATE,
        alert_type=DayAlertType.REQUIREMENT_UNRESOLVED.value,
        audience=EcoAudience.PRIMARY_ADMIN.value,
        mode=mode,
        recipient_user_id=recipient_user_id,
        recipient_outcome=recipient_outcome,
        recipient_snapshot=snapshot,
        rendered_body=render_requirement_unresolved_body(),
        rendered_copy_id=REQUIREMENT_UNRESOLVED_BODY_ID,
        raised_by=raised_by,
    )
    db.add(alert)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # A concurrent run won the uniqueness race. The database is the
        # authority; re-read and report the winner rather than raising a
        # second alert about the same evaluation.
        db.expunge(alert)
        winner = _existing(db, day_record, mode=mode)
        if winner is None:  # pragma: no cover - not our IntegrityError
            raise
        return DayAlertResult(
            outcome=DayAlertOutcome.ALREADY_RAISED,
            organization_id=int(day_record.organization_id),
            user_id=int(day_record.user_id), work_date=day_record.work_date,
            alert_id=int(winner.id),
            recipient_user_id=winner.recipient_user_id)

    return DayAlertResult(
        outcome=DayAlertOutcome.RAISED,
        organization_id=int(day_record.organization_id),
        user_id=int(day_record.user_id), work_date=day_record.work_date,
        alert_id=int(alert.id),
        refusal=(RefusalReason.RECIPIENT_UNRESOLVED
                 if recipient_outcome == RECIPIENT_UNRESOLVED else None),
        recipient_user_id=recipient_user_id)


__all__ = [
    "ALERTABLE_STATE",
    "ALERT_ACTOR",
    "RECIPIENT_RESOLVED",
    "RECIPIENT_UNRESOLVED",
    "raise_day_administrator_alert",
]
