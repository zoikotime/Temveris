"""Seat selection and the plan-limit gate on POST /api/admin/billing/upgrade.

The subscription configurator lets a customer choose how many seats to buy,
which is new: the endpoint previously derived the quantity from the billable
headcount alone. That makes the request a number the browser now sends, so
these tests exist to pin what the server does with it.

The rule is a floor and a ceiling, both server-side:

  floor    the billable headcount — you cannot buy fewer seats than the people
           already occupying them, or the organization is instantly over
           capacity on a plan it just paid for.
  ceiling  the target plan's ``user_limit``, read from the plan catalog.

Nothing here trusts the client for price, plan ceiling or Stripe Price id.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database import models
from database.models import User, Organization
from app.auth.service import get_current_user, hash_password
from app.billing import service as billing_service
from app.billing import worker_state
import app.billing.router as billing_router


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _stripe_prices(monkeypatch):
    """The endpoint fails closed without a configured price — set all six."""
    for plan in ("VERIFIED", "GOVERNED", "SOVEREIGN"):
        for cycle in ("MONTHLY", "ANNUAL"):
            monkeypatch.setenv(f"STRIPE_PRICE_{plan}_{cycle}", f"price_{plan.lower()}_{cycle.lower()}")
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.com")


@pytest.fixture
def plans(engine, Session):
    """The real catalog, seeded exactly as the app seeds it.

    Same shape as `plans_table` in test_billing_seats_plans_rbac: the table is
    created on demand and cleared either side, because `ensure_plans` commits
    and committed rows would otherwise collide on the unique `code` across
    tests sharing this engine.
    """
    # Every billing table the endpoint touches. The shared engine only carries
    # the core schema, so these are created on demand.
    for table in (models.BillingPlan, models.BillingSubscription,
                  models.BillingAuditEvent, models.BillingEnterpriseQuote,
                  models.BillingInvoice, models.BillingDowngradeRequest):
        table.__table__.create(engine, checkfirst=True)
    s = Session()
    try:
        s.query(models.BillingPlan).delete()
        s.commit()
        billing_service.ensure_plans(s)
        s.commit()
        catalog = {p.code: p for p in s.query(models.BillingPlan).all()}
        for row in catalog.values():
            s.expunge(row)
    finally:
        s.close()

    yield catalog

    s = Session()
    try:
        s.query(models.BillingPlan).delete()
        s.commit()
    finally:
        s.close()


@pytest.fixture
def org(Session, plans):
    """An organization on Verified with a handful of billable people."""
    suffix = int(time.time() * 1_000_000) % 10_000_000
    s = Session()
    try:
        o = Organization(name="Configurator Ltd", billing_type="customer")
        s.add(o)
        s.flush()
        admin = User(
            name="Ada Admin", email=f"cfg-admin-{suffix}@engines.example.com",
            password=hash_password("Passw0rd!23"), role="admin",
            organization_id=o.id, is_active=True,
            billing_state=worker_state.ACTIVE_BILLABLE,
        )
        s.add(admin)
        # Two more billable people, so the floor is 3.
        for i in range(2):
            s.add(User(
                name=f"Worker {i}", email=f"cfg-w{i}-{suffix}@engines.example.com",
                password=hash_password("Passw0rd!23"), role="employee",
                organization_id=o.id, is_active=True,
                billing_state=worker_state.ACTIVE_BILLABLE,
            ))
        s.commit()
        s.refresh(admin)
        s.expunge(admin)
        return {"admin": admin, "org_id": o.id}
    finally:
        s.close()


def _client(Session, user):
    app = FastAPI()
    app.include_router(billing_router.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def client(Session, org):
    return _client(Session, org["admin"])


@pytest.fixture
def stripe_session():
    """Intercept Checkout creation; assert on what Stripe was actually asked for."""
    created = {}

    def _create(**kwargs):
        created.update(kwargs)
        return MagicMock(url="https://checkout.stripe.test/s/abc", id="cs_test_1")

    with patch.object(billing_router.stripe.checkout.Session, "create", side_effect=_create), \
         patch.object(billing_router.stripe.Subscription, "list",
                      return_value={"data": []}):
        yield created


def _upgrade(client, **body):
    return client.post("/api/admin/billing/upgrade", json=body)


# ══════════════════════════════════════════════════════════════════════════
# The catalog is the source of truth
# ══════════════════════════════════════════════════════════════════════════
def test_the_catalog_holds_the_limits_and_prices_the_ui_shows(plans):
    governed, sovereign, enterprise = plans["governed"], plans["sovereign"], plans["enterprise"]
    assert governed.user_limit == 250
    assert governed.monthly_price_per_user == 15.0
    assert governed.annual_price_per_user == 144.0
    assert governed.annual_discount_percentage == 20.0
    assert sovereign.monthly_price_per_user == 25.0
    assert sovereign.user_limit is None            # unlimited
    assert enterprise.is_enterprise is True


# ══════════════════════════════════════════════════════════════════════════
# Seats — the requested quantity
# ══════════════════════════════════════════════════════════════════════════
def test_requested_seats_reach_stripe_as_the_line_item_quantity(client, stripe_session):
    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=25)
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.test/")
    assert stripe_session["line_items"] == [{"price": "price_governed_monthly", "quantity": 25}]


def test_omitting_seats_keeps_the_original_headcount_behaviour(client, stripe_session):
    # Three billable users were seeded.
    r = _upgrade(client, plan_code="governed", billing_cycle="monthly")
    assert r.status_code == 200, r.text
    assert stripe_session["line_items"][0]["quantity"] == 3


def test_seats_below_the_billable_headcount_are_raised_to_it(client, stripe_session):
    """Buying 1 seat for 3 people would leave the org over capacity on arrival."""
    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=1)
    assert r.status_code == 200, r.text
    assert stripe_session["line_items"][0]["quantity"] == 3


def test_the_cycle_selects_the_annual_stripe_price(client, stripe_session):
    r = _upgrade(client, plan_code="governed", billing_cycle="annual", seats=25)
    assert r.status_code == 200, r.text
    assert stripe_session["line_items"][0]["price"] == "price_governed_annual"


# ══════════════════════════════════════════════════════════════════════════
# The plan ceiling
# ══════════════════════════════════════════════════════════════════════════
def test_exactly_at_the_limit_is_allowed(client, stripe_session, plans):
    limit = plans["governed"].user_limit
    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=limit)
    assert r.status_code == 200, r.text
    assert stripe_session["line_items"][0]["quantity"] == limit


def test_one_above_the_limit_is_refused(client, stripe_session, plans):
    limit = plans["governed"].user_limit
    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=limit + 1)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PLAN_LIMIT_EXCEEDED"
    assert not stripe_session, "no Checkout Session may be created for a refused quantity"


def test_the_refusal_carries_the_numbers_the_modal_renders(client, plans):
    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=300)
    detail = r.json()["detail"]

    assert detail["requested_seats"] == 300
    assert detail["current_plan"]["name"] == "Governed"
    assert detail["current_plan"]["user_limit"] == plans["governed"].user_limit
    # The plan that actually solves 300 seats, named by the server.
    assert detail["upgrade_plan"]["code"] == "sovereign"
    assert detail["upgrade_plan"]["monthly_price_per_user"] == plans["sovereign"].monthly_price_per_user
    assert detail["enterprise_plan"]["is_enterprise"] is True
    assert "250" in detail["message"] and "300" in detail["message"]


def test_sovereign_has_no_ceiling_so_a_large_order_proceeds(client, stripe_session):
    r = _upgrade(client, plan_code="sovereign", billing_cycle="monthly", seats=5000)
    assert r.status_code == 200, r.text
    assert stripe_session["line_items"] == [{"price": "price_sovereign_monthly", "quantity": 5000}]


def test_switching_to_the_offered_plan_then_succeeds(client, stripe_session):
    """The modal's 'Switch to Sovereign' path, end to end."""
    blocked = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=300)
    offered = blocked.json()["detail"]["upgrade_plan"]["code"]

    ok = _upgrade(client, plan_code=offered, billing_cycle="monthly", seats=300)
    assert ok.status_code == 200, ok.text
    assert stripe_session["line_items"][0]["quantity"] == 300


