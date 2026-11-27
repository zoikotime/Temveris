"""POST /api/v1/billing/checkout-session (§14.4).

Creates a checkout session for the authenticated user's tenant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user
from app.commercial import ids, constants
from app.commercial.billing import checkout as billing_checkout
from app.commercial.billing import quotes as billing_quotes
from app.commercial.models import CommercialOrganization, PriceQuote
from app.commercial.schemas import (
    CheckoutRequest, CheckoutResponse, QuoteRequest, QuoteResponse,
    SignupOfferResponse,
)
from app.rate_limit import limiter


_log = logging.getLogger("zoiko.commercial.routers.checkout")
router = APIRouter(prefix="/api/v1/billing", tags=["Commercial v1 Billing"])


def _quote_out(q: PriceQuote) -> QuoteResponse:
    return QuoteResponse(
        quote_id=q.id,
        plan=q.plan_code,
        billing_cycle=q.billing_cycle,
        seat_count=q.seat_count,
        currency=q.currency,
        line_items=q.line_items or [],
        subtotal_cents=q.subtotal_cents,
        discount_cents=q.discount_cents,
        tax_cents=q.tax_cents,
        tax_state=q.tax_state,
        total_cents=q.total_cents,
        renewal_amount_cents=q.renewal_amount_cents,
        renewal_at=q.renewal_at.isoformat() if q.renewal_at else None,
        state=q.state,
        expires_at=q.expires_at.isoformat() if q.expires_at else None,
    )


@router.post("/quote", response_model=QuoteResponse)
def create_quote(
    payload: QuoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Server-authoritative price quote (§D-05). The frontend renders this
    and never computes totals/tax/discount/FX itself. Any seat, cadence,
    currency or location change requests a fresh quote."""
    try:
        q = billing_quotes.create_quote(
            db,
            plan_code=payload.plan,
            billing_cycle=payload.billing_cycle,
            seat_count=payload.seat_count,
            currency=payload.currency,
            organization_id=payload.organization_id,
            workspace_id=payload.workspace_id,
            country=payload.country,
            promotion_code=payload.promotion_code,
            correlation_id=ids.correlation_id(),
        )
    except billing_quotes.QuoteError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _quote_out(q)


# Registered BEFORE `/quote/{quote_id}` deliberately: FastAPI matches routes in
# declaration order, so the literal path has to come first or "preview" would be
# read as a quote id. (Quote ids are `qot_<32 hex>`, so no real id can collide.)
@router.get("/quote/preview", response_model=SignupOfferResponse)
@limiter.limit(constants.PUBLIC_QUOTE_PREVIEW_RATE_LIMIT)
def preview_offer(
    request: Request,
    plan: str = constants.DEFAULT_SELF_SERVE_PLAN,
    billing_cycle: str = "annual",
    db: Session = Depends(get_db),
):
    """The trial offer shown on the public signup page.

    Unauthenticated by necessity — it renders on the Account step, before an
    account exists. Three properties make that safe:

    * **No writes.** Unlike ``POST /quote`` this persists no ``PriceQuote`` row,
      so an anonymous page view cannot grow the quote table. Nothing here is
      referenced at checkout; a real quote is minted, authenticated, when the
      customer actually converts.
    * **No tenant data.** The response is plan configuration and the trial
      clock. It leaks nothing about any organization or user.
    * **Same math as the paid path.** Price and discount come from
      ``quotes.compute_amounts`` and the trial length from
      ``app.billing.trial`` — the modules checkout and the trial sweep already
      use — so the summary cannot drift from what is charged later.

    Seat count is fixed at 1: the panel quotes a per-worker rate, and seats are
    not known until the workspace has people in it.
    """
    from app.billing import service as billing_service
    from app.billing import trial as billing_trial

    plan = (plan or "").strip().lower() or constants.DEFAULT_SELF_SERVE_PLAN
    if plan not in constants.SELF_SERVE_PLANS:
        raise HTTPException(400, "Unknown plan")
    billing_cycle = (billing_cycle or "").strip().lower() or "annual"

    try:
        amounts = billing_quotes.compute_amounts(plan, billing_cycle, 1)
    except billing_quotes.QuoteError as e:
        raise HTTPException(400, str(e))

    # Display name from the plan catalog so there is one place a plan is named.
    # Guarded because this is the first thing a visitor loads: an unseeded
    # catalog, or a database the migration has not reached yet, must degrade to
    # the plan code rather than break the signup page. Pricing does not depend
    # on this lookup.
    try:
        plan_row = billing_service.get_plan_by_code(db, plan)
        plan_name = plan_row.name if plan_row is not None else plan.title()
    except Exception:
        _log.warning("Plan catalog unavailable for offer preview; using %r", plan)
        db.rollback()
        plan_name = plan.title()

    trial_days = billing_trial.TRIAL_LENGTH_DAYS
    trial_ends = datetime.now(timezone.utc) + timedelta(days=trial_days)

    return SignupOfferResponse(
        plan=plan,
        plan_label=f"Temveris {plan_name}",
        currency="USD",
        unit_price_monthly_cents=amounts["monthly_unit_cents"],
        effective_unit_price_monthly_cents=amounts["effective_monthly_unit_cents"],
        billing_cycle=billing_cycle,
        annual_discount_pct=constants.ANNUAL_DISCOUNT_PCT,
        trial_available=billing_service.trial_enabled_for(db, plan),
        trial_length_days=trial_days,
        trial_ends_at=trial_ends.isoformat(),
        # Zero regardless of whether a card is collected: a trial is never
        # charged on the day it starts. Conversion goes through checkout, which
        # consumes a real quote and needs its own authorisation.
        amount_due_today_cents=0,
        card_required=constants.CREDIT_CARD_REQUIRED_FOR_TRIAL,
        terms_version=constants.TERMS_VERSION_CURRENT,
        privacy_version=constants.PRIVACY_VERSION_CURRENT,
    )


