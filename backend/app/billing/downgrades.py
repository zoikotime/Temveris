"""Downgrade approval and application.

``billing_downgrade_requests`` was write-only. The endpoint created a row, told the
customer "a billing specialist will review it within one business day", and no code —
no job, no screen, no query — ever read the table. Twenty-one rows accumulated over
two and a half months.

Worse, the review gate was decorative: ``/upgrade`` had no tier check, so a customer
could simply request a cheaper plan there and it applied to Stripe immediately,
bypassing review and every capacity check. That hole is closed in the router; this
module is the path a downgrade now actually takes.

Rules (approved 2026-08-13):

* A downgrade requires **super-admin approval** — never self-service, never automatic.
* It is **refused when the target plan cannot hold the organization's billable
  headcount**. Governed→Verified with 57 people on a 25-seat plan is not a discount,
  it is an unbillable state.
* Applying it reuses ``service.change_stripe_plan``, so the existing Stripe
  subscription is *modified* — Phase 2.1's no-duplicate-subscription guarantee holds.
* Capacity is re-checked at apply time, not just at approval: headcount moves between
  the two.
* An **internal tenant can never be involved** — it has no Stripe subscription to move.

Statuses: pending → approved → applied, or pending → rejected. ``approved`` exists so
a super-admin can green-light a change that takes effect on the requested date.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.billing import service as billing_service
from database import models

_log = logging.getLogger("zoiko.billing.downgrades")

PENDING = "pending"
APPROVED = "approved"
APPLIED = "applied"
REJECTED = "rejected"

OPEN_STATUSES = frozenset({PENDING, APPROVED})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class DowngradeError(Exception):
    """Carries an HTTP status so the router can translate without re-deciding."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def serialize(db: Session, req: models.BillingDowngradeRequest) -> Dict[str, Any]:
    cur = (db.query(models.BillingPlan)
             .filter(models.BillingPlan.id == req.current_plan_id).first())
    tgt = (db.query(models.BillingPlan)
             .filter(models.BillingPlan.id == req.requested_plan_id).first())
    org = (db.query(models.Organization)
             .filter(models.Organization.id == req.organization_id).first())
    shortfall = billing_service.plan_capacity_shortfall(db, req.organization_id, tgt)
    return {
        "id": req.id,
        "organization_id": req.organization_id,
        "organization_name": org.name if org else None,
        "current_plan": cur.code if cur else None,
        "requested_plan": tgt.code if tgt else None,
        "requested_plan_user_limit": tgt.user_limit if tgt else None,
        "billable_users": billing_service.count_billable_seats(db, req.organization_id)
                          if req.organization_id else None,
        "status": req.status,
        "reason": req.reason,
        "acknowledged_risk": req.acknowledged_risk,
        "preferred_effective_date": (req.preferred_effective_date.isoformat()
                                     if req.preferred_effective_date else None),
        "requested_by_user_id": req.requested_by_user_id,
        "reviewed_by_user_id": req.reviewed_by_user_id,
        "review_notes": req.review_notes,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        # Surfaced so a reviewer sees the blocker before approving, not after.
        "capacity_blocked": shortfall is not None,
        "capacity_detail": (f"{shortfall[0]} billable users exceed the "
                            f"{shortfall[1]}-seat limit") if shortfall else None,
    }


def list_requests(db: Session, *, status: Optional[str] = None,
                  organization_id: Optional[int] = None) -> list:
    q = db.query(models.BillingDowngradeRequest)
    if status:
        q = q.filter(models.BillingDowngradeRequest.status == status)
    if organization_id is not None:
        q = q.filter(models.BillingDowngradeRequest.organization_id == organization_id)
    return [serialize(db, r) for r in
            q.order_by(models.BillingDowngradeRequest.id.desc()).all()]


def _load_open(db: Session, request_id: int) -> models.BillingDowngradeRequest:
    req = (db.query(models.BillingDowngradeRequest)
             .filter(models.BillingDowngradeRequest.id == request_id).first())
    if req is None:
        raise DowngradeError(404, "Downgrade request not found.")
    if req.status not in OPEN_STATUSES:
        raise DowngradeError(409, f"This request is already {req.status}.")
    return req


def _validate(db: Session, req: models.BillingDowngradeRequest):
    """Shared checks for approve and apply. Returns (sub, target_plan)."""
    org_id = req.organization_id
    if org_id is None:
        raise DowngradeError(409, "This request has no organization and cannot be applied.")
    if billing_service.is_internal_tenant(db, org_id):
        raise DowngradeError(
            403, "This organization is an internal Temveris tenant and is not billed.")

    target = (db.query(models.BillingPlan)
                .filter(models.BillingPlan.id == req.requested_plan_id).first())
    if target is None:
        raise DowngradeError(409, "The requested plan no longer exists.")

    sub = (db.query(models.BillingSubscription)
             .filter(models.BillingSubscription.organization_id == org_id).first())
    if sub is None:
        raise DowngradeError(409, "This organization has no subscription.")

    # Capacity, re-checked every time: headcount changes between request, approval and
    # application, so a check done only at approval could apply an impossible state.
    try:
        billing_service.assert_plan_capacity(db, org_id, target)
    except billing_service.PlanCapacityExceeded as exc:
        raise DowngradeError(409, str(exc))

    return sub, target