# ══════════════════════════════════════════════════════════════════════════
# Security — the browser decides nothing that costs money
# ══════════════════════════════════════════════════════════════════════════
def test_an_unauthenticated_caller_is_refused(Session, org):
    assert _client(Session, None).post(
        "/api/admin/billing/upgrade",
        json={"plan_code": "governed", "seats": 25},
    ).status_code in (401, 403)


def test_a_non_admin_cannot_start_checkout(Session, org, plans):
    s = Session()
    worker = s.query(User).filter(
        User.organization_id == org["org_id"], User.role == "employee").first()
    s.refresh(worker)
    s.expunge(worker)
    s.close()
    r = _client(Session, worker).post(
        "/api/admin/billing/upgrade", json={"plan_code": "governed", "seats": 25})
    assert r.status_code == 403


def test_a_stripe_price_id_in_the_body_is_ignored(client, stripe_session):
    """The price comes from configuration, keyed by plan and cycle."""
    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=10,
                 price_id="price_attacker_free", stripe_price="price_attacker_free")
    assert r.status_code == 200, r.text
    assert stripe_session["line_items"][0]["price"] == "price_governed_monthly"


def test_an_unknown_plan_is_rejected(client):
    assert _upgrade(client, plan_code="platinum", seats=10).status_code == 404