@router.get("/quote/{quote_id}", response_model=QuoteResponse)
def get_quote(
    quote_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-fetch a quote (refresh check). Returns the current state incl.
    `expired` so the UI can require review before payment."""
    q = db.query(PriceQuote).filter_by(id=quote_id).one_or_none()
    if q is None:
        raise HTTPException(404, "Quote not found")
    try:
        billing_quotes.get_valid_quote(db, quote_id)
    except billing_quotes.QuoteError:
        db.commit()  # persist the expired-state flip
        return _quote_out(q)
    return _quote_out(q)


@router.post("/checkout-session", response_model=CheckoutResponse)
def create_checkout_session(
    payload: CheckoutRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate plan.
    if payload.plan not in constants.CANONICAL_PRICES_MONTHLY:
        raise HTTPException(400, f"Unknown plan: {payload.plan}")
    if constants.CANONICAL_PRICES_MONTHLY[payload.plan] is None:
        raise HTTPException(400, f"{payload.plan} requires sales-led signup")

    # Verify the user owns / is admin of the organization.
    org = db.query(CommercialOrganization).filter_by(id=payload.organization_id).one_or_none()
    if org is None:
        raise HTTPException(404, "Organization not found")
    if org.owner_user_id != current_user.id and current_user.role not in {"admin", "owner"}:
        raise HTTPException(403, "Not your organization")

    try:
        session = billing_checkout.create_session(
            db,
            organization_id=payload.organization_id,
            workspace_id=payload.workspace_id,
            plan_code=payload.plan,
            billing_cycle=payload.billing_cycle,
            seat_count=payload.seat_count,
            currency=payload.currency,
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
            correlation_id=ids.correlation_id(),
            quote_id=payload.quote_id,
        )
    except billing_quotes.QuoteError as e:
        raise HTTPException(400, str(e))
    db.commit()

    return CheckoutResponse(
        checkout_session_id=session.id,
        provider=session.provider,
        redirect_url=session.provider_redirect_url,
        state=session.state,
    )


@router.post("/checkout-session/{session_id}/mark-paid-stub")
def mark_paid_stub(session_id: str, body: dict | None = None,
                   db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Stub-only endpoint to advance the local provider's payment to success.

    In production this transition is driven by the provider webhook
    (see routers/webhooks.py). The stub provider has no webhook source,
    so we expose this protected endpoint so QA and the admin console
    can finalize stub checkouts.
    """
    if current_user.role not in {"admin", "owner"}:
        raise HTTPException(403, "Admin only")
    try:
        result = billing_checkout.mark_payment_succeeded(
            db, checkout_session_id=session_id,
            provider_payment_id=(body or {}).get("provider_payment_id"),
        )
    except ValueError as exc:
        # An unknown session id is a client mistake, not a server fault. It
        # raised ValueError from the service and surfaced as a 500.
        db.rollback()
        raise HTTPException(404, str(exc) or "Checkout session not found")
    db.commit()
    return result