def approve(db: Session, request_id: int, *, reviewer: models.User,
            notes: Optional[str] = None, apply_now: bool = False) -> Dict[str, Any]:
    """Super-admin approval. Applies immediately only when asked to.

    Otherwise the request waits for its effective date and ``apply_due`` picks it up.
    """
    req = _load_open(db, request_id)
    _validate(db, req)

    req.status = APPROVED
    req.reviewed_by_user_id = reviewer.id
    req.review_notes = notes
    db.commit()
    _log.info("downgrade request %s APPROVED by user=%s (apply_now=%s)",
              req.id, reviewer.id, apply_now)

    if apply_now:
        return apply_request(db, req.id, actor=reviewer)
    return serialize(db, req)


def reject(db: Session, request_id: int, *, reviewer: models.User,
           notes: Optional[str] = None) -> Dict[str, Any]:
    req = _load_open(db, request_id)
    req.status = REJECTED
    req.reviewed_by_user_id = reviewer.id
    req.review_notes = notes
    db.commit()
    _log.info("downgrade request %s REJECTED by user=%s", req.id, reviewer.id)
    return serialize(db, req)


def apply_request(db: Session, request_id: int, *,
                  actor: Optional[models.User] = None,
                  stripe_client=None) -> Dict[str, Any]:
    """Move the organization onto the requested plan.

    Goes through ``service.change_stripe_plan``, so the *existing* Stripe subscription
    is modified rather than a second one created (Phase 2.1).
    """
    req = (db.query(models.BillingDowngradeRequest)
             .filter(models.BillingDowngradeRequest.id == request_id).first())
    if req is None:
        raise DowngradeError(404, "Downgrade request not found.")
    if req.status == APPLIED:
        # Idempotent: a second call is a no-op rather than a second Stripe change.
        return serialize(db, req)
    if req.status != APPROVED:
        raise DowngradeError(409, f"A {req.status} request cannot be applied — "
                                  "it must be approved first.")

    sub, target = _validate(db, req)

    cycle = sub.billing_cycle or "monthly"
    price_key = f"STRIPE_PRICE_{(target.code or '').upper()}_{cycle.upper()}"
    price_id = os.getenv(price_key)
    if not price_id:
        _log.error("downgrade %s: %s is not configured", req.id, price_key)
        raise DowngradeError(
            500, "Billing is not fully configured for the target plan. No change "
                 "has been made to the subscription.")

    seats = max(billing_service.count_billable_seats(db, req.organization_id), 1)
    try:
        billing_service.change_stripe_plan(
            db, sub, price_id=price_id, seats=seats, target_plan=target,
            billing_cycle=cycle, stripe_client=stripe_client,
        )
    except billing_service.StripePlanChangeError as exc:
        raise DowngradeError(getattr(exc, "status_code", 502), str(exc))

    req.status = APPLIED
    if actor is not None and req.reviewed_by_user_id is None:
        req.reviewed_by_user_id = actor.id
    db.commit()
    _log.info("downgrade request %s APPLIED: org=%s -> plan=%s seats=%s",
              req.id, req.organization_id, target.code, seats)
    return serialize(db, req)


def apply_due(db: Session, *, now: Optional[datetime] = None,
              stripe_client=None) -> Dict[str, Any]:
    """Apply approved requests whose effective date has arrived.

    Only ``approved`` rows are ever touched — a pending request is never auto-applied,
    so the 21 pre-existing production rows stay exactly as they are until a human
    reviews them.
    """
    now = now or _now()
    tally = {"checked": 0, "applied": 0, "blocked": 0, "reports": []}
    rows = (db.query(models.BillingDowngradeRequest)
              .filter(models.BillingDowngradeRequest.status == APPROVED)
              .all())
    for req in rows:
        effective = _aware(req.preferred_effective_date)
        if effective is not None and effective > now:
            continue
        tally["checked"] += 1
        try:
            apply_request(db, req.id, stripe_client=stripe_client)
            tally["applied"] += 1
        except DowngradeError as exc:
            tally["blocked"] += 1
            tally["reports"].append({"id": req.id, "error": str(exc)})
            _log.warning("downgrade %s could not be applied: %s", req.id, exc)
    if tally["checked"]:
        _log.info("downgrade apply sweep: %s",
                  {k: v for k, v in tally.items() if k != "reports"})
    return tally