def test_enterprise_cannot_be_self_served(client, stripe_session):
    r = _upgrade(client, plan_code="enterprise", billing_cycle="monthly", seats=400)
    assert r.status_code == 400
    assert "quote-led" in r.json()["detail"].lower()
    assert not stripe_session


def test_a_negative_or_zero_seat_count_is_rejected_by_validation(client):
    for bad in (0, -5):
        r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=bad)
        assert r.status_code == 422, f"seats={bad} should not validate"


def test_the_checkout_session_is_bound_to_the_callers_own_organization(client, stripe_session, org):
    _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=25)
    assert stripe_session["client_reference_id"] == str(org["org_id"])


def test_success_and_cancel_urls_come_from_configuration(client, stripe_session):
    _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=25)
    assert stripe_session["success_url"].startswith("https://app.example.com/")
    assert stripe_session["cancel_url"].startswith("https://app.example.com/")


def test_success_returns_to_the_confirmation_screen_with_the_session(client, stripe_session):
    """The customer lands somewhere that says they paid, not on a settings tab.

    The session id has to survive the round trip — /upgrade/confirm is what
    actually verifies the payment with Stripe, and it needs that id.
    """
    _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=25)
    assert "/subscription/confirmed" in stripe_session["success_url"]
    assert "session_id={CHECKOUT_SESSION_ID}" in stripe_session["success_url"]


def test_cancel_returns_to_the_configurator(client, stripe_session):
    """Backing out lands where the configuration still is, not on a settings page."""
    _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=25)
    assert stripe_session["cancel_url"].endswith("/subscription/configure")


def test_checkout_is_created_in_subscription_mode(client, stripe_session):
    _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=25)
    assert stripe_session["mode"] == "subscription"


# ══════════════════════════════════════════════════════════════════════════
# Idempotency
# ══════════════════════════════════════════════════════════════════════════
def test_repeated_clicks_reuse_one_stripe_idempotency_key(Session, org, stripe_session):
    """Stripe returns the same session for the same key, so a double-click
    cannot produce two subscriptions that both get paid."""
    keys = []

    def _create(**kwargs):
        keys.append(kwargs.get("idempotency_key"))
        return MagicMock(url="https://checkout.stripe.test/s/abc", id="cs_test_1")

    c = _client(Session, org["admin"])
    with patch.object(billing_router.stripe.checkout.Session, "create", side_effect=_create), \
         patch.object(billing_router.stripe.Subscription, "list", return_value={"data": []}):
        for _ in range(3):
            assert c.post("/api/admin/billing/upgrade",
                          json={"plan_code": "governed", "billing_cycle": "monthly",
                                "seats": 25}).status_code == 200

    assert len(set(keys)) == 1, f"expected one idempotency key, got {set(keys)}"


def test_a_different_seat_count_is_a_different_intent(Session, org):
    """Genuinely changing the order must still be able to open a new session."""
    keys = []

    def _create(**kwargs):
        keys.append(kwargs.get("idempotency_key"))
        return MagicMock(url="https://checkout.stripe.test/s/abc", id="cs_test_1")

    c = _client(Session, org["admin"])
    with patch.object(billing_router.stripe.checkout.Session, "create", side_effect=_create), \
         patch.object(billing_router.stripe.Subscription, "list", return_value={"data": []}):
        c.post("/api/admin/billing/upgrade",
               json={"plan_code": "governed", "billing_cycle": "monthly", "seats": 25})
        c.post("/api/admin/billing/upgrade",
               json={"plan_code": "governed", "billing_cycle": "monthly", "seats": 40})

    assert len(set(keys)) == 2


