"""Subscription reconciliation — Stripe is the source of truth.

Phase 2.2 reconciled exactly one dimension: seat quantity. Everything else could
drift silently and permanently — a missed webhook, an out-of-order delivery, or
the ``BILLING DRIFT`` path in ``service.change_stripe_plan`` (Stripe moved, the
local commit failed) all leave local state wrong with nothing to correct it.

This sweep closes that gap for:

===================  =========================================================
status               corrected from Stripe
billing cycle        corrected from the price's recurring interval
plan / price         corrected via the STRIPE_PRICE_* catalog
renewal date         corrected from current_period_end
cancel_at_period_end corrected onto cancel_pending / cancellation_requested_at
quantity             delegated to seats.sync_org_seats — not reimplemented
customer id          **reported only**, never rewritten
subscription id      **reported only**, never rewritten
===================  =========================================================

The identity fields are deliberately report-only. A customer- or subscription-id
mismatch means the local row points at another tenant's Stripe object, or a second
subscription is billing in parallel. Silently "correcting" either one could move a
tenant onto someone else's subscription or orphan a live one that keeps charging;
which object is authoritative is a business decision, so it is surfaced, not
guessed.

The state-mirroring itself reuses ``webhooks.apply_subscription_state``, so
reconciliation and webhook processing can never disagree about what Stripe state
means locally.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.billing import seats as billing_seats
from app.billing import service as billing_service
from app.billing import webhooks as billing_webhooks
from database import models

_log = logging.getLogger("zoiko.billing.reconcile")

DEFAULT_INTERVAL_SECONDS = 3600


def reconcile_subscription(db: Session, org_id: int, *, stripe_client=None,
                           apply: bool = True) -> Dict[str, Any]:
    """Compare one organization's local row against Stripe.

    Returns a report dict; never raises. ``apply=False`` detects divergence
    without writing, for read-only auditing.
    """
    if stripe_client is None:                     # pragma: no cover - runtime path
        import stripe as stripe_client            # noqa: PLC0415

    report: Dict[str, Any] = {"organization_id": org_id, "divergences": {},
                              "anomalies": [], "status": "in_sync"}

    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub is None:
        report["status"] = "no_subscription"
        return report
    # Checked before the Stripe id test: an internal tenant that still carries ids
    # from before it was exempted must not be reconciled against Stripe.
    if billing_service.is_internal_tenant(db, org_id):
        report["status"] = "internal_tenant"
        return report
    if not sub.stripe_subscription_id or not sub.stripe_customer_id:
        report["status"] = "no_stripe_subscription"
        return report

    try:
        remote = stripe_client.Subscription.retrieve(sub.stripe_subscription_id)
    except Exception as exc:
        _log.error("reconcile: Stripe retrieve failed org=%s subscription=%s: %s",
                   org_id, sub.stripe_subscription_id, exc)
        report["status"] = "stripe_unavailable"
        report["error"] = str(exc)[:200]
        return report

    # ── Identity checks: report, never rewrite ───────────────────────────────
    remote_customer = billing_webhooks._g(remote, "customer")
    if remote_customer and remote_customer != sub.stripe_customer_id:
        report["anomalies"].append({
            "field": "stripe_customer_id",
            "local": sub.stripe_customer_id,
            "stripe": remote_customer,
            "action": "reported_only",
        })
        report["status"] = "anomaly"
        _log.error("reconcile ANOMALY org=%s: local customer=%s but subscription %s "
                   "belongs to %s — not corrected automatically",
                   org_id, sub.stripe_customer_id, sub.stripe_subscription_id,
                   remote_customer)
        return report          # refuse to mirror another tenant's subscription

    duplicates = _extra_stripe_subscriptions(stripe_client, sub)
    if duplicates:
        report["anomalies"].append({
            "field": "stripe_subscription_id",
            "local": sub.stripe_subscription_id,
            "stripe_extra_active": duplicates,
            "action": "reported_only",
        })
        report["status"] = "anomaly"
        _log.error("reconcile ANOMALY org=%s: customer %s has additional active "
                   "subscriptions %s alongside the recorded %s — a duplicate may "
                   "be billing in parallel; resolve manually",
                   org_id, sub.stripe_customer_id, duplicates,
                   sub.stripe_subscription_id)

    # ── State mirror: status / cycle / plan / renewal / cancellation ──────────
    try:
        changed = billing_webhooks.apply_subscription_state(
            db, sub, remote, event_type="reconcile")
    except billing_webhooks.WebhookRejected as exc:
        db.rollback()
        report["anomalies"].append({"field": "state", "reason": str(exc),
                                    "action": "reported_only"})
        report["status"] = "anomaly"
        _log.error("reconcile ANOMALY org=%s: %s", org_id, exc)
        return report

    if changed:
        report["divergences"] = {k: {"local": _repr(v[0]), "stripe": _repr(v[1])}
                                 for k, v in changed.items()}
        if apply:
            db.commit()
            report["status"] = "corrected"
            _log.warning("reconcile corrected org=%s: %s", org_id,
                         list(changed.keys()))
        else:
            db.rollback()
            report["status"] = "divergent"
    elif not apply:
        db.rollback()

    # ── Quantity: delegate, do not reimplement (Phase 2.2 owns seat writes) ──
    desired = billing_seats.desired_seat_quantity(db, org_id)
    remote_qty = _remote_quantity(remote)
    if remote_qty is not None and remote_qty != desired:
        report["divergences"]["quantity"] = {"local": desired, "stripe": remote_qty}
        if apply:
            seat_result = billing_seats.sync_org_seats(
                db, org_id, stripe_client=stripe_client, reason="reconcile")
            report["quantity_sync"] = seat_result.get("status")
            if report["status"] == "in_sync":
                report["status"] = "corrected"
        elif report["status"] == "in_sync":
            report["status"] = "divergent"

    return report


def _repr(value):
    """JSON-safe rendering for the report."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def _remote_quantity(remote: Any) -> Optional[int]:
    items = billing_webhooks._g(billing_webhooks._g(remote, "items"), "data") or []
    return billing_webhooks._g(items[0], "quantity") if items else None


