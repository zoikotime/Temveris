"""Trial lifecycle scheduler (§6.1).

Runs once per hour and:

  - Emits the right lifecycle email for each tenant on Day 0/1/7/14/18/25/30/32/35
    (idempotent — each (org, day_marker) pair is sent at most once)
  - Transitions trial subscriptions through trial_active → trial_ending
    (Day 14) → trial_expired (Day 30) → trial_closed (Day 35)
  - Marks subscriptions for closure if no payment by Day 35
  - Writes every transition + email to commercial_events

The scheduler is started inside main.py's lifespan handler and runs on
the same event loop as the rest of the backend. Each tick opens a fresh
DB session, processes due tenants, and commits per-tenant so a single
bad row never blocks the rest.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import User
from app.commercial import constants, state_machines, entitlements
from app.commercial.audit import emit_event
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, TrialEmailSent,
)
from app.commercial.notifications import send_trial_email

_log = logging.getLogger("zoiko.commercial.trial_lifecycle")


TRIAL_DAY_MARKERS = ["day_0", "day_1", "day_7", "day_14", "day_18",
                     "day_25", "day_30", "day_32", "day_35"]
_DAY_NUMBERS = {f"day_{n}": n for n in (0, 1, 7, 14, 18, 25, 30, 32, 35)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(t: Optional[datetime]) -> Optional[int]:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int((_now() - t).total_seconds() // 86400)


def _due_markers_for_age(age_days: int) -> List[str]:
    """Return the markers that are now due for a trial of this age."""
    return [m for m, d in _DAY_NUMBERS.items() if d <= age_days]


def _already_sent(db: Session, organization_id: str) -> set:
    rows = (
        db.query(TrialEmailSent.day_marker)
          .filter(TrialEmailSent.organization_id == organization_id)
          .all()
    )
    return {r[0] for r in rows}


# ──────────────────────────────────────────────────────────────────────────
# PER-TENANT TICK
# ──────────────────────────────────────────────────────────────────────────
def _tick_one(db: Session, sub: CommercialSubscription, org: CommercialOrganization) -> None:
    if sub.state not in {"trial_active", "trial_ending", "trial_expired"}:
        return
    if not sub.trial_started_at:
        return

    age = _days_since(sub.trial_started_at) or 0
    sent = _already_sent(db, sub.organization_id)
    due = [m for m in _due_markers_for_age(age) if m not in sent]

    # Find owner email — first admin user for this org.
    owner = db.query(User).filter_by(id=org.owner_user_id).one_or_none() if org.owner_user_id else None
    if owner is None:
        _log.warning("No owner user for org %s — skipping trial emails", org.id)
        return
    first_name = (owner.name or owner.email).split()[0]

    for marker in due:
        ok = send_trial_email(
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

    # State transitions:
    if age >= 14 and sub.state == "trial_active":
        sub.state = state_machines.subscription.transition("trial_active", "trial_ending")
        emit_event(db, event_type="subscription.activated",
                   source_service="subscription-service",
                   payload={"transition": "trial_active->trial_ending"},
                   organization_id=org.id, user_id=owner.id,
                   state_from="trial_active", state_to="trial_ending")
    if age >= constants.TRIAL_LENGTH_DAYS and sub.state in {"trial_active", "trial_ending"}:
        prev = sub.state
        sub.state = state_machines.subscription.transition(sub.state, "trial_expired")
        sub.trial_expired_at = _now()
        org.commercial_state = "trial_expired"
        entitlements.invalidate(db, org.id)
        # §16 mandatory event — emitted exactly once per transition.
        emit_event(db, event_type="trial.expired",
                   source_service="subscription-service",
                   payload={"transition": f"{prev}->trial_expired",
                            "trial_age_days": age,
                            "trial_length_days": constants.TRIAL_LENGTH_DAYS},
                   organization_id=org.id, user_id=owner.id,
                   state_from=prev, state_to="trial_expired")
    if age >= 35 and sub.state == "trial_expired":
        sub.state = state_machines.subscription.transition("trial_expired", "trial_closed")
        org.commercial_state = "trial_closed"
        entitlements.invalidate(db, org.id)
        emit_event(db, event_type="trial.closed",
                   source_service="subscription-service",
                   payload={"transition": "trial_expired->trial_closed",
                            "trial_age_days": age,
                            "retention_window_days": 35 - constants.TRIAL_LENGTH_DAYS},
                   organization_id=org.id, user_id=owner.id,
                   state_from="trial_expired", state_to="trial_closed")
        # Mark every workspace under this org as closed too. We just emit
        # an event — the actual workspace state column stays so support
        # can reopen during a recovery window.
        emit_event(db, event_type="workspace.closed",
                   source_service="subscription-service",
                   payload={"reason": "trial_closed_after_retention",
                            "trial_age_days": age},
                   organization_id=org.id, user_id=owner.id)

    db.flush()


def run_once_sync() -> int:
    """Process all trial tenants once. Returns the number of subs touched."""
    touched = 0
    db = SessionLocal()
    try:
        subs = (
            db.query(CommercialSubscription)
              .filter(CommercialSubscription.state.in_(["trial_active", "trial_ending", "trial_expired"]))
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
                _log.exception("Trial tick failed for org %s", sub.organization_id)
                db.rollback()
    finally:
        db.close()
    return touched


async def run_forever(interval_seconds: int = 3600) -> None:
    """Async loop intended to be started in the FastAPI lifespan."""
    _log.info("Trial lifecycle scheduler started (interval=%ss)", interval_seconds)
    while True:
        try:
            count = await asyncio.to_thread(run_once_sync)
            if count:
                _log.info("Trial tick processed %s subscriptions", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Trial tick error")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