def test_an_existing_active_subscription_blocks_a_second_checkout(Session, org, plans):
    """The guard that stopped four parallel subscriptions accumulating."""
    s = Session()
    sub = billing_service.ensure_subscription(s, org["org_id"])
    sub.stripe_customer_id = "cus_existing"
    s.commit()
    s.close()

    c = _client(Session, org["admin"])
    with patch.object(billing_router.stripe.Subscription, "list",
                      return_value={"data": [{"id": "sub_already_paying"}]}), \
         patch.object(billing_router.stripe.checkout.Session, "create") as create:
        r = c.post("/api/admin/billing/upgrade",
                   json={"plan_code": "governed", "billing_cycle": "monthly", "seats": 25})

    assert r.status_code == 409
    create.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
# Misconfiguration fails closed
# ══════════════════════════════════════════════════════════════════════════
def test_a_missing_stripe_price_refuses_rather_than_granting_the_plan(client, monkeypatch):
    monkeypatch.delenv("STRIPE_PRICE_GOVERNED_MONTHLY", raising=False)
    with patch.object(billing_router.stripe.checkout.Session, "create") as create:
        r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=25)
    assert r.status_code == 500
    create.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
# Guard ordering — regressions found in the browser
# ══════════════════════════════════════════════════════════════════════════
#
# Both of these were reported as "the plan-limit popup does not appear". The
# cause was the order of the checks in /upgrade, not the modal: the "you are
# already on this plan" guard ran first and short-circuited with a 400, so a
# caller who raised the seat count on the tier they were already on never
# reached the seat-limit check at all.

def _put_org_on(Session, org_id, plan_code, plans, cycle="monthly", billable=None):
    s = Session()
    try:
        sub = billing_service.ensure_subscription(s, org_id)
        sub.plan_id = plans[plan_code].id
        sub.billing_cycle = cycle
        if billable is not None:
            sub.billable_users = billable
        s.commit()
    finally:
        s.close()


def test_exceeding_the_limit_on_your_current_plan_still_reports_the_limit(
        client, Session, org, plans, stripe_session):
    """The reported bug.

    Sitting on Governed and asking for 300 seats used to answer "you are
    already on this plan and billing cycle" — a 400 with a string body, so the
    configurator had no PLAN_LIMIT_EXCEEDED to open the modal with, and the
    admin was told something both unhelpful and beside the point.
    """
    _put_org_on(Session, org["org_id"], "governed", plans)

    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=300)

    assert r.status_code == 409, f"expected the limit refusal, got {r.status_code}: {r.text}"
    detail = r.json()["detail"]
    assert detail["code"] == "PLAN_LIMIT_EXCEEDED"
    assert detail["current_plan"]["user_limit"] == plans["governed"].user_limit
    assert detail["requested_seats"] == 300
    assert detail["upgrade_plan"]["code"] == "sovereign"
    assert not stripe_session


def test_the_offered_plan_is_the_smallest_one_that_fits(client, Session, org, plans):
    """26 seats on Verified needs Governed, not the biggest plan on the shelf."""
    _put_org_on(Session, org["org_id"], "verified", plans)

    r = _upgrade(client, plan_code="verified", billing_cycle="monthly", seats=26)

    assert r.status_code == 409
    assert r.json()["detail"]["upgrade_plan"]["code"] == "governed"


def test_changing_only_the_seat_count_on_the_current_plan_is_allowed(
        client, Session, org, plans, stripe_session):
    """"Update anytime" has to mean something.

    Same plan, same cycle, different quantity is the change the configurator
    exists to make. It used to be rejected as a no-op.
    """
    _put_org_on(Session, org["org_id"], "governed", plans, billable=25)

    r = _upgrade(client, plan_code="governed", billing_cycle="monthly", seats=40)

    assert r.status_code == 200, r.text
    assert stripe_session["line_items"][0]["quantity"] == 40


def test_a_true_no_op_is_still_refused(client, Session, org, plans):
    """The original guard still earns its place — nothing at all has changed."""
    _put_org_on(Session, org["org_id"], "governed", plans)

    r = _upgrade(client, plan_code="governed", billing_cycle="monthly")

    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)      # the console renders this in a toast
    assert "already on this plan" in r.json()["detail"]
