"""Self-serve subscription lifecycle mutations (Enterprise Billing §1).

Fills a real gap: the subscription state machine has the cancellation edges
(active → cancelled_pending_end → cancelled) but nothing called them, so the
``cancelled`` state was unreachable. These functions drive those transitions —
idempotently, audited, with events — reusing the existing SM, subscription
model, and audit emitter. No duplication of billing/checkout logic.

Semantics:
  * cancel(at_renewal)  : active → cancelled_pending_end (keeps access to period end)
  * cancel(immediate)   : → cancelled (access ends now)
  * undo_cancel()       : cancelled_pending_end → active (undo before period end)

All operations are safe to repeat: a call that would be a no-op returns the
current state without re-emitting events.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.commercial import state_machines
from app.commercial.audit import emit_event
from app.commercial.models import CommercialSubscription, CommercialOrganization
from database.models import User

_log = logging.getLogger("zoiko.commercial.subscription_ops")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SubscriptionOpError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _load(db: Session, organization_id: str) -> CommercialSubscription:
    sub = (db.query(CommercialSubscription)
           .filter_by(organization_id=organization_id).one_or_none())
    if sub is None:
        raise SubscriptionOpError("SUBSCRIPTION_STATE_INVALID",
                                  "No subscription exists for this organization.")
    return sub


def _sync_org(db: Session, organization_id: str, state: str) -> None:
    org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()
    if org is not None:
        org.commercial_state = state


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _result(sub: CommercialSubscription) -> dict:
    return {
        "organization_id": sub.organization_id,
        "state": sub.state,
        "cancel_requested_at": _iso(sub.cancel_requested_at),
        "cancelled_at": _iso(sub.cancelled_at),
    }


def cancel(db: Session, *, organization_id: str, mode: str = "at_renewal",
           correlation_id: Optional[str] = None) -> dict:
    """Cancel a subscription. mode='at_renewal' schedules end-of-period
    cancellation; mode='immediate' cancels now. Idempotent."""
    sub = _load(db, organization_id)
    start = sub.state

    if start == "cancelled":
        return _result(sub)  # already fully cancelled — no-op
    if start not in ("active", "cancelled_pending_end"):
        raise SubscriptionOpError(
            "SUBSCRIPTION_STATE_INVALID",
            f"A subscription in state '{start}' cannot be cancelled here.",
        )

    if start == "active":
        sub.state = state_machines.subscription.transition("active", "cancelled_pending_end")
        sub.cancel_requested_at = sub.cancel_requested_at or _now()

    if mode == "immediate" and sub.state == "cancelled_pending_end":
        sub.state = state_machines.subscription.transition("cancelled_pending_end", "cancelled")
        sub.cancelled_at = _now()

    if sub.state == start:
        return _result(sub)  # nothing changed (e.g. at_renewal on already-pending) — no event

    _sync_org(db, organization_id, sub.state)
    event = ("subscription.cancelled" if sub.state == "cancelled"
             else "subscription.cancellation_scheduled")
    emit_event(db, event_type=event, source_service="subscription-service",
               payload={"mode": mode}, organization_id=organization_id,
               correlation_id=correlation_id, state_from=start, state_to=sub.state)

    # ZT-BIL-07 (Essential - subscription). Best-effort: a mail failure must
    # never affect the cancellation. Only reached on a real state change, so the
    # no-op returns above cannot produce a duplicate email.
    try:
        from app.commercial.notifications import send_cancellation
        send_cancellation(
            db, organization_id=organization_id, subscription=sub,
            cancellation_status="canceled" if sub.state == "cancelled" else "scheduled",
            correlation_id=correlation_id,
        )
    except Exception:
        _log.warning("ZT-BIL-07 cancellation email failed (cancellation preserved)", exc_info=True)

    db.commit()
    return _result(sub)


def undo_cancel(db: Session, *, organization_id: str,
                correlation_id: Optional[str] = None) -> dict:
    """Undo a scheduled cancellation (cancelled_pending_end → active). Idempotent."""
    sub = _load(db, organization_id)
    start = sub.state

    if start == "active":
        return _result(sub)  # nothing to undo — no-op
    if start != "cancelled_pending_end":
        raise SubscriptionOpError(
            "SUBSCRIPTION_STATE_INVALID",
            f"A subscription in state '{start}' cannot be reactivated here.",
        )

    sub.state = state_machines.subscription.transition("cancelled_pending_end", "active")
    sub.cancel_requested_at = None
    _sync_org(db, organization_id, "active")
    emit_event(db, event_type="subscription.reactivated",
               source_service="subscription-service", payload={},
               organization_id=organization_id, correlation_id=correlation_id,
               state_from=start, state_to="active")

    # ZT-BIL-07 reversed (Essential - subscription). Best-effort: a mail failure
    # must never affect the reactivation.
    try:
        from app.commercial.notifications import send_cancellation
        send_cancellation(db, organization_id=organization_id, subscription=sub,
                          cancellation_status="reversed", correlation_id=correlation_id)
    except Exception:
        _log.warning("ZT-BIL-07 reactivation email failed (reactivation preserved)", exc_info=True)

    db.commit()
    return _result(sub)


def used_seats(db: Session, organization_id: str) -> int:
    """Active assigned seats. Counts active users in the linked operational org;
    falls back to 1 (the owner) when a self-signup org isn't linked yet.

    ponytail: counts active users only; pending invites aren't reserved seats
    here — tighten to include is_invited if seat reservations become billable.
    """
    org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()
    op_id = org.operational_org_id if org else None
    if not op_id:
        return 1
    n = (db.query(User)
         .filter(User.organization_id == op_id, User.is_active.is_(True))
         .count())
    return max(n, 1)


def _seat_result(sub: CommercialSubscription, used: int) -> dict:
    seats = sub.seat_count or 0
    return {
        "organization_id": sub.organization_id,
        "seat_count": seats,
        "used_seats": used,
        "available_seats": max(seats - used, 0),
    }


def change_seats(db: Session, *, organization_id: str, seat_count: int,
                 correlation_id: Optional[str] = None) -> dict:
    """Set the subscription seat count. Guards against reducing below the seats
    already in use (no over-allocation). Idempotent (same count → no-op)."""
    if seat_count is None or seat_count < 1:
        raise SubscriptionOpError("VALIDATION_FAILED", "seat_count must be at least 1.")
    sub = _load(db, organization_id)
    used = used_seats(db, organization_id)
    if seat_count < used:
        raise SubscriptionOpError(
            "SEAT_OVER_ALLOCATION",
            f"Cannot reduce to {seat_count} seats; {used} are currently in use.",
        )
    current = sub.seat_count or 0
    if seat_count == current:
        return _seat_result(sub, used)  # no change — no event

    sub.seat_count = seat_count
    emit_event(db, event_type="subscription.seats_changed",
               source_service="subscription-service",
               payload={"from": current, "to": seat_count,
                        "delta": seat_count - current, "used": used},
               organization_id=organization_id, correlation_id=correlation_id)
    db.commit()
    return _seat_result(sub, used)


# ══════════════════════════════════════════════════════════════════════════════
# Billing → commercial bridge
# ══════════════════════════════════════════════════════════════════════════════
# The operational billing row (``billing_subscriptions``) is the record Stripe
# writes to. The entitlement resolver never reads it — it reads
# ``CommercialOrganization.commercial_state`` and ``CommercialSubscription``
# (.plan_code / .state). Nothing connected the two, so a customer could pay for
# Governed and keep Verified entitlements until someone edited the commercial
# row by hand. ``sync_from_billing`` is that missing edge.
#
# It is deliberately a *projection*, not a second source of truth: it only ever
# copies an already-authoritative billing value onto the commercial row. Every
# Stripe-driven billing write funnels through three call sites, and all three
# call this:
#   1. webhooks.apply_subscription_state  (checkout.completed, subscription.updated,
#      invoice.paid, invoice.payment_failed — and reconcile.py's repair sweep)
#   2. webhooks.handle_subscription_deleted  (sets status directly, bypassing 1)
#   3. billing.router /upgrade/confirm  (synchronous post-redirect confirmation)
#
# Idempotency and atomicity come from the caller: the webhook claim row and this
# update share one transaction, so an event cannot be marked processed unless
# the commercial projection was durably applied with it.

#: Local billing status → commercial state. Left side is the *normalized* local
#: status produced by ``webhooks.STRIPE_STATUS_MAP`` / ``derive_local_status``,
#: not a raw Stripe status. Statuses absent from this table are deliberately not
#: projected — see ``_UNMAPPED_STATUSES``.
BILLING_TO_COMMERCIAL_STATE = {
    "active":         "active",
    "trial":          "trial_active",
    "trial_expired":  "trial_expired",
    "past_due":       "past_due",
    "cancel_pending": "cancelled_pending_end",
    "canceled":       "cancelled",
    "cancelled":      "cancelled",   # spelling tolerated from non-Stripe writers
}

#: Statuses with no unambiguous commercial equivalent. ``incomplete`` means the
#: very first payment never finalized: the tenant has not gained a paid plan,
#: but neither have they lost the trial they were already on. Projecting either
#: way is a business decision, not an engineering one, so the commercial row is
#: left exactly as it is and the skip is logged. This is strictly safer than
#: guessing — it preserves today's behaviour for these two statuses only.
_UNMAPPED_STATUSES = frozenset({"incomplete", "incomplete_expired"})

#: Commercial states this projection will never move a tenant out of. Mirrors
#: ``webhooks.TERMINAL_STATES``: once a tenant is cancelled or closed, a late or
#: replayed billing event must not silently resurrect their access.
_TERMINAL_COMMERCIAL = frozenset({"cancelled", "closed"})


def sync_from_billing(
    db: Session,
    *,
    operational_org_id: Optional[int],
    billing_status: Optional[str],
    plan_code: Optional[str] = None,
    source: str = "billing",
) -> Optional[dict]:
    """Project an operational billing row onto the commercial row entitlements read.

    Returns a dict of ``{field: (before, after)}`` for what changed, ``{}`` when
    the commercial row already agreed, or ``None`` when this tenant has no
    commercial records at all (internal/super-admin orgs provisioned before the
    commercial layer existed — nothing to sync, and not an error).

    Does not commit: the caller's transaction owns the commit, so this update
    lands atomically with the billing write that triggered it.
    """
    if operational_org_id is None:
        return None

    org = (db.query(CommercialOrganization)
             .filter(CommercialOrganization.operational_org_id == operational_org_id)
             .one_or_none())
    if org is None:
        return None
    sub = (db.query(CommercialSubscription)
             .filter_by(organization_id=org.id).one_or_none())
    if sub is None:
        return None

    changed: dict = {}
    state_from = org.commercial_state

    # ── plan ──────────────────────────────────────────────────────────────
    # Stripe's price is authoritative for which plan was actually paid for.
    if plan_code and sub.plan_code != plan_code:
        changed["plan_code"] = (sub.plan_code, plan_code)
        sub.plan_code = plan_code

    # ── state ─────────────────────────────────────────────────────────────
    target = BILLING_TO_COMMERCIAL_STATE.get(billing_status or "")
    if target is None:
        if billing_status in _UNMAPPED_STATUSES:
            _log.info(
                "commercial sync: leaving org=%s state=%s untouched — billing "
                "status %r has no unambiguous commercial equivalent",
                org.id, org.commercial_state, billing_status)
        elif billing_status:
            _log.warning("commercial sync: unknown billing status %r for org=%s",
                         billing_status, org.id)
    elif org.commercial_state in _TERMINAL_COMMERCIAL and target != org.commercial_state:
        _log.warning(
            "commercial sync: refusing to move org=%s out of terminal state %s "
            "to %s (billing_status=%s, source=%s)",
            org.id, org.commercial_state, target, billing_status, source)
    else:
        # entitlements.resolve() reads ``org.commercial_state or sub.state`` —
        # write both so the fallback can never contradict the primary.
        if org.commercial_state != target:
            changed["commercial_state"] = (org.commercial_state, target)
            org.commercial_state = target
        if sub.state != target:
            changed["state"] = (sub.state, target)
            sub.state = target

    if not changed:
        return {}

    # The resolver caches per organization; a stale entry would keep serving the
    # old plan for the whole TTL, which is the exact bug this bridge closes.
    from app.commercial import entitlements  # noqa: PLC0415 — avoids an import cycle
    entitlements.invalidate(db, org.id)

    emit_event(
        db,
        event_type="entitlements.recomputed",
        source_service="billing_bridge",
        organization_id=org.id,
        state_from=state_from,
        state_to=org.commercial_state,
        payload={
            "source": source,
            "billing_status": billing_status,
            "changed": {k: {"from": v[0], "to": v[1]} for k, v in changed.items()},
        },
    )
    _log.info("commercial sync from %s: org=%s %s", source, org.id,
              {k: f"{v[0]}->{v[1]}" for k, v in changed.items()})
    return changed
