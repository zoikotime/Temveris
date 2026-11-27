"""Server-authoritative price quotes (Handoff §D-05, §6.3, §12.1 Price quote).

The frontend NEVER computes totals, tax, discount, FX or renewal. It renders
a quote produced here and references it by `quote_id`. Checkout consumes a
quote by id so the amount the customer saw is the amount they are charged.

Pricing is derived from `constants.CANONICAL_PRICES_MONTHLY` (the binding
commercial contract), not from anything the client sends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.commercial import ids, constants
from app.commercial.models import PriceQuote

# Currencies the Billing Service can quote today. Local currency + FX come
# from the provider, never from client-side arithmetic (Handoff §8.3).
# ponytail: single currency until Billing Service returns real FX quotes.
SUPPORTED_CURRENCIES = {"USD"}

# How long a quote stays honourable before it must be refreshed (§7.1 states:
# quote expired). Any seat/cadence/currency/address change mints a new quote.
QUOTE_TTL_SECONDS = 1800  # 30 minutes


class QuoteError(ValueError):
    """Base for quote problems the router maps to 4xx (code carries which)."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_amounts(plan_code: str, billing_cycle: str, seat_count: int) -> dict:
    """Pure pricing math. The single source of truth for what a plan costs.

    Returns cents integers so no float rounding leaks into money. Tax is left
    at 0/estimated here — it is computed after billing-address validation.
    """
    if plan_code not in constants.CANONICAL_PRICES_MONTHLY:
        raise QuoteError(constants.ERR_VALIDATION_FAILED, f"Unknown plan: {plan_code}")
    base = constants.CANONICAL_PRICES_MONTHLY[plan_code]
    if base is None:  # enterprise / custom
        raise QuoteError(constants.ERR_VALIDATION_FAILED,
                         f"{plan_code} is sales-led; no self-serve quote")
    if billing_cycle not in {"monthly", "annual"}:
        raise QuoteError(constants.ERR_VALIDATION_FAILED,
                         f"Unsupported billing cycle: {billing_cycle}")

    seats = max(1, int(seat_count or 1))
    monthly_unit_cents = round(base * 100)

    if billing_cycle == "annual":
        list_subtotal = monthly_unit_cents * 12 * seats
        discount_cents = round(list_subtotal * constants.ANNUAL_DISCOUNT_PCT / 100)
        total_cents = list_subtotal - discount_cents
        effective_monthly_unit = round(monthly_unit_cents * (1 - constants.ANNUAL_DISCOUNT_PCT / 100))
        renewal_days = 365
    else:
        list_subtotal = monthly_unit_cents * seats
        discount_cents = 0
        total_cents = list_subtotal
        effective_monthly_unit = monthly_unit_cents
        renewal_days = 30

    return {
        "plan_code": plan_code,
        "billing_cycle": billing_cycle,
        "seat_count": seats,
        "monthly_unit_cents": monthly_unit_cents,
        "effective_monthly_unit_cents": effective_monthly_unit,
        "subtotal_cents": list_subtotal,
        "discount_cents": discount_cents,
        "tax_cents": 0,
        "tax_state": "estimated",  # final after billing address (§3.2)
        "total_cents": total_cents,
        "renewal_amount_cents": total_cents,
        "renewal_days": renewal_days,
        "line_items": [
            {
                "label": f"{plan_code.title()} × {seats}",
                "cadence": billing_cycle,
                "unit_price_cents": monthly_unit_cents,
                "effective_monthly_unit_cents": effective_monthly_unit,
                "quantity": seats,
                "amount_cents": list_subtotal,
            }
        ],
    }


def create_quote(
    db: Session,
    *,
    plan_code: str,
    billing_cycle: str = "monthly",
    seat_count: int = 1,
    currency: str = "USD",
    organization_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    country: Optional[str] = None,
    promotion_code: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> PriceQuote:
    currency = (currency or "USD").upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise QuoteError(constants.ERR_VALIDATION_FAILED,
                         f"Currency not supported for self-serve: {currency}")
    # ponytail: no active promotion programme yet — any code is rejected
    # server-side rather than silently ignored (§8.4). Wire real validation
    # here when a campaign catalogue exists.
    if promotion_code:
        raise QuoteError(constants.ERR_VALIDATION_FAILED,
                         "Promotion codes are not currently available")

    amt = compute_amounts(plan_code, billing_cycle, seat_count)
    now = _utcnow()
    row = PriceQuote(
        id=ids.quote_id(),
        organization_id=organization_id,
        workspace_id=workspace_id,
        plan_code=plan_code,
        billing_cycle=billing_cycle,
        seat_count=amt["seat_count"],
        currency=currency,
        country=country,
        promotion_code=None,
        subtotal_cents=amt["subtotal_cents"],
        discount_cents=amt["discount_cents"],
        tax_cents=amt["tax_cents"],
        tax_state=amt["tax_state"],
        total_cents=amt["total_cents"],
        renewal_amount_cents=amt["renewal_amount_cents"],
        renewal_at=now + timedelta(days=amt["renewal_days"]),
        line_items=amt["line_items"],
        state="active",
        expires_at=now + timedelta(seconds=QUOTE_TTL_SECONDS),
        correlation_id=correlation_id,
    )
    db.add(row)
    db.flush()
    return row


def get_valid_quote(db: Session, quote_id: str) -> PriceQuote:
    """Fetch a quote and enforce it is still honourable.

    Raises QuoteError(expired) if past TTL — the caller must refresh before
    payment (§7.4 / §8.6 quote-expired recovery).
    """
    q = db.query(PriceQuote).filter_by(id=quote_id).one_or_none()
    if q is None:
        raise QuoteError(constants.ERR_VALIDATION_FAILED, "Quote not found")
    expires = q.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if q.state == "consumed":
        raise QuoteError(constants.ERR_VALIDATION_FAILED, "Quote already used")
    if expires is not None and _utcnow() > expires:
        if q.state != "expired":
            q.state = "expired"
            db.flush()
        raise QuoteError(constants.ERR_VALIDATION_FAILED, "Quote expired; refresh before payment")
    return q


def mark_consumed(db: Session, quote: PriceQuote) -> None:
    quote.state = "consumed"
    db.flush()
