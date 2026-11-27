"""Seat quantity synchronisation — ZT-COM-BILL-001 Phase 2.2.

Billable seats are *derived* from an organization's workers (§17
``organization.active_billable_workers`` — "Derived from worker billing
states"), not purchased separately. This module keeps the Stripe subscription's
quantity equal to that derived count.

Design notes that matter:

* **Absolute, never delta.** Every sync recomputes the desired count and sends
  that absolute value to Stripe. Two near-simultaneous activations therefore
  converge on the true total instead of racing to ``5 -> 6`` twice.
* **Seat-only syncs never send a price.** ``Subscription.modify`` is called with
  ``items=[{id, quantity}]`` and no ``price``, so a seat change can never
  silently move a customer between plans. Combined plan+seat changes go through
  ``service.change_stripe_plan`` as a *single* modify (one proration event).
* **Never raises into the employee lifecycle.** Deactivating a leaver must not
  fail because Stripe is briefly unreachable. Failures are logged, recorded as a
  billing audit event, and left for reconciliation.
* **Never creates a subscription.** An employee change is not a purchase.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from database import models
from app.billing import service as billing_service
from app.billing import worker_state

_log = logging.getLogger("zoiko.billing.seats")

# Stripe is charged the absolute seat count, but a subscription cannot have
# quantity 0 — an org with no billable workers still holds its subscription.
MIN_QUANTITY = 1

SEAT_SYNC_FAILED = "seat_sync_failed"
SEAT_SYNC_DRIFT = "seat_sync_drift"


# ══════════════════════════════════════════════════════════════════════════════
# Desired quantity
# ══════════════════════════════════════════════════════════════════════════════
def desired_seat_quantity(db: Session, org_id: Optional[int]) -> int:
    """The quantity Stripe should be charging this organization for.

    Billable: ACTIVE_BILLABLE, SUSPENDED_BILLABLE.
    Not billable: INVITED_PENDING, DEACTIVATED, TERMINATED, ARCHIVED,
    MIGRATION_REVIEW, NULL. Always scoped to one organization.
    """
    return max(billing_service.count_billable_seats(db, org_id), MIN_QUANTITY)


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle: write the worker's billing state (and keep Stripe in step)
# ══════════════════════════════════════════════════════════════════════════════
def set_worker_billing_state(
    db: Session,
    user: models.User,
    state: str,
    *,
    sync: bool = True,
    reason: str = "lifecycle",
    enforce_capacity: bool = True,
) -> None:
    """Record a worker's billing state, then best-effort resync the org's seats.

    ``billing_state`` is the sole authority for billability — deliberately not
    derived from ``is_active``/``is_invited``, which cannot distinguish invited
    from accepted, or suspended from deactivated.

    The caller is expected to commit; we only flush so the new state is visible
    to the seat count in the same transaction. Stripe sync happens after and can
    never raise here.

    ``enforce_capacity=False`` records the state even when it puts the organization
    over its plan ceiling, logging the overage instead of raising. Used by the invite
    *acceptance* path: the person setting their password cannot upgrade the plan, so
    refusing there would break an individual's account setup over their employer's
    billing. Capacity is refused earlier instead, at invite and add time, where an
    administrator sees it and can act — see service.assert_room_for_new_worker.
    """
    if state not in worker_state.ALL_STATES:
        raise ValueError(f"unknown billing_state {state!r}")

    if user.billing_state == state:
        return

    # Plan capacity, enforced at the one place billability is granted.
    #
    # user_limit used to be advisory — it only coloured an alert — so an organization
    # could grow arbitrarily past the tier it paid for. Checked only when a worker is
    # *becoming* billable, and only for a real capacity breach: removing billability,
    # or moving between two billable states, can never push an org over its limit.
    if (state in worker_state.BILLABLE_STATES
            and user.billing_state not in worker_state.BILLABLE_STATES
            and user.organization_id is not None):
        sub = (
            db.query(models.BillingSubscription)
              .filter(models.BillingSubscription.organization_id == user.organization_id)
              .first()
        )
        plan = (db.query(models.BillingPlan)
                  .filter(models.BillingPlan.id == sub.plan_id).first()) if sub else None
        # extra_seats=1: this worker is not billable yet, so ask whether they would fit.
        if enforce_capacity:
            billing_service.assert_plan_capacity(db, user.organization_id, plan,
                                                 extra_seats=1)
        else:
            over = billing_service.plan_capacity_shortfall(
                db, user.organization_id, plan, extra_seats=1)
            if over:
                # Recorded loudly, not refused: blocking here would fail in front of a
                # person who cannot fix it. The org is now over its ceiling, which the
                # red "Active users exceed plan limit" alert surfaces to its admins,
                # and Stripe bills the true count regardless.
                _log.warning(
                    "plan capacity EXCEEDED and allowed: org=%s user=%s "
                    "billable_would_be=%s limit=%s reason=%s",
                    user.organization_id, user.id, over[0], over[1], reason,
                )

    previous = user.billing_state
    user.billing_state = state
    db.flush()
    _log.info(
        "worker billing_state: user=%s org=%s %s -> %s (%s)",
        user.id, user.organization_id, previous, state, reason,
    )

    if sync and user.organization_id is not None:
        sync_org_seats(db, user.organization_id, reason=f"{reason}:{state}")


# ══════════════════════════════════════════════════════════════════════════════
# Stripe quantity synchronisation
# ══════════════════════════════════════════════════════════════════════════════
def sync_org_seats(
    db: Session,
    org_id: Optional[int],
    *,
    stripe_client=None,
    reason: str = "manual",
    _depth: int = 0,
) -> Dict[str, Any]:
    """Make Stripe's quantity equal this organization's billable seat count.

    Returns a status dict; never raises. Statuses:
      no_subscription        — org has no billing_subscriptions row
      no_stripe_subscription — not yet purchased; nothing to sync, none created
      internal_tenant        — not billed at all; local counters only
      in_sync                — Stripe already correct, no API call made
      updated                — quantity modified
      failed                 — Stripe error; recorded for reconciliation
      drift                  — Stripe updated but local persistence failed
    """
    try:
        org_id = billing_service.require_org_id(org_id)
    except billing_service.MissingOrganization:
        # Fail closed: a worker with no organization can never move another
        # tenant's quantity.
        _log.warning("seat sync skipped: no organization (reason=%s)", reason)
        return {"status": "no_organization"}

    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    if sub is None:
        return {"status": "no_subscription", "organization_id": org_id}

    desired = desired_seat_quantity(db, org_id)

    # An internal tenant is never charged, so its seat count must never reach
    # Stripe — even if it still carries Stripe ids from before it was exempted.
    # Local counters stay accurate so the UI and reports remain truthful.
    if billing_service.is_internal_tenant(db, org_id):
        _persist_local_quantity(db, sub, desired)
        return {"status": "internal_tenant", "organization_id": org_id,
                "desired": desired}

    # Not a customer yet. Keep local counters honest, but an employee change is
    # not a purchase — provisioning stays with checkout.
    if not sub.stripe_subscription_id or not sub.stripe_customer_id:
        _persist_local_quantity(db, sub, desired)
        return {"status": "no_stripe_subscription", "organization_id": org_id,
                "desired": desired}

    if stripe_client is None:                     # pragma: no cover - runtime path
        import stripe as stripe_client            # noqa: PLC0415

    subscription_id = sub.stripe_subscription_id
    try:
        remote = stripe_client.Subscription.retrieve(subscription_id)

        # Tenancy: the ids come from this org's own row, but a mismatch would
        # mean the row points at another tenant's subscription. Refuse.
        if remote["customer"] != sub.stripe_customer_id:
            _log.error(
                "seat sync tenancy anomaly: org=%s row customer=%s subscription %s "
                "belongs to %s", org_id, sub.stripe_customer_id, subscription_id,
                remote["customer"],
            )
            _record_failure(db, org_id, subscription_id, desired,
                            "customer_mismatch", reason)
            return {"status": "failed", "error": "customer_mismatch"}

        if remote["status"] not in billing_service.MODIFIABLE_STRIPE_STATES:
            _log.warning(
                "seat sync skipped: org=%s subscription %s is %s",
                org_id, subscription_id, remote["status"],
            )
            return {"status": "failed", "error": f"status_{remote['status']}"}

        items = remote["items"]["data"]
        if len(items) != 1:
            _log.error("seat sync: subscription %s has %d items", subscription_id,
                       len(items))
            _record_failure(db, org_id, subscription_id, desired,
                            "unexpected_item_count", reason)
            return {"status": "failed", "error": "unexpected_item_count"}

        item = items[0]
        current = item["quantity"]
        if current == desired:
            _persist_local_quantity(db, sub, desired)
            return {"status": "in_sync", "organization_id": org_id,
                    "quantity": desired}

        # Seat-only change: no `price` key, so the plan cannot move here.
        #
        # payment_behavior="error_if_incomplete" matches service.change_stripe_plan
        # (Phase 2.1). Without it, Stripe accepts the seat increase and returns
        # 200 even when the proration invoice cannot be collected, leaving the
        # subscription "incomplete"/"past_due" while this function reported
        # "updated" and mirrored the new quantity locally — i.e. seats granted
        # against a payment that never landed. With it, an uncollectable seat
        # change raises, so it lands in the failure path below: recorded for
        # reconciliation, local quantity untouched, and never reported as success.
        stripe_client.Subscription.modify(
            subscription_id,
            items=[{"id": item["id"], "quantity": desired}],
            proration_behavior="create_prorations",
            payment_behavior="error_if_incomplete",
            metadata={"organization_id": str(org_id), "seat_sync_reason": reason},
        )
        _log.info(
            "seat quantity synced: org=%s subscription=%s %s -> %s (%s)",
            org_id, subscription_id, current, desired, reason,
        )
    except Exception as exc:
        _log.error(
            "seat sync FAILED: org=%s subscription=%s desired=%s reason=%s error=%s",
            org_id, subscription_id, desired, reason, exc,
        )
        _record_failure(db, org_id, subscription_id, desired, str(exc)[:200], reason)
        return {"status": "failed", "organization_id": org_id, "desired": desired,
                "error": str(exc)}

    # Stripe has moved. Mirror it locally.
    try:
        _persist_local_quantity(db, sub, desired)
    except Exception as exc:
        db.rollback()
        _log.error(
            "BILLING DRIFT: stripe quantity=%s but local persist failed. "
            "org=%s subscription=%s customer=%s desired=%s error=%s",
            desired, org_id, subscription_id, sub.stripe_customer_id, desired, exc,
        )
        return {"status": "drift", "organization_id": org_id,
                "stripe_quantity": desired, "error": str(exc)}

    # Convergence: if workers changed while we were talking to Stripe, resync
    # once. Bounded so a rapidly-changing org cannot loop.
    if _depth == 0:
        latest = desired_seat_quantity(db, org_id)
        if latest != desired:
            _log.info(
                "seat count moved during sync (org=%s %s -> %s); converging",
                org_id, desired, latest,
            )
            return sync_org_seats(db, org_id, stripe_client=stripe_client,
                                  reason=f"{reason}:converge", _depth=1)

    return {"status": "updated", "organization_id": org_id,
            "previous_quantity": current, "quantity": desired}


def _persist_local_quantity(db: Session, sub: models.BillingSubscription,
                           desired: int) -> None:
    """Mirror the billable count onto the subscription row.

    ``active_users`` is what the UI renders and the upgrade modal multiplies by,
    so it must equal the quantity Stripe is charging.
    """
    if sub.billable_users == desired and sub.active_users == desired:
        return
    sub.billable_users = desired
    sub.active_users = desired
    db.commit()


def _record_failure(db: Session, org_id: int, subscription_id: Optional[str],
                    desired: int, error: str, reason: str) -> None:
    """Persist enough for reconciliation to retry, using the existing audit table
    (no new schema).

    Deliberately written on its **own** session. The caller is usually mid-way
    through an employee lifecycle operation with uncommitted work — touching
    their session (or worse, rolling it back to clear it) would undo the
    deactivation or activation we were called about. A Stripe outage must never
    revert the lifecycle change.
    """
    from database.database import SessionLocal  # noqa: PLC0415

    audit_db = SessionLocal()
    try:
        audit_db.add(models.BillingAuditEvent(
            organization_id=org_id,
            action_key=SEAT_SYNC_FAILED,
            summary=f"Seat sync failed (desired={desired})",
            details_json={
                "stripe_subscription_id": subscription_id,
                "desired_quantity": desired,
                "error": error,
                "reason": reason,
            },
        ))
        audit_db.commit()
    except Exception:
        audit_db.rollback()
        _log.exception("could not record seat sync failure for org=%s", org_id)
    finally:
        audit_db.close()


# ══════════════════════════════════════════════════════════════════════════════
# Reconciliation — the smallest mechanism that corrects drift
# ══════════════════════════════════════════════════════════════════════════════
def reconcile_org_seat_quantity(db: Session, org_id: int, *,
                                stripe_client=None) -> Dict[str, Any]:
    """Compare local desired quantity with Stripe and correct any difference.

    Deliberately just ``sync_org_seats`` under a clearer name: because sync
    compares before modifying and sends an absolute value, reconciliation and
    synchronisation are the same operation. Nothing extra to maintain.
    """
    return sync_org_seats(db, org_id, stripe_client=stripe_client,
                          reason="reconcile")


def reconcile_all_orgs(db: Session, *, stripe_client=None) -> Dict[str, int]:
    """Sweep every organization that has a Stripe subscription.

    Catches quantity drift from a failed lifecycle sync, a missed webhook, or a
    seat change made while Stripe was unreachable.
    """
    tally = {"checked": 0, "in_sync": 0, "updated": 0, "failed": 0, "skipped": 0}
    # Internal tenants are excluded in the query rather than skipped per-row, so a
    # background job can never open a Stripe call for a non-billable organization.
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
        result = reconcile_org_seat_quantity(db, sub.organization_id,
                                             stripe_client=stripe_client)
        status = result.get("status")
        if status == "in_sync":
            tally["in_sync"] += 1
        elif status == "updated":
            tally["updated"] += 1
        elif status in {"failed", "drift"}:
            tally["failed"] += 1
        else:
            tally["skipped"] += 1
    return tally


def run_once_sync() -> Dict[str, int]:
    """One reconciliation pass. Opens and closes its own session."""
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        return reconcile_all_orgs(db)
    finally:
        db.close()


async def run_forever(interval_seconds: int = 900,
                      initial_delay_seconds: int = 0) -> None:
    """Periodic seat reconciliation, matching the existing scheduler pattern."""
    # Environment gate. The task is still created so the shutdown handler stays
    # valid, but it exits immediately outside production: these sweeps send real
    # mail and can modify Stripe, and "Seat reconciliation" running on a developer laptop is
    # how real customers were once emailed. Enable deliberately with
    # BILLING_JOBS_ENABLED=true.
    from app.billing import environment as _billing_env    # noqa: PLC0415
    if not _billing_env.billing_jobs_enabled():
        _log.info("Seat reconciliation not started (environment=%s)", _billing_env.environment())
        return
    # Stagger the first run — see the note in main.py.
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    _log.info("Seat reconciliation scheduler started (interval=%ss)", interval_seconds)
    while True:
        try:
            tally = await asyncio.to_thread(run_once_sync)
            if tally.get("updated") or tally.get("failed"):
                _log.info("Seat reconciliation tick: %s", tally)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Seat reconciliation tick error")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
