"""Temveris — Billing Router (Tier-0 launch).

All endpoints are mounted under /api/admin/billing/* and gated by the
billing permission rules below. Permission checks run server-side, never
trust the client role state — see Spec §19.
"""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime, timezone, timedelta, timezone
from typing import Optional
import os
import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, Response, RedirectResponse
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user
from app.billing import payment_methods as billing_payment_methods
from app.billing import suspension as billing_suspension
from app.billing import service as billing_service
from app.billing import trial as billing_trial
from app.billing import webhooks as billing_webhooks
from app.billing.schemas import (
    UpgradeRequest, DowngradeRequestIn, EnterpriseQuoteIn,
    BillingContactIn, BillingContactPatch, TaxProfileIn, PaymentMethodIn,
)


router = APIRouter(prefix="/api/admin/billing", tags=["Billing"])

_log = logging.getLogger("zoiko.billing.router")


# ── Permission helpers ────────────────────────────────────────────────────────
# ZT-COM-BILL-001 §K7 / §15 canonical billing RBAC:
#   Owner          — view + manage
#   Billing Admin  — view + manage
#   Admin          — view only, never modifies billing
#   HR             — no billing access at all
#
# DOCUMENTED DEVIATION from §15 (approved 2026-08-12): "admin" is included in the
# manage set. §15 says Admin views but never modifies. The deviation is deliberate,
# because the doctrine's model is unreachable in practice:
#
#   * production has 0 users with role "owner" or "billing_admin" (62 employee,
#     8 admin, 1 super_admin), so nobody could perform ANY billing action; and
#   * frontend AdminRoute (App.jsx) admits only ["admin", "hr"], so granting a
#     user "owner" would remove their access to the admin area entirely.
#
# Revisit when the role model gains real owner/billing_admin users and the frontend
# gate is widened; at that point "admin" should be removed again and this comment
# deleted. Until then, tenant-level protection is what prevents mistaken spend:
# _require_billable_tenant blocks every Stripe-charging path for internal tenants.
# HR and employee remain excluded from billing entirely.
BILLING_VIEW_ROLES   = {"owner", "billing_admin", "admin"}
BILLING_MANAGE_ROLES = {"owner", "billing_admin", "admin"}

# How close to the end of the trial the amber "ending soon" banner appears. Matches the
# day-11 reminder email (14 - 11 = 3), so the banner and the email say the same thing on
# the same day rather than the banner running for the entire trial.
TRIAL_WARNING_DAYS = 3


def _require_view(user: models.User):
    if user.role not in BILLING_VIEW_ROLES:
        raise HTTPException(403, "You do not have permission to view billing.")


def _require_manage(user: models.User):
    if user.role not in BILLING_MANAGE_ROLES:
        raise HTTPException(403, "You do not have permission to make billing changes.")


def _require_billable_tenant(db: Session, org_id: int) -> int:
    """Gate the endpoints that can create a Stripe charge.

    Temveris's owner runs the product it sells: it must keep full plan
    entitlements but must never reach Stripe. Applied to /upgrade,
    /payment-method/session and /customer-portal — the only endpoints that create
    or modify a Stripe object. Read-only billing endpoints deliberately stay open
    so an internal tenant can still see its plan, seats and usage.
    """
    try:
        return billing_service.require_billable_tenant(db, org_id)
    except billing_service.InternalTenantBilling as exc:
        raise HTTPException(403, str(exc))


def _org(user: models.User) -> int:
    """The authenticated caller's organization — every billing resource is scoped
    to it (§4/§5). A user with no organization cannot transact billing; we fail
    closed rather than fall back to a shared tenant.
    """
    try:
        return billing_service.require_org_id(user.organization_id)
    except billing_service.MissingOrganization:
        raise HTTPException(
            400,
            "Your account is not linked to an organization, so billing is unavailable.",
        )


def _client_meta(request: Optional[Request]):
    if request is None:
        return None, None
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else None
    )
    ua = request.headers.get("user-agent", "")
    return ip, hashlib.sha256(ua.encode("utf-8")).hexdigest() if ua else None


def _checkout_idempotency_key(org_id: int, plan_code: str, cycle: str,
                              seats: int) -> str:
    """A stable Stripe idempotency key for one checkout intent.

    Scoped to the *intent* (organization, plan, cycle, seat count) rather than to the
    request, so every duplicate submission of the same intent — a double click, a
    refresh, a second tab, a retried POST, two concurrent requests — resolves to the
    same key and therefore to the same Checkout Session. Choosing a genuinely different
    plan produces a different key and correctly opens a new session.

    Stripe retains idempotency keys for 24 hours, which is far longer than any
    double-submit window. Hashed rather than concatenated so the key length stays fixed
    and no tenant identifier travels to Stripe in the clear.

    The version segment must change whenever the session parameters for an unchanged
    intent change. Stripe rejects a reused key whose parameters differ, so without the
    bump anyone who opened a checkout in the 24 hours before such a deploy would get an
    error instead of a session. v2: annual checkouts gained allow_promotion_codes.
    """
    raw = f"checkout:v2:{org_id}:{plan_code}:{cycle}:{seats}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _serialize_plan(p: models.BillingPlan) -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "description": p.description,
        "badge": p.badge,
        "short_description": p.short_description,
        "tier_order": p.tier_order,
        "monthly_price_per_user": p.monthly_price_per_user,
        "annual_price_per_user": p.annual_price_per_user,
        "annual_discount_percentage": p.annual_discount_percentage,
        "user_limit": p.user_limit,
        "feature_list_json": p.feature_list_json or [],
        "feature_flags_json": p.feature_flags_json or {},
        "is_most_popular": p.is_most_popular,
        "is_enterprise": p.is_enterprise,
        "trial_enabled": p.trial_enabled,
        "active_status": p.active_status,
        "currency": p.currency,
    }


def _serialize_subscription(sub: models.BillingSubscription, plan: Optional[models.BillingPlan]) -> dict:
    return {
        "id": sub.id,
        "organization_id": sub.organization_id,
        "plan": _serialize_plan(plan) if plan else None,
        "billing_cycle": sub.billing_cycle,
        "subscription_status": sub.subscription_status,
        "active_users": sub.active_users,
        "billable_users": sub.billable_users,
        "pending_invites": sub.pending_invites,
        "suspended_users": sub.suspended_users,
        "renewal_date": sub.renewal_date.isoformat() if sub.renewal_date else None,
        "trial_start_date": sub.trial_start_date.isoformat() if sub.trial_start_date else None,
        "trial_end_date": sub.trial_end_date.isoformat() if sub.trial_end_date else None,
        # Derived server-side so the countdown cannot be computed two different ways.
        # The billing page used to read a `trial_ends_at` key that this endpoint never
        # emitted, silently fall back to renewal_date, and show the wrong number.
        "trial_days_remaining": billing_trial.days_remaining(sub),
        "is_trial": billing_trial.is_on_trial(sub),
        "trial_expired": (sub.subscription_status or "") == billing_trial.STATUS_TRIAL_EXPIRED,
        # Whether a Stripe customer exists for this organization, as a plain boolean —
        # the id itself is never sent to the browser. The billing page needs this because
        # a card can only be attached to an existing customer, and that customer is
        # created by the first plan purchase. Without it the trial banner offered "Add
        # Payment Method" to a workspace that had no billing account, and the only
        # possible outcome was a 409 telling them to choose a plan first.
        "has_billing_account": bool(sub.stripe_customer_id),
        "currency": sub.currency,
    }


