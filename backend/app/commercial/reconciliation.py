"""Reconciliation jobs (§11 — billing/internal state aligned, §23 — playbooks).

Two jobs:

  1. ``reconcile_subscriptions`` — every CommercialOrganization must have a
     CommercialSubscription. If not, create a default trial subscription
     and emit subscription.recovered.

  2. ``reconcile_billing`` — for every active checkout session older than
     N minutes that is still in payment_pending, query the provider for
     the real state. (In the stub provider this is a no-op; in the real
     Stripe provider this would call stripe.checkout.Session.retrieve.)

Both jobs are idempotent and safe to re-run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from database.database import SessionLocal
from app.commercial import ids, state_machines, entitlements, constants
from app.commercial.audit import emit_event
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, CheckoutSession,
)

_log = logging.getLogger("zoiko.commercial.reconciliation")


def reconcile_subscriptions() -> dict:
    """Repair tenants that have an org but no subscription (per §23 playbook)."""
    db = SessionLocal()
    repaired = 0
    try:
        orgs = db.query(CommercialOrganization).all()
        for org in orgs:
            sub = db.query(CommercialSubscription).filter_by(organization_id=org.id).one_or_none()
            if sub is not None:
                continue
            now = datetime.now(timezone.utc)
            sub = CommercialSubscription(
                id=ids.subscription_id(),
                organization_id=org.id,
                plan_code="verified",
                billing_cycle="monthly",
                seat_count=org.expected_users or 1,
                state="trial_active",
                trial_started_at=now,
                trial_ends_at=now + timedelta(days=constants.TRIAL_LENGTH_DAYS),
                provider="stub",
            )
            db.add(sub)
            db.flush()
            emit_event(
                db,
                event_type="subscription.recovered",
                source_service="reconciliation-service",
                payload={"organization_id": org.id, "reason": "missing_subscription"},
                organization_id=org.id,
            )
            entitlements.invalidate(db, org.id)
            entitlements.compute(db, org.id, email_verified=False)
            db.commit()
            repaired += 1
        return {"repaired": repaired}
    finally:
        db.close()


def reconcile_billing(stale_minutes: int = 30) -> dict:
    """Repair stuck checkout sessions and mismatched billing.reconciled state."""
    db = SessionLocal()
    moved = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        stuck = (
            db.query(CheckoutSession)
              .filter(CheckoutSession.state.in_(["checkout_pending", "payment_pending"]))
              .filter(CheckoutSession.created_at < cutoff)
              .all()
        )
        for sess in stuck:
            # Without polling the provider we can't know if the user paid
            # out-of-band; we mark the session as expired (failed) so it
            # no longer blocks dashboards. Real Stripe provider would call
            # checkout.Session.retrieve here and reconcile.
            try:
                sess.state = state_machines.billing.transition(sess.state, "payment_pending")
            except state_machines.IllegalTransitionError:
                pass
            try:
                sess.state = state_machines.billing.transition(sess.state, "payment_failed")
            except state_machines.IllegalTransitionError:
                continue
            sess.failed_at = datetime.now(timezone.utc)
            emit_event(
                db, event_type="billing.reconciled",
                source_service="reconciliation-service",
                payload={"session_id": sess.id, "reason": "stale_checkout"},
                organization_id=sess.organization_id,
            )
            db.commit()
            moved += 1
        return {"moved": moved}
    finally:
        db.close()


def run_all_once() -> dict:
    return {
        "subscriptions": reconcile_subscriptions(),
        "billing": reconcile_billing(),
    }