def _extra_stripe_subscriptions(stripe_client, sub) -> list:
    """Active Stripe subscriptions for this customer other than the recorded one.

    This is the Phase 2.1 duplicate-subscription failure mode: a second
    subscription billing alongside the one we track. Best-effort — if the listing
    call is unavailable we simply do not report duplicates.
    """
    try:
        listed = stripe_client.Subscription.list(customer=sub.stripe_customer_id,
                                                 status="active", limit=10)
    except Exception:
        return []
    data = billing_webhooks._g(listed, "data") or []
    return [sid for sid in (billing_webhooks._g(s, "id") for s in data)
            if sid and sid != sub.stripe_subscription_id]


def reconcile_all(db: Session, *, stripe_client=None,
                  apply: bool = True) -> Dict[str, Any]:
    """Sweep every organization that has a Stripe subscription."""
    tally = {"checked": 0, "in_sync": 0, "corrected": 0, "divergent": 0,
             "anomaly": 0, "stripe_unavailable": 0, "reports": []}
    # Internal tenants never reach Stripe, so they are excluded here rather than
    # skipped per-row — a sweep must not be able to open a call for a non-billable
    # organization even if one still carries Stripe ids.
    subs = (
        db.query(models.BillingSubscription)
          .join(models.Organization,
                models.Organization.id == models.BillingSubscription.organization_id)
          .filter(models.BillingSubscription.stripe_subscription_id.isnot(None),
                  models.BillingSubscription.organization_id.isnot(None),
                  models.Organization.billing_type != billing_service.BILLING_TYPE_INTERNAL)
          .all()
    )
    for sub in subs:
        tally["checked"] += 1
        report = reconcile_subscription(db, sub.organization_id,
                                        stripe_client=stripe_client, apply=apply)
        status = report.get("status")
        if status in tally:
            tally[status] += 1
        if status not in ("in_sync",):
            tally["reports"].append(report)
    _log.info("subscription reconciliation sweep: %s",
              {k: v for k, v in tally.items() if k != "reports"})
    return tally


def run_once_sync() -> Dict[str, Any]:
    """One sweep with its own session — safe to call from a thread."""
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        return reconcile_all(db)
    finally:
        db.close()


async def run_forever(interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                      initial_delay_seconds: int = 0) -> None:
    """Background sweep. Mirrors the seat job's shape (app/billing/seats.py)."""
    # Environment gate. The task is still created so the shutdown handler stays
    # valid, but it exits immediately outside production: these sweeps send real
    # mail and can modify Stripe, and "Subscription reconciliation" running on a developer laptop is
    # how real customers were once emailed. Enable deliberately with
    # BILLING_JOBS_ENABLED=true.
    from app.billing import environment as _billing_env    # noqa: PLC0415
    if not _billing_env.billing_jobs_enabled():
        _log.info("Subscription reconciliation not started (environment=%s)", _billing_env.environment())
        return
    # Stagger the first run — see the note in main.py.
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    _log.info("subscription reconciliation job started (every %ss)", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await asyncio.to_thread(run_once_sync)
        except asyncio.CancelledError:
            _log.info("subscription reconciliation job stopped")
            raise
        except Exception as exc:
            _log.error("subscription reconciliation sweep failed: %s", exc)
