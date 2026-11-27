"""Discount codes are offered on annual checkouts, and only there.

Black Friday 2026 runs as a Stripe promotion code. Stripe itself enforces the
discount size, the expiry date and the first-time-order rule; the one rule left
to the product is "annual plans only", enforced by offering the code box on an
annual Checkout Session and on no other.

Stripe is stubbed — no network, no live resources.
"""

from __future__ import annotations

import hashlib

import pytest

from app.billing import service as billing_service
from app.billing import worker_state
from database.models import BillingPlan, BillingSubscription, Organization, User

ORG = 8501


@pytest.fixture
def first_purchase_org(engine, db):
    """An organization on Verified with no Stripe customer or subscription yet."""
    for model in (Organization, BillingPlan, BillingSubscription):
        model.__table__.create(engine, checkfirst=True)

    def _clean():
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id == ORG).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id == ORG).delete(synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG, name="Promo Org"))
    plans = {}
    for i, (code, monthly, annual, cap) in enumerate(
        [("verified", 8.0, 76.80, 25), ("governed", 15.0, 144.0, 250),
         ("sovereign", 25.0, 240.0, None)], start=1
    ):
        p = BillingPlan(code=code, name=code.title(), tier_order=i,
                        monthly_price_per_user=monthly, annual_price_per_user=annual,
                        user_limit=cap)
        db.add(p)
        plans[code] = p
    db.flush()
    db.add(BillingSubscription(
        organization_id=ORG, plan_id=plans["verified"].id, billing_cycle="monthly",
        subscription_status="active", stripe_customer_id=None,
        stripe_subscription_id=None, billable_users=1, active_users=1,
    ))
    db.add(User(name="Owner", email="owner@promo.test", organization_id=ORG,
                role="owner", billing_state=worker_state.ACTIVE_BILLABLE, is_active=True))
    db.commit()
    yield plans
    _clean()


def _checkout(db, monkeypatch, plan_code, cycle):
    """POST /upgrade for ORG and return the kwargs sent to Checkout Session create."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth.service import get_current_user
    from app.billing.router import router as billing_router
    import app.billing.router as br
    from database.database import get_db

    monkeypatch.setenv(f"STRIPE_PRICE_{plan_code.upper()}_{cycle.upper()}",
                       f"price_{plan_code}_{cycle}")
    monkeypatch.setattr(billing_service, "change_stripe_plan",
                        lambda *a, **k: pytest.fail("a first purchase must not modify"))
    created = []

    def _fake_create(**kw):
        created.append(kw)
        return type("S", (), {"id": "cs_test_stub", "url": "https://stub/checkout"})()

    monkeypatch.setattr(br.stripe.checkout.Session, "create", _fake_create)

    actor = db.query(User).filter_by(email="owner@promo.test").one()
    app = FastAPI()
    app.include_router(billing_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    with TestClient(app) as c:
        r = c.post("/api/admin/billing/upgrade",
                   json={"plan_code": plan_code, "billing_cycle": cycle})

    assert r.status_code == 200, r.text
    assert len(created) == 1, "a first purchase goes through exactly one Checkout Session"
    return created[0]


@pytest.mark.parametrize("plan_code", ["governed", "sovereign"])
def test_annual_checkout_offers_the_discount_code_box(db, first_purchase_org,
                                                      monkeypatch, plan_code):
    kwargs = _checkout(db, monkeypatch, plan_code, "annual")
    assert kwargs.get("allow_promotion_codes") is True


@pytest.mark.parametrize("plan_code", ["governed", "sovereign"])
def test_monthly_checkout_never_offers_a_discount_code(db, first_purchase_org,
                                                       monkeypatch, plan_code):
    """The offer is annual-only; Stripe cannot express that, so the box must be absent."""
    kwargs = _checkout(db, monkeypatch, plan_code, "monthly")
    assert "allow_promotion_codes" not in kwargs


def test_checkout_never_hardcodes_a_discount(db, first_purchase_org, monkeypatch):
    """Stripe refuses allow_promotion_codes together with discounts, and a hardcoded
    discount would outlive the offer. The discount must only ever come from a code."""
    kwargs = _checkout(db, monkeypatch, "governed", "annual")
    assert "discounts" not in kwargs


def test_idempotency_key_moved_off_the_pre_promotion_version():
    """Stripe rejects a reused idempotency key whose parameters differ. Annual sessions
    gained allow_promotion_codes, so a key minted before this change must not be
    reused for the same intent after it."""
    from app.billing.router import _checkout_idempotency_key

    key = _checkout_idempotency_key(ORG, "governed", "annual", 5)
    old = hashlib.sha256(f"checkout:v1:{ORG}:governed:annual:5".encode()).hexdigest()
    assert key != old
    assert key == _checkout_idempotency_key(ORG, "governed", "annual", 5), "still stable"