# ══════════════════════════════════════════════════════════════════════════════
# /summary  — overview cards, alerts, seat metrics, current plan
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/summary")
def get_summary(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    sub = billing_service.ensure_subscription(db, _org(user))
    billing_service.refresh_seat_metrics(db, sub, _org(user))

    plan = db.query(models.BillingPlan).filter(models.BillingPlan.id == sub.plan_id).first()

    # Next-invoice estimate (proration not modelled — use plan price × seats).
    next_invoice = None
    if plan:
        seats = max(sub.active_users or 1, 1)
        per_user = (
            plan.annual_price_per_user
            if sub.billing_cycle == "annual"
            else plan.monthly_price_per_user
        )
        subtotal = round(per_user * seats, 2)
        # No tax is applied. Business decision 2026-08-12: plan prices are final,
        # tax-inclusive customer prices. Stripe Tax is not enabled and no manual rates
        # are configured, so the quoted amount IS what the customer pays.
        #
        # This replaced a hardcoded 8% that was invented in code — it matched no
        # jurisdiction and ignored the customer's address entirely. The note also
        # changed: it previously said tax would be "calculated at invoicing", which
        # implied a charge still to come. Nothing adds tax later, so saying so would
        # be a second inaccuracy in the opposite direction.
        tax = 0.0
        next_invoice = {
            "amount": round(subtotal + tax, 2),
            "subtotal": subtotal,
            "tax_amount": tax,
            "currency": sub.currency,
            "due_date": (
                (sub.renewal_date or datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            ),
            "tax_note": "Prices are final and tax-inclusive. No additional tax is applied.",
        }

    # Seat alert tier — Spec §9.
    user_limit = plan.user_limit if plan else None
    seat_alert = None
    if user_limit:
        usage_pct = (sub.active_users or 0) / user_limit
        if (sub.active_users or 0) > user_limit:
            seat_alert = {"tier": "red", "key": "seat_over_capacity"}
        elif usage_pct >= 1.0:
            seat_alert = {"tier": "amber", "key": "seat_limit_reached"}
        elif usage_pct >= 0.9:
            seat_alert = {"tier": "amber", "key": "seat_approaching_limit"}
        elif (sub.pending_invites or 0) > max(user_limit - (sub.active_users or 0), 0):
            seat_alert = {"tier": "amber", "key": "invites_exceed_seats"}

    # Billing alerts (Spec §15).
    alerts = []
    if seat_alert:
        alerts.append(seat_alert)
    if sub.subscription_status == "past_due":
        alerts.append({"tier": "red", "key": "payment_failed"})
    # Dunning / read-only state. The old red banner was the *only* consequence of a
    # failed payment; now it comes with a deadline the customer can act on.
    suspension_state = billing_suspension.status_for_org(db, sub.organization_id)
    if suspension_state.get("suspended"):
        alerts.append({"tier": "red", "key": "billing_suspended",
                       "grace_expires_at": suspension_state.get("grace_expires_at")})
    elif suspension_state.get("dunning_started_at"):
        alerts.append({"tier": "red", "key": "payment_overdue",
                       "grace_expires_at": suspension_state.get("grace_expires_at"),
                       "days_overdue": suspension_state.get("days_overdue")})
    # "Your trial is ending soon" used to be raised for the whole trial, so a customer
    # saw it on day 1 of 14 and learned to ignore it. Now it appears only inside the
    # final stretch, which is when it is both true and actionable.
    trial_left = billing_trial.days_remaining(sub)
    if billing_trial.is_on_trial(sub):
        if trial_left is not None and trial_left <= TRIAL_WARNING_DAYS:
            alerts.append({"tier": "amber", "key": "trial_ending",
                           "trial_end_date": sub.trial_end_date.isoformat()
                                             if sub.trial_end_date else None,
                           "days_remaining": trial_left})
    elif (sub.subscription_status or "") == billing_trial.STATUS_TRIAL_EXPIRED:
        alerts.append({"tier": "red", "key": "trial_expired",
                       "trial_end_date": sub.trial_end_date.isoformat()
                                         if sub.trial_end_date else None})
    tax_profile = (
        db.query(models.BillingTaxProfile)
          .filter(models.BillingTaxProfile.organization_id == sub.organization_id)
          .first()
    )
    if not tax_profile or not (tax_profile.legal_entity_name and tax_profile.country):
        alerts.append({"tier": "amber", "key": "tax_details_incomplete"})

    payment_method = (
        db.query(models.BillingPaymentMethod)
          .filter(models.BillingPaymentMethod.organization_id == sub.organization_id,
                  models.BillingPaymentMethod.is_default == True)  # noqa: E712
          .first()
    )

    # Lets the UI hide purchase/portal actions for a tenant that is never billed,
    # instead of offering buttons that would 403.
    internal_tenant = billing_service.is_internal_tenant(db, sub.organization_id)

    return {
        "subscription": _serialize_subscription(sub, plan),
        "billing_type": (billing_service.BILLING_TYPE_INTERNAL if internal_tenant
                         else billing_service.BILLING_TYPE_CUSTOMER),
        "is_internal_tenant": internal_tenant,
        "next_invoice": next_invoice,
        "seat_metrics": {
            "included": user_limit,
            "active": sub.active_users,
            "pending_invites": sub.pending_invites,
            "suspended": sub.suspended_users,
            "available": (user_limit - (sub.active_users or 0)) if user_limit else None,
        },
        "alerts": alerts,
        # Dunning / read-only state, so the UI can explain exactly what is restricted
        # and by when, rather than showing a bare "payment failed" banner.
        "billing_suspension": suspension_state,
        "payment_method": (
            {
                "id": payment_method.id,
                "brand": payment_method.brand,
                "last_four": payment_method.last_four,
                "expiry_month": payment_method.expiry_month,
                "expiry_year": payment_method.expiry_year,
                "billing_name": payment_method.billing_name,
                "verification_status": payment_method.verification_status,
            }
            if payment_method else None
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# /plans  — plan catalog
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/plans")
def list_plans(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    return {"plans": [_serialize_plan(p) for p in billing_service.list_plans(db)]}


# ══════════════════════════════════════════════════════════════════════════════
# /upgrade  — change plan (immediate, with proration when Stripe is wired)
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/upgrade")
def upgrade_plan(
    payload: UpgradeRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    _require_billable_tenant(db, _org(user))       # internal tenants are never charged
    target = billing_service.get_plan_by_code(db, payload.plan_code)
    if not target:
        raise HTTPException(404, "Plan not found")
    if target.is_enterprise:
        raise HTTPException(400, "Enterprise plans are quote-led — submit an enterprise quote request instead.")

    sub = billing_service.ensure_subscription(db, _org(user))

    # ── Seats ────────────────────────────────────────────────────────────────
    # Resolved before every other check that can reject the request, because a
    # seat count the plan cannot hold is the most useful thing to tell the
    # caller — more useful than "you are already on this plan", which is what
    # they used to get when they raised the seat count on their current tier.
    #
    #   floor    the billable headcount. Selling fewer seats than there are
    #            people would put the organization instantly over capacity.
    #   ceiling  the target plan's user_limit, from the plan catalog.
    #
    # Absent `seats`, behaviour is exactly as before: bill for who is here.
    billable_now = max(billing_service.count_billable_seats(db, _org(user)), 1)
    requested = payload.seats
    seats = max(int(requested), billable_now) if requested else billable_now

    limit = target.user_limit
    if requested and limit is not None and seats > int(limit):
        # Structured so the UI can render the plan, the limit, what was asked
        # for and which plan solves it, all from the catalog. Scoped to callers
        # that sent `seats`: `detail` on this endpoint has always been a string
        # and the billing console renders it straight into a toast, so an
        # object would put "[object Object]" in front of an admin.
        raise HTTPException(
            409,
            detail={
                "code": "PLAN_LIMIT_EXCEEDED",
                "message": (
                    f"{target.name} supports up to {limit} seats. "
                    f"You entered {seats} seats."
                ),
                **billing_service.plan_limit_options(
                    db, requested_seats=seats, current_plan=target,
                ),
            },
        )

    # Same plan, same cycle, same seats is genuinely a no-op. Same plan with a
    # DIFFERENT seat count is not — it is the quantity change the subscription
    # configurator exists to make, and rejecting it left "Update anytime" next
    # to a control that could not update anything.
    same_plan = sub.plan_id == target.id
    same_cycle = sub.billing_cycle == (payload.billing_cycle or "monthly")
    seats_unchanged = (not requested) or int(seats) == int(sub.billable_users or 0)
    if same_plan and same_cycle and seats_unchanged:
        raise HTTPException(400, "You are already on this plan and billing cycle.")

    # A move to a LOWER tier is a downgrade and must go through review — this endpoint
    # applies changes to Stripe immediately. Without this check the review table was
    # decorative: a customer could simply call /upgrade with a cheaper plan and it
    # applied instantly, taking effect before anyone saw the request. It also skipped
    # every capacity check, so an organization could land on a plan whose user_limit is
    # far below its headcount.
    current_plan = (db.query(models.BillingPlan)
                      .filter(models.BillingPlan.id == sub.plan_id).first())
    if (current_plan is not None
            and target.tier_order is not None
            and current_plan.tier_order is not None
            and target.tier_order < current_plan.tier_order):
        raise HTTPException(
            409,
            f"Moving from {current_plan.name} to {target.name} is a downgrade and "
            "cannot be applied here. Submit a downgrade request; it needs review "
            "before it takes effect.",
        )

    # Headcount capacity, for callers that did not name a seat count. Keeps its
    # original sentence-shaped 409 for the billing console.
    try:
        billing_service.assert_plan_capacity(db, _org(user), target)
    except billing_service.PlanCapacityExceeded as exc:
        raise HTTPException(409, str(exc))

    price_id_key = f"STRIPE_PRICE_{payload.plan_code.upper()}_{payload.billing_cycle.upper()}"
    price_id = os.getenv(price_id_key)

    if not price_id:
        # A missing price used to fall through to a local plan change: the org
        # was marked active on a paid tier with no Stripe subscription and no
        # charge. That is a revenue hole, not a fallback — fail closed and leave
        # the subscription exactly as it was.
        _log.error(
            "billing misconfiguration: %s is not set; refusing to upgrade org=%s to plan=%s cycle=%s",
            price_id_key, _org(user), payload.plan_code, payload.billing_cycle,
        )
        raise HTTPException(
            500,
            "Billing is not fully configured for this plan. No change has been made "
            "to your subscription. Please contact support.",
        )

    _log.info(
        "billing upgrade requested: org=%s plan=%s cycle=%s price_key=%s",
        _org(user), payload.plan_code, payload.billing_cycle, price_id_key,
    )

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")

    # `seats` was resolved above: the requested count clamped to the billable
    # floor and checked against the plan ceiling. It is never taken from the
    # client verbatim, and never decided by whichever tenant last loaded the
    # billing page.
    _log.info("billing upgrade seats: org=%s billable=%s requested=%s charged=%s",
              _org(user), billable_now, requested, seats)

    # ── Existing subscriber → modify the subscription they already have ───────
    # Opening another Checkout Session here is what produced duplicate active
    # Stripe subscriptions: the new one billed alongside the old, and the local
    # row's stripe_subscription_id was overwritten so the original was forgotten.
    # The ids come from this organization's own row — never from the client.
    if sub.stripe_subscription_id and sub.stripe_customer_id:
        try:
            billing_service.change_stripe_plan(
                db, sub,
                price_id=price_id,
                seats=seats,
                target_plan=target,
                billing_cycle=payload.billing_cycle or "monthly",
            )
        except billing_service.StripePlanChangeError as e:
            raise HTTPException(e.status_code, e.message)
        # No `url`: the frontend already treats a url-less response as an
        # in-place change (BillingModule.jsx — `if (d.url)` … else show message).
        return {
            "ok": True,
            "subscription": _serialize_subscription(sub, target),
            "message": f"You're now on {target.name} ({sub.billing_cycle}).",
        }

    # ── First purchase → Checkout Session ────────────────────────────────────
    customer_id = getattr(sub, "stripe_customer_id", None)

    # ── Guard 1: already paying, we just have not been told yet ───────────────
    # The branch above only routes to change_stripe_plan when BOTH ids are stored.
    # Between paying and checkout.session.completed being processed, the row can hold
    # a customer id but no subscription id — and in that window a second click used to
    # open a second Checkout Session. If that one is also paid, Stripe has two
    # subscriptions, the webhook keeps the first ("fill in, never replace") and the
    # second bills the card forever, invisible to the product: the hosted portal is
    # opened for the stored customer, so the customer cannot even cancel it.
    # Four such subscriptions ($1,450/month) accumulated in the sandbox this way.
    #
    # Asking Stripe closes the window authoritatively, and it is a read.
    if customer_id:
        try:
            existing = stripe.Subscription.list(customer=customer_id, status="active",
                                                limit=1)
            if (billing_webhooks._g(existing, "data") or []):
                raise HTTPException(
                    409,
                    "This organization already has an active subscription. Reload the "
                    "billing page — if it still looks wrong, contact support rather "
                    "than paying again.",
                )
        except HTTPException:
            raise
        except Exception as exc:                              # noqa: BLE001
            # A Stripe read failure must not block a legitimate first purchase. The
            # idempotency key below is the primary protection; this is belt.
            _log.warning("could not pre-check existing subscriptions for org=%s: %s",
                         _org(user), exc)

    try:
        create_kwargs: dict = dict(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': seats,
            }],
            mode='subscription',
            # A confirmation screen rather than the settings console: the
            # customer has just paid and deserves somewhere that says so before
            # being handed a page of tabs. It calls this module's own
            # /upgrade/confirm, so the authority for "was this actually paid"
            # is unchanged.
            #
            # BillingModule still handles ?session_id= on the settings page, so
            # any Checkout Session created before this shipped still confirms
            # correctly when its older success_url resolves.
            success_url=f"{frontend_url}/subscription/confirmed?session_id={{CHECKOUT_SESSION_ID}}",
            # Cancelling returns to the configurator the customer came from,
            # with their plan and seat count still on screen, rather than to a
            # settings page that has forgotten what they were doing.
            cancel_url=f"{frontend_url}/subscription/configure",
            client_reference_id=str(sub.organization_id),
            metadata={
                'plan_code': payload.plan_code,
                'billing_cycle': payload.billing_cycle or 'monthly',
            },
        )
        if customer_id:
            create_kwargs["customer"] = customer_id
        # ── Discount codes: annual plans only ────────────────────────────────
        # Offers (Black Friday 2026 first) live entirely in Stripe: a coupon plus a
        # customer-facing promotion code carry the discount size, the expiry date and
        # the "first-time order only" rule, so starting or ending an offer is a
        # Dashboard change, never a deploy. The one rule Stripe cannot express is
        # "annual plans only" (a coupon can be limited to products, and the monthly
        # and annual prices share a product), so the code box is offered only on an
        # annual checkout. Without a live promotion code the box is inert.
        if (payload.billing_cycle or "monthly") == "annual":
            create_kwargs["allow_promotion_codes"] = True
        # ── Guard 2: Stripe-side idempotency ─────────────────────────────────
        # Repeated clicks, a refresh, a second tab, or a retried POST all produce the
        # same key, so Stripe returns the SAME session instead of minting another one
        # that could be paid separately. Deliberately server-side: a disabled button is
        # not a control, it is a courtesy.
        #
        # The key is scoped to the intent (org + plan + cycle + seat count), not to the
        # request, so genuinely choosing a different plan still opens a new session.
        # Stripe retains keys for 24h, which comfortably covers a double-submit window.
        idem_key = _checkout_idempotency_key(
            sub.organization_id, payload.plan_code,
            payload.billing_cycle or "monthly", seats,
        )
        session = stripe.checkout.Session.create(**create_kwargs,
                                                 idempotency_key=idem_key)
        _log.info("billing upgrade session: org=%s session=%s idem=%s",
                  sub.organization_id, getattr(session, "id", None), idem_key)
        return {"ok": True, "url": session.url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))



# ══════════════════════════════════════════════════════════════════════════════
# /downgrade-request  — reviewed request, never instant
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/downgrade-request")
def request_downgrade(
    payload: DowngradeRequestIn,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    target = billing_service.get_plan_by_code(db, payload.requested_plan_code)
    if not target:
        raise HTTPException(404, "Requested plan not found")

    sub = billing_service.ensure_subscription(db, _org(user))
    eff_date = None
    if payload.preferred_effective_date:
        try:
            eff_date = datetime.fromisoformat(payload.preferred_effective_date)
        except Exception:
            eff_date = None

    req = models.BillingDowngradeRequest(
        organization_id        = sub.organization_id,
        current_plan_id        = sub.plan_id,
        requested_plan_id      = target.id,
        requested_by_user_id   = user.id,
        reason                 = payload.reason,
        preferred_effective_date = eff_date,
        acknowledged_risk      = payload.acknowledged_risk,
        status                 = "pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db,
        actor=user,
        action_key="downgrade_requested",
        summary=f"Downgrade requested to {target.name}",
        details={"requested_plan": target.code, "reason": payload.reason[:200]},
        ip_address=ip,
        user_agent_hash=ua_hash,
    )

    return {
        "ok": True,
        "request_id": req.id,
        # No SLA is promised here. The previous text said "a billing specialist will
        # review it within one business day", but nothing consumes this table:
        # BillingDowngradeRequest is written here and read by no code, no job and no
        # admin screen. 21 production requests sat unseen for 2.5 months while the API
        # kept promising next-day review. Until a review workflow exists, the response
        # states only what actually happened — the request was recorded.
        "message": ("Your downgrade request has been recorded. Downgrades are not "
                    "automatic — please contact your account manager to have it "
                    "actioned."),
    }


# ══════════════════════════════════════════════════════════════════════════════
# /enterprise-quote  — quote-led, creates internal record + audit
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/enterprise-quote")
def submit_enterprise_quote(
    payload: EnterpriseQuoteIn,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    sub = billing_service.ensure_subscription(db, _org(user))

    quote = models.BillingEnterpriseQuote(
        organization_id          = sub.organization_id,
        company_name             = payload.company_name,
        work_email               = payload.work_email,
        user_count               = payload.user_count,
        deployment_preference    = payload.deployment_preference,
        required_integrations    = payload.required_integrations,
        compliance_requirements  = payload.compliance_requirements,
        notes                    = payload.notes,
        requested_by_user_id     = user.id,
        status                   = "submitted",
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db,
        actor=user,
        action_key="enterprise_quote_requested",
        summary=f"Enterprise quote requested for {payload.company_name}",
        details={
            "company_name": payload.company_name,
            "deployment": payload.deployment_preference,
            "user_count": payload.user_count,
        },
        ip_address=ip,
        user_agent_hash=ua_hash,
    )

    return {
        "ok": True,
        "quote_id": quote.id,
        "submitted_at": quote.created_at.isoformat() if quote.created_at else None,
        "message": "We've received your request and will respond within one business day.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# /invoices  — list + filters + detail + PDF + pay
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/invoices")
def list_invoices(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    status_: Optional[str] = Query(None, alias="status"),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
):
    _require_view(user)
    sub = billing_service.ensure_subscription(db, _org(user))

    q = db.query(models.BillingInvoice).filter(
        models.BillingInvoice.organization_id == sub.organization_id
    )
    if status_:
        q = q.filter(models.BillingInvoice.status == status_)
    if search:
        q = q.filter(models.BillingInvoice.invoice_number.ilike(f"%{search}%"))
    if start_date:
        try:
            q = q.filter(models.BillingInvoice.billing_period_start >= datetime.fromisoformat(start_date))
        except Exception:
            pass
    if end_date:
        try:
            q = q.filter(models.BillingInvoice.billing_period_end <= datetime.fromisoformat(end_date))
        except Exception:
            pass
    rows = q.order_by(models.BillingInvoice.billing_period_start.desc()).all()

    return {
        "invoices": [
            {
                "id": r.id,
                "invoice_number": r.invoice_number,
                "billing_period_start": r.billing_period_start.isoformat() if r.billing_period_start else None,
                "billing_period_end": r.billing_period_end.isoformat() if r.billing_period_end else None,
                "subtotal": r.subtotal,
                "tax_amount": r.tax_amount,
                "discount_amount": r.discount_amount,
                "total_amount": r.total_amount,
                "currency": r.currency,
                "status": r.status,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "paid_date": r.paid_date.isoformat() if r.paid_date else None,
                # Lets the UI tell a real Stripe invoice from a legacy fabricated row,
                # and only offer a PDF where one genuinely exists.
                "source": "stripe" if r.stripe_invoice_id else "legacy",
                "has_pdf": bool(r.pdf_url),
            }
            for r in rows
        ]
    }


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    inv = (
        db.query(models.BillingInvoice)
          .filter(models.BillingInvoice.id == invoice_id,
                  models.BillingInvoice.organization_id == _org(user))   # §5 tenant scope
          .first()
    )
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "billing_period_start": inv.billing_period_start.isoformat() if inv.billing_period_start else None,
        "billing_period_end": inv.billing_period_end.isoformat() if inv.billing_period_end else None,
        "subtotal": inv.subtotal,
        "tax_amount": inv.tax_amount,
        "discount_amount": inv.discount_amount,
        "total_amount": inv.total_amount,
        "currency": inv.currency,
        "status": inv.status,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "paid_date": inv.paid_date.isoformat() if inv.paid_date else None,
    }


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    inv = (
        db.query(models.BillingInvoice)
          .filter(models.BillingInvoice.id == invoice_id,
                  models.BillingInvoice.organization_id == _org(user))   # §5 tenant scope
          .first()
    )
    if not inv:
        raise HTTPException(404, "Invoice not found")

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="invoice_action",
        summary=f"Invoice {inv.invoice_number} downloaded",
        details={"invoice_id": inv.id, "action": "pdf_download"},
        ip_address=ip, user_agent_hash=ua_hash,
    )

    # A Stripe-issued invoice has a real, Stripe-hosted PDF. Stripe's copy is the
    # authoritative record, correctly formatted and carrying the issuer details a real
    # invoice needs, so we hand back its URL instead of fabricating a document.
    #
    # Returned as JSON rather than a 307 deliberately. A redirect would be followed by
    # the browser's fetch(), which would (a) forward this request's Authorization
    # header to Stripe, and (b) fail CORS anyway since Stripe serves no CORS headers
    # for invoice PDFs. The client opens the URL in a new tab instead.
    if inv.pdf_url:
        return {"ok": True, "url": inv.pdf_url, "invoice_number": inv.invoice_number}

    # Legacy rows predate invoice mirroring (stripe_invoice_id IS NULL) — the
    # fabricated production invoices. They have no Stripe document, and inventing a
    # PDF for them would dress up a number Stripe never charged. Say so plainly.
    if not inv.stripe_invoice_id:
        raise HTTPException(
            409,
            "This invoice predates Stripe invoicing and has no downloadable document. "
            "Please contact support for a copy.",
        )

    # Mirrored from Stripe but the PDF is not ready yet (a draft has none until it is
    # finalised). Not an error worth a 500.
    raise HTTPException(
        409,
        "This invoice has not been finalised by Stripe yet, so no PDF is available. "
        "Please try again shortly.",
    )


@router.post("/invoices/{invoice_id}/pay")
def pay_invoice(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """RETIRED — always 410 Gone.

    This endpoint used to mark an invoice ``paid`` and stamp ``paid_date`` while
    making *zero* Stripe calls: no PaymentIntent, no charge, no settlement. Any
    caller with billing-manage rights could clear a real receivable for free,
    and the resulting row was indistinguishable from a genuinely settled
    invoice — corrupting revenue records rather than collecting revenue.

    It is disabled rather than deleted so existing clients get an explicit,
    diagnosable failure instead of a 404 that reads as "wrong URL". Real
    collection needs a Stripe PaymentIntent / hosted invoice payment flow, which
    is not in scope here (see Phase 2.4).

    No database read or write happens: nothing is looked up, so no invoice can be
    mutated and no cross-tenant existence can be probed through this route.
    """
    raise HTTPException(
        410,
        "In-app invoice payment has been disabled. This endpoint previously "
        "marked invoices as paid without taking payment. Pay via the Stripe "
        "billing portal, or contact support.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# /payment-method  — list + create/update + delete + setup session stub
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/payment-method")
def list_payment_methods(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    sub = billing_service.ensure_subscription(db, _org(user))
    rows = (
        db.query(models.BillingPaymentMethod)
          .filter(models.BillingPaymentMethod.organization_id == sub.organization_id)
          .order_by(models.BillingPaymentMethod.is_default.desc(),
                    models.BillingPaymentMethod.created_at.desc())
          .all()
    )
    return {
        "payment_methods": [
            {
                "id": r.id,
                "brand": r.brand,
                "last_four": r.last_four,
                "expiry_month": r.expiry_month,
                "expiry_year": r.expiry_year,
                "billing_name": r.billing_name,
                "billing_address": r.billing_address_json,
                "is_default": r.is_default,
                "verification_status": r.verification_status,
                "payment_type": r.payment_type,
                # "stripe" = mirrored from a real card Stripe holds and can charge.
                # "legacy" = a hand-entered row from the retired manual form; it has
                # no Stripe reference and cannot be charged.
                "source": "stripe" if r.stripe_payment_method_id else "legacy",
            }
            for r in rows
        ]
    }


@router.post("/payment-method")
def upsert_payment_method(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """RETIRED — always 410 Gone.

    This accepted a card brand chosen from a dropdown, four digits typed by hand and
    an expiry date, then stored the row with ``provider="stripe"`` and
    ``verification_status="verified"``. Stripe knew nothing about it, nothing was
    verified, and the resulting "payment method" could not be charged — while the UI
    told the customer their card was tokenized through Stripe.

    A payment method Temveris has never seen is not a payment method. Cards are now
    collected by Stripe's hosted Setup Session (POST /payment-method/session) and
    mirrored from Stripe (app/billing/payment_methods.py), so the only card that can
    appear is one Stripe actually holds.

    Disabled rather than deleted so an old client gets a diagnosable failure instead
    of a 404 that reads as "wrong URL". No database read or write happens here.
    """
    raise HTTPException(
        410,
        "Manually entering card details has been disabled — it created a record "
        "Stripe could not charge. Use 'Add payment method' to enter your card "
        "securely on Stripe's hosted page.",
    )


@router.delete("/payment-method/{pm_id}")
def delete_payment_method(
    pm_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    org_id = _require_billable_tenant(db, _org(user))
    pm = (
        db.query(models.BillingPaymentMethod)
          .filter(models.BillingPaymentMethod.id == pm_id,
                  models.BillingPaymentMethod.organization_id == org_id)   # §5 tenant scope
          .first()
    )
    if not pm:
        raise HTTPException(404, "Payment method not found")

    # A live subscription must always keep a way to pay. Removing the last card would
    # not "cancel" anything — it would silently guarantee the next renewal fails and
    # push the tenant into dunning with no way to recover.
    if (billing_payment_methods.subscription_needs_a_card(db, org_id)
            and billing_payment_methods.countable_cards(db, org_id, exclude_id=pm.id) == 0):
        raise HTTPException(
            409,
            "This is the only payment method on an active subscription. Add a "
            "replacement card first, then remove this one.",
        )

    brand, last_four = pm.brand, pm.last_four
    stripe_pm_id = pm.stripe_payment_method_id

    # Detach at Stripe FIRST. Deleting the local row while the card stays attached
    # would tell the customer it was removed while Stripe kept charging it.
    if stripe_pm_id:
        try:
            stripe.PaymentMethod.detach(stripe_pm_id)
        except Exception as e:
            _log.error("payment method detach failed org=%s pm=%s: %s",
                       org_id, stripe_pm_id, e)
            raise HTTPException(
                502,
                "We could not remove this card at our payment provider, so it has "
                "not been removed. Please try again shortly.",
            )

    db.delete(pm)
    db.commit()

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="payment_method_changed",
        summary=f"Payment method removed: {brand} ****{last_four}",
        details={"payment_method_id": pm_id, "action": "removed",
                 "stripe_payment_method_id": stripe_pm_id},
        ip_address=ip, user_agent_hash=ua_hash,
    )
    return {"ok": True}


@router.post("/payment-method/session")
def create_setup_session(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Open Stripe's hosted Setup Session so the customer enters their card there.

    This is the only way a card can now be registered. Card number and CVC go
    straight to Stripe and never reach Temveris; we later mirror the resulting
    ``pm_...`` reference.

    Two defects fixed here:

    * ``customer=`` was not passed, so completing the session made Stripe create a
      *new* customer — orphaned from the one this organization's subscription already
      bills against, leaving the card attached to the wrong customer.
    * every failure was swallowed and returned as ``{"ok": True, "session_url": None}``
      with a message telling the admin to use the in-app form. A failure reported as
      success, pointing at the fake flow.
    """
    _require_manage(user)
    # mode='setup' takes no payment, but it does open a Stripe Checkout Session and
    # attaches a card to a Stripe customer — an internal tenant has no business
    # having either.
    org_id = _require_billable_tenant(db, _org(user))
    sub = billing_service.ensure_subscription(db, org_id)

    # A card must attach to the customer this org is already billed against. Without an
    # existing customer there is nothing to attach to — the customer is created by
    # first purchase (Checkout), not by adding a card.
    if not sub.stripe_customer_id:
        raise HTTPException(
            409,
            "This organization does not have a billing account yet. Choose a plan "
            "first; a payment method can be added once the subscription exists.",
        )

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="setup",
            customer=sub.stripe_customer_id,          # attach to the EXISTING customer
            success_url=f"{frontend_url}/settings/billing-subscription?setup_intent={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{frontend_url}/settings/billing-subscription",
            client_reference_id=str(org_id),
            metadata={"organization_id": str(org_id), "purpose": "payment_method_setup"},
        )
    except Exception as e:
        _log.error("setup session creation failed for org=%s: %s", org_id, e)
        raise HTTPException(
            502,
            "We could not start the secure card setup with our payment provider. "
            "No card has been saved. Please try again shortly.",
        )
    return {"ok": True, "session_url": session.url}


@router.get("/payment-method/confirm")
def confirm_payment_method_setup(
    setup_intent: str,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Called after Stripe redirects back from the hosted Setup Session.

    ``setup_intent`` is the Checkout Session id (that is what Stripe substitutes into
    ``{CHECKOUT_SESSION_ID}``). Its ownership is verified against this tenant before
    anything is mirrored — the same protection ``/upgrade/confirm`` applies, because a
    session id in a URL is not proof it belongs to the caller.

    No new webhook event type is introduced (Phase 3 K3): this endpoint covers in-app
    additions and the hourly sweep covers cards added directly in the Stripe portal.
    """
    _require_manage(user)
    org_id = _require_billable_tenant(db, _org(user))
    sub = billing_service.ensure_subscription(db, org_id)

    try:
        session = stripe.checkout.Session.retrieve(setup_intent)
    except Exception as e:
        _log.error("setup confirm: session retrieve failed org=%s id=%s: %s",
                   org_id, setup_intent, e)
        raise HTTPException(400, "We could not verify that card setup with Stripe.")

    # ── Tenant ownership, checked two independent ways ────────────────────────
    session_org = getattr(session, "client_reference_id", None)
    try:
        session_org_id = int(session_org) if session_org is not None else None
    except (TypeError, ValueError):
        session_org_id = None
    if session_org_id != org_id:
        raise HTTPException(403, "This card setup belongs to another organization.")

    session_customer = getattr(session, "customer", None)
    if session_customer and session_customer != sub.stripe_customer_id:
        _log.error("setup confirm: customer mismatch org=%s stored=%s session=%s",
                   org_id, sub.stripe_customer_id, session_customer)
        raise HTTPException(403, "This card setup belongs to another billing account.")

    status = getattr(session, "status", None)
    if status != "complete":
        raise HTTPException(
            400,
            f"Card setup was not completed (status={status or 'unknown'}). "
            "No payment method has been saved.",
        )

    result = billing_payment_methods.sync_org_payment_methods(db, org_id)
    if result.get("status") != "synced":
        raise HTTPException(
            502,
            "Your card was saved with our payment provider but we could not refresh "
            "it here yet. It will appear shortly.",
        )

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="payment_method_changed",
        summary="Payment method added via Stripe hosted setup",
        details={"action": "added", "setup_session": setup_intent,
                 "created": result.get("created"), "updated": result.get("updated")},
        ip_address=ip, user_agent_hash=ua_hash,
    )
    return {"ok": True, "synced": result}

@router.post("/customer-portal")
def create_customer_portal_session(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    # The hosted portal lets the customer change plan, cancel, and add cards —
    # real Stripe writes that happen entirely outside our guards. It must be
    # unreachable for an internal tenant, including one that still holds a
    # stripe_customer_id from before it was exempted.
    _require_billable_tenant(db, _org(user))
    sub = billing_service.ensure_subscription(db, _org(user))
    
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    
    # You must have a stripe_customer_id saved in your DB to use the customer portal.
    # For now, we mock the requirement or throw an error if missing.
    customer_id = getattr(sub, "stripe_customer_id", None)
    if not customer_id:
        raise HTTPException(400, "No Stripe customer linked to this account yet.")
        
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{frontend_url}/settings/billing-subscription",
        )
        return {"ok": True, "url": session.url}
    except Exception as e:
        raise HTTPException(400, str(e))

@router.get("/upgrade/confirm")
def confirm_upgrade_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Called by the frontend after Stripe redirects back. Synchronously confirms
    the session and updates the subscription so the UI reflects the new plan
    immediately without waiting for the webhook."""
    _require_manage(user)
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        raise HTTPException(400, str(e))

    # Stripe v10 uses attribute access, not .get()
    payment_status = getattr(session, "payment_status", None)
    status = getattr(session, "status", None)
    # A Checkout Session is only a successful *payment* when payment_status says
    # so. session.status == "complete" merely means the customer finished the
    # flow — it is "complete" even when payment_status == "unpaid" (e.g. an
    # asynchronous or failed payment method), so it must NOT be accepted as an
    # alternative proof of payment.
    if payment_status not in ("paid", "no_payment_required"):
        raise HTTPException(
            400,
            f"Payment not completed (payment_status={payment_status or 'unknown'}). "
            "Your plan has not been changed.",
        )

    org_id = getattr(session, "client_reference_id", None)
    raw_meta = getattr(session, "metadata", None)
    plan_code = getattr(raw_meta, "plan_code", None) if raw_meta else None
    billing_cycle = (getattr(raw_meta, "billing_cycle", None) or "monthly") if raw_meta else "monthly"
    stripe_sub_id = getattr(session, "subscription", None)
    stripe_customer_id = getattr(session, "customer", None)

    print(f"[billing/confirm] session_id={session_id} payment_status={payment_status} session_status={status} org_id={org_id} plan_code={plan_code} billing_cycle={billing_cycle}", flush=True)

    if not org_id:
        raise HTTPException(400, "Missing organization reference")

    # client_reference_id arrives as a string; organization_id is an integer FK.
    try:
        session_org_id = int(org_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid organization reference on checkout session")

    # §5 — a caller may only confirm a checkout belonging to their own tenant.
    # Without this, any billing admin could paste another org's session_id and
    # mutate that org's subscription.
    if session_org_id != _org(user):
        raise HTTPException(403, "This checkout session belongs to another organization.")

    sub = db.query(models.BillingSubscription).filter(
        models.BillingSubscription.organization_id == session_org_id
    ).first()
    if not sub:
        raise HTTPException(404, "Subscription not found")

    # Only ever *fill in* the subscription id, never replace a different one.
    # Overwriting is how a duplicate subscription used to become the record of
    # truth while the original kept billing unnoticed. Full webhook idempotency
    # is Phase 3; this is the minimum protection.
    if stripe_sub_id and not sub.stripe_subscription_id:
        sub.stripe_subscription_id = stripe_sub_id
    elif stripe_sub_id and stripe_sub_id != sub.stripe_subscription_id:
        _log.error(
            "refusing to overwrite subscription id for org=%s: stored=%s incoming=%s "
            "(a second Stripe subscription may exist and need cancelling)",
            sub.organization_id, sub.stripe_subscription_id, stripe_sub_id,
        )
    if stripe_customer_id and not sub.stripe_customer_id:
        sub.stripe_customer_id = stripe_customer_id
    if plan_code:
        target_plan = db.query(models.BillingPlan).filter(
            models.BillingPlan.code == plan_code
        ).first()
        if target_plan:
            sub.plan_id = target_plan.id
    sub.billing_cycle = billing_cycle
    sub.subscription_status = "active"

    # ── Renewal date: Stripe is the source of truth ───────────────────────────
    # This used to be `now + 365/30`, invented locally. Stripe's actual
    # current_period_end is the date the customer is really charged on, and a month is
    # not 30 days — a February purchase drifted by two days, and every month by one to
    # three. The billing page showed one date while Stripe billed on another.
    #
    # Read via the same helper the webhook layer uses (billing_webhooks
    # .current_period_end), so the two paths that can activate a subscription cannot
    # disagree about when it renews.
    #
    # The local estimate survives only as an explicitly provisional fallback for when
    # Stripe cannot be reached: showing a rough date beats showing none, but it must
    # never silently outrank Stripe's.
    authoritative_renewal = None
    if sub.stripe_subscription_id:
        try:
            remote = stripe.Subscription.retrieve(sub.stripe_subscription_id)
            authoritative_renewal = billing_webhooks.current_period_end(remote)
        except Exception as exc:                                  # noqa: BLE001
            _log.warning("could not read current_period_end for org=%s (%s); using a "
                         "provisional renewal date until the webhook corrects it",
                         sub.organization_id, type(exc).__name__)
    if authoritative_renewal is not None:
        sub.renewal_date = authoritative_renewal
    else:
        sub.renewal_date = datetime.now(timezone.utc) + timedelta(
            days=365 if billing_cycle == "annual" else 30
        )
    # Project onto the commercial row before the commit, so the entitlement
    # state the resolver reads lands in the same transaction as the payment
    # confirmation. This path and the webhook can race; both derive their values
    # from the same authoritative Stripe objects and the projection is a
    # set-to-value, so whichever lands second writes the same result.
    billing_webhooks.notify_state_applied(
        db, sub, plan_code=plan_code, source="upgrade_confirm")

    db.commit()
    db.refresh(sub)

    plan = db.query(models.BillingPlan).filter(models.BillingPlan.id == sub.plan_id).first()
    return {"ok": True, "subscription": _serialize_subscription(sub, plan)}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe webhook entry point.

    Two responsibilities only: authenticate the request, then hand the event to
    ``app.billing.webhooks`` for idempotent processing. The business logic used to
    be inlined here, unclaimed and unaudited, so every Stripe re-delivery
    re-executed it — see that module for the claim protocol.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    # Fail closed, explicitly. Passing None to construct_event raises
    # AttributeError inside the SDK, which the generic handler below then reports
    # as a payload problem — so a missing secret looked identical to an attack in
    # the logs while every genuine event was rejected.
    if not webhook_secret:
        _log.error("STRIPE_WEBHOOK_SECRET is not configured; refusing all webhooks")
        raise HTTPException(status_code=400, detail="Webhook secret is not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        # Also covers a valid signature outside the 300s replay tolerance.
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except Exception as e:
        _log.error("webhook could not be parsed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    result = billing_webhooks.process_event(db, event)

    # A retryable failure returns 5xx so Stripe re-delivers; the ledger's attempts
    # counter caps how many times we will actually process it (K5). Everything
    # else is acknowledged: a duplicate is a no-op by design, and a rejected event
    # (unpaid session, tenancy violation) will not become valid on re-delivery, so
    # asking Stripe to retry it would only bury the record.
    if result.get("status") == "failed":
        raise HTTPException(status_code=500, detail="Webhook processing failed")

    return {"status": "success", "result": result}


# ══════════════════════════════════════════════════════════════════════════════
# /contacts
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/contacts")
def list_contacts(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    sub = billing_service.ensure_subscription(db, _org(user))
    rows = (
        db.query(models.BillingContact)
          .filter(models.BillingContact.organization_id == sub.organization_id)
          .order_by(models.BillingContact.is_primary.desc(),
                    models.BillingContact.created_at.asc())
          .all()
    )
    return {
        "contacts": [
            {
                "id": r.id,
                "name": r.name,
                "email": r.email,
                "role": r.role,
                "receives_invoices": r.receives_invoices,
                "receives_payment_alerts": r.receives_payment_alerts,
                "receives_renewal_notices": r.receives_renewal_notices,
                "receives_tax_documents": r.receives_tax_documents,
                "is_primary": r.is_primary,
            }
            for r in rows
        ]
    }


@router.post("/contacts")
def create_contact(
    payload: BillingContactIn,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    sub = billing_service.ensure_subscription(db, _org(user))
    if payload.is_primary:
        for existing in db.query(models.BillingContact).filter(
            models.BillingContact.organization_id == sub.organization_id,
            models.BillingContact.is_primary == True,  # noqa: E712
        ).all():
            existing.is_primary = False
    c = models.BillingContact(
        organization_id          = sub.organization_id,
        name                     = payload.name,
        email                    = payload.email,
        role                     = payload.role or "Billing Admin",
        receives_invoices        = payload.receives_invoices,
        receives_payment_alerts  = payload.receives_payment_alerts,
        receives_renewal_notices = payload.receives_renewal_notices,
        receives_tax_documents   = payload.receives_tax_documents,
        is_primary               = payload.is_primary,
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="billing_contact_changed",
        summary=f"Contact added: {c.email}",
        details={"contact_id": c.id, "action": "created", "email": c.email},
        ip_address=ip, user_agent_hash=ua_hash,
    )
    return {"ok": True, "id": c.id}


@router.patch("/contacts/{contact_id}")
def update_contact(
    contact_id: int,
    payload: BillingContactPatch,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    c = (
        db.query(models.BillingContact)
          .filter(models.BillingContact.id == contact_id,
                  models.BillingContact.organization_id == _org(user))   # §5 tenant scope
          .first()
    )
    if not c:
        raise HTTPException(404, "Contact not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("is_primary"):
        for existing in db.query(models.BillingContact).filter(
            models.BillingContact.organization_id == c.organization_id,
            models.BillingContact.is_primary == True,  # noqa: E712
            models.BillingContact.id != c.id,
        ).all():
            existing.is_primary = False
    for k, v in data.items():
        setattr(c, k, v)
    db.commit()

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="billing_contact_changed",
        summary=f"Contact updated: {c.email}",
        details={"contact_id": c.id, "action": "updated", "fields": list(data.keys())},
        ip_address=ip, user_agent_hash=ua_hash,
    )
    return {"ok": True}


@router.delete("/contacts/{contact_id}")
def delete_contact(
    contact_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    c = (
        db.query(models.BillingContact)
          .filter(models.BillingContact.id == contact_id,
                  models.BillingContact.organization_id == _org(user))   # §5 tenant scope
          .first()
    )
    if not c:
        raise HTTPException(404, "Contact not found")
    if c.is_primary:
        # Spec §12 — can't remove the primary unless another primary is named.
        other_primary = (
            db.query(models.BillingContact)
              .filter(models.BillingContact.organization_id == c.organization_id,
                      models.BillingContact.is_primary == True,  # noqa: E712
                      models.BillingContact.id != c.id)
              .first()
        )
        if not other_primary:
            raise HTTPException(400, "Designate another primary contact before removing this one.")

    email = c.email
    db.delete(c)
    db.commit()

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="billing_contact_changed",
        summary=f"Contact removed: {email}",
        details={"contact_id": contact_id, "action": "deleted"},
        ip_address=ip, user_agent_hash=ua_hash,
    )
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# /tax-profile
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/tax-profile")
def get_tax_profile(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    sub = billing_service.ensure_subscription(db, _org(user))
    p = (
        db.query(models.BillingTaxProfile)
          .filter(models.BillingTaxProfile.organization_id == sub.organization_id)
          .first()
    )
    if not p:
        return {"profile": None}
    return {
        "profile": {
            "legal_entity_name": p.legal_entity_name,
            "trading_name": p.trading_name,
            "billing_address": p.billing_address,
            "city": p.city,
            "state": p.state,
            "postal_code": p.postal_code,
            "country": p.country,
            "tax_id_label": p.tax_id_label,
            "tax_id_value": p.tax_id_value,
            "tax_exempt": p.tax_exempt,
            "exempt_certificate_url": p.exempt_certificate_url,
            "invoice_currency": p.invoice_currency,
        }
    }


@router.patch("/tax-profile")
def update_tax_profile(
    payload: TaxProfileIn,
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_manage(user)
    sub = billing_service.ensure_subscription(db, _org(user))
    p = (
        db.query(models.BillingTaxProfile)
          .filter(models.BillingTaxProfile.organization_id == sub.organization_id)
          .first()
    )
    if not p:
        p = models.BillingTaxProfile(organization_id=sub.organization_id)
        db.add(p)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="tax_profile_updated",
        summary="Tax details updated",
        details={"fields": list(data.keys()), "country": p.country},
        ip_address=ip, user_agent_hash=ua_hash,
    )
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# /audit
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/audit")
def list_audit(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    limit: int = Query(100, le=500),
    offset: int = 0,
    action: Optional[str] = None,
):
    _require_view(user)
    q = db.query(models.BillingAuditEvent)
    if action:
        q = q.filter(models.BillingAuditEvent.action_key == action)
    rows = q.order_by(models.BillingAuditEvent.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "events": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "actor_name": r.actor_name,
                "actor_email": r.actor_email,
                "action_key": r.action_key,
                "summary": r.summary,
                "details": r.details_json,
                "ip_address": r.ip_address,
            }
            for r in rows
        ]
    }


@router.get("/audit/export")
def export_audit_csv(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    rows = (
        db.query(models.BillingAuditEvent)
          .order_by(models.BillingAuditEvent.created_at.desc())
          .limit(5000)
          .all()
    )
    lines = ["timestamp,actor,action,summary,ip"]
    for r in rows:
        ts = r.created_at.isoformat() if r.created_at else ""
        actor = (r.actor_email or "").replace(",", " ")
        action = (r.action_key or "").replace(",", " ")
        summary = (r.summary or "").replace('"', "'").replace("\n", " ").replace(",", ";")
        ip = r.ip_address or ""
        lines.append(f'{ts},{actor},{action},"{summary}",{ip}')
    body = "\n".join(lines).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="billing-audit.csv"'},
    )


@router.get("/statement")
def download_statement(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_view(user)
    sub = billing_service.ensure_subscription(db, _org(user))
    invoices = (
        db.query(models.BillingInvoice)
          .filter(models.BillingInvoice.organization_id == sub.organization_id)
          .order_by(models.BillingInvoice.billing_period_start.desc())
          .all()
    )
    lines = ["invoice_number,period_start,period_end,subtotal,tax,total,currency,status"]
    for r in invoices:
        lines.append(",".join([
            r.invoice_number,
            r.billing_period_start.isoformat() if r.billing_period_start else "",
            r.billing_period_end.isoformat() if r.billing_period_end else "",
            f"{r.subtotal:.2f}",
            f"{r.tax_amount:.2f}",
            f"{r.total_amount:.2f}",
            r.currency,
            r.status,
        ]))
    body = "\n".join(lines).encode("utf-8")

    ip, ua_hash = _client_meta(request)
    billing_service.write_audit(
        db, actor=user, action_key="statement_downloaded",
        summary="Account statement downloaded",
        details={"invoice_count": len(invoices)},
        ip_address=ip, user_agent_hash=ua_hash,
    )
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="billing-statement.csv"'},
    )
