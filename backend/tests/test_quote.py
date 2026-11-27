"""Server-authoritative price quote (Handoff §D-05).

Guards: pricing math matches the canonical commercial constants, currency /
promotion / enterprise gating, quote expiry, and that checkout charges the
quoted amount (not a recomputed one).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.commercial import ids
from app.commercial.billing import quotes, checkout as billing_checkout
from app.commercial.billing.quotes import QuoteError
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, CommercialInvoice, PriceQuote,
)


# ── pricing math (pure) ─────────────────────────────────────────────────────
def test_governed_monthly_and_annual_match_constants():
    m = quotes.compute_amounts("governed", "monthly", 1)
    assert m["total_cents"] == 1500 and m["discount_cents"] == 0

    a = quotes.compute_amounts("governed", "annual", 1)
    # $15 × 12 = $180 list, 20% off → $144 charged today; $12 effective/mo.
    assert a["subtotal_cents"] == 18000
    assert a["discount_cents"] == 3600
    assert a["total_cents"] == 14400
    assert a["effective_monthly_unit_cents"] == 1200


def test_verified_annual_per_worker_is_7680():
    a = quotes.compute_amounts("verified", "annual", 1)
    assert a["total_cents"] == 7680  # $76.80


def test_seats_multiply():
    assert quotes.compute_amounts("sovereign", "monthly", 10)["total_cents"] == 25000


def test_enterprise_has_no_self_serve_quote():
    with pytest.raises(QuoteError):
        quotes.compute_amounts("enterprise", "monthly", 1)


# ── create / gating ─────────────────────────────────────────────────────────
def test_create_quote_persists_and_is_active(db):
    q = quotes.create_quote(db, plan_code="governed", billing_cycle="annual", seat_count=5)
    db.commit()
    assert q.state == "active"
    assert q.total_cents == 14400 * 5
    assert q.expires_at is not None
    assert db.query(PriceQuote).filter_by(id=q.id).one().total_cents == q.total_cents


def test_unsupported_currency_rejected(db):
    with pytest.raises(QuoteError):
        quotes.create_quote(db, plan_code="verified", currency="EUR")


def test_promotion_code_rejected_until_programme_exists(db):
    with pytest.raises(QuoteError):
        quotes.create_quote(db, plan_code="verified", promotion_code="SAVE10")


# ── expiry ──────────────────────────────────────────────────────────────────
def test_expired_quote_raises_and_flips_state(db):
    q = quotes.create_quote(db, plan_code="verified", billing_cycle="monthly", seat_count=1)
    q.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    with pytest.raises(QuoteError):
        quotes.get_valid_quote(db, q.id)
    assert db.query(PriceQuote).filter_by(id=q.id).one().state == "expired"


# ── checkout consumes the quote (server authority end-to-end) ───────────────
def _seed_org(db):
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name="Acme", commercial_state="trial_active"))
    db.add(CommercialSubscription(id=ids.subscription_id(), organization_id=org_id,
                                  plan_code="verified", state="trial_active", seat_count=1))
    db.commit()
    return org_id


def test_checkout_charges_quoted_amount(db):
    org_id = _seed_org(db)
    q = quotes.create_quote(db, plan_code="governed", billing_cycle="annual",
                            seat_count=3, organization_id=org_id)
    db.commit()

    sess = billing_checkout.create_session(
        db, organization_id=org_id, workspace_id=None,
        plan_code="verified", billing_cycle="monthly", seat_count=1,  # stale client values
        quote_id=q.id,
    )
    db.commit()
    # Quote is authoritative: session reflects the quote, not the client fields.
    assert sess.quote_id == q.id
    assert sess.plan_code == "governed" and sess.billing_cycle == "annual" and sess.seat_count == 3

    result = billing_checkout.mark_payment_succeeded(db, checkout_session_id=sess.id)
    db.commit()

    inv = db.query(CommercialInvoice).filter_by(id=result["invoice_id"]).one()
    assert inv.total_cents == q.total_cents == 14400 * 3
    assert inv.discount_cents == 3600 * 3
    assert db.query(PriceQuote).filter_by(id=q.id).one().state == "consumed"


def test_checkout_rejects_expired_quote(db):
    org_id = _seed_org(db)
    q = quotes.create_quote(db, plan_code="verified", organization_id=org_id)
    q.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    with pytest.raises(QuoteError):
        billing_checkout.create_session(
            db, organization_id=org_id, workspace_id=None,
            plan_code="verified", billing_cycle="monthly", seat_count=1,
            quote_id=q.id,
        )
