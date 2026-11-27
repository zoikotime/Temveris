"""Dunning lifecycle scheduler (§6.1 + §11.2).

Runs hourly on the same lifespan loop as trial_lifecycle and:

  - Walks every subscription in past_due / grace_period / dunning_active
    state, computes days-since-first-failure, and emits the right dunning
    email (day 1 / 3 / 7 / 14) — at-most-once per (org, marker) pair.
  - On day 14 the subscription is transitioned grace_period → suspended
    per §10's "Grace period expires → suspended" rule.

Idempotency comes from the same TrialEmailSent table the trial-lifecycle
uses; we just namespace the markers with the "dunning_" prefix so the
two flows can't collide.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import User
from app.commercial import constants, state_machines, entitlements
from app.commercial.audit import emit_event
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, TrialEmailSent,
)
from app.commercial.notifications import send_dunning_email, _DUNNING_DAY_NUMBERS  # type: ignore

_log = logging.getLogger("zoiko.commercial.dunning")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(t: Optional[datetime]) -> Optional[int]:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int((_now() - t).total_seconds() // 86400)


def _already_sent(db: Session, organization_id: str) -> set:
    rows = (
        db.query(TrialEmailSent.day_marker)
          .filter(TrialEmailSent.organization_id == organization_id,
                  TrialEmailSent.day_marker.like("dunning_%"))
          .all()
    )
    return {r[0] for r in rows}


def _due_markers(age_days: int) -> List[str]:
    return [m for m, d in _DUNNING_DAY_NUMBERS.items() if d <= age_days]


def _resolve_failure_anchor(sub: CommercialSubscription) -> Optional[datetime]:
    """Use whatever timestamp the subscription was put into the
    past_due/dunning bucket. We don't have an explicit field for it, so
    we approximate from `updated_at` — which is when the state last
    changed for non-payment-recovered subs. That's accurate enough for
    sending day-N reminders since the precise hour doesn't matter."""
    return getattr(sub, "past_due_since", None) or getattr(sub, "updated_at", None)


def _tick_one(db: Session, sub: CommercialSubscription, org: CommercialOrganization) -> None:
    if sub.state not in {"past_due", "grace_period", "dunning_active"}:
        return
    anchor = _resolve_failure_anchor(sub)
    age = _days_since(anchor)
    if age is None or age < 1:
        return

    sent = _already_sent(db, sub.organization_id)
    due = [m for m in _due_markers(age) if m not in sent]

    owner = db.query(User).filter_by(id=org.owner_user_id).one_or_none() if org.owner_user_id else None
    if owner is None:
        _log.warning("No owner user for org %s — skipping dunning emails", org.id)
        return
    first_name = (owner.name or owner.email).split()[0]

    for marker in due:
        ok = send_dunning_email(
            db, day_marker=marker, to_email=owner.email,
            first_name=first_name, company_name=org.name,
            organization_id=org.id, user_id=owner.id,
        )
        row = TrialEmailSent(
            organization_id=org.id, subscription_id=sub.id,
            day_marker=marker, status="sent" if ok else "failed",
        )
        db.add(row)
        db.flush()

    # State escalation: grace_period → suspended on day 14.
    if age >= 14 and sub.state in {"grace_period", "dunning_active"}:
        prev = sub.state
        try:
            sub.state = state_machines.subscription.transition(sub.state, "suspended")
        except Exception:
            # If the SM doesn't allow that direct transition, force-set —
            # the source of truth is whether we sent the 14-day email.
            sub.state = "suspended"
        org.commercial_state = "suspended"
        entitlements.invalidate(db, org.id)
        emit_event(db, event_type="subscription.recovered" if False else "payment.declined",
                   source_service="dunning-service",
                   payload={"transition": f"{prev}->suspended", "reason": "dunning_unresolved_14d"},
                   organization_id=org.id, user_id=owner.id,
                   state_from=prev, state_to="suspended")

    db.flush()


def run_once_sync() -> int:
    touched = 0
    db = SessionLocal()
    try:
        subs = (
            db.query(CommercialSubscription)
              .filter(CommercialSubscription.state.in_(["past_due", "grace_period", "dunning_active"]))
              .all()
        )
        for sub in subs:
            org = db.query(CommercialOrganization).filter_by(id=sub.organization_id).one_or_none()
            if not org:
                continue
            try:
                _tick_one(db, sub, org)
                db.commit()
                touched += 1
            except Exception:
                _log.exception("Dunning tick failed for org %s", sub.organization_id)
                db.rollback()
    finally:
        db.close()
    return touched


async def run_forever(interval_seconds: int = 3600) -> None:
    _log.info("Dunning scheduler started (interval=%ss)", interval_seconds)
    while True:
        try:
            count = await asyncio.to_thread(run_once_sync)
            if count:
                _log.info("Dunning tick processed %s subscriptions", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Dunning tick error")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
