"""Endpoint-level billing tenant isolation — Phase 1.5.

Two endpoints outside `app/billing` read billing tables and were hardcoded to
the shared `"default"` organization, so every tenant was shown organization 1's
subscription, invoices and payment method:

  * GET /org/linked-status         (app/org/router.py)
  * GET /api/workspace/billing-summary (app/workspace/router.py)

`/api/workspace/billing-summary` additionally counted billable users across the
whole platform with no organization filter.

Also covers the upgrade endpoint refusing to grant a plan when the Stripe price
is unconfigured, rather than silently upgrading for free.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import worker_state
from database.database import get_db
from database.models import (
    BillingInvoice, BillingPaymentMethod, BillingPlan, BillingSubscription,
    Organization, User,
)

ORG_A = 7301
ORG_B = 7302


@pytest.fixture
def seeded(engine, db):
    """Two organizations, each with its own subscription, invoice, card and staff."""
    # These endpoints read well beyond the billing tables (org_settings, holidays,
    # app_settings, sessions...). Create the whole legacy schema rather than
    # chasing each dependency — it is the same metadata production uses.
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        for model in (BillingInvoice, BillingPaymentMethod, BillingSubscription, BillingPlan):
            db.query(model).delete()
        db.query(User).filter(User.organization_id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="Alpha Ltd"))
    db.add(Organization(id=ORG_B, name="Beta Ltd"))

    verified = BillingPlan(code="verified", name="Verified", tier_order=1,
                           monthly_price_per_user=8.0, annual_price_per_user=76.80,
                           user_limit=25)
    governed = BillingPlan(code="governed", name="Governed", tier_order=2,
                           monthly_price_per_user=15.0, annual_price_per_user=144.0,
                           user_limit=100)
    db.add_all([verified, governed])
    db.flush()

    db.add(BillingSubscription(organization_id=ORG_A, plan_id=verified.id,
                               billing_cycle="monthly", subscription_status="trial"))
    db.add(BillingSubscription(organization_id=ORG_B, plan_id=governed.id,
                               billing_cycle="annual", subscription_status="active"))
    db.add(BillingInvoice(organization_id=ORG_A, invoice_number="A-001", total_amount=11.0))
    db.add(BillingInvoice(organization_id=ORG_B, invoice_number="B-001", total_amount=99.0))
    db.add(BillingPaymentMethod(organization_id=ORG_B, brand="visa", last_four="4242",
                                is_default=True))

    # 2 billable in A, 6 billable in B (plus non-billable noise in both).
    for i in range(2):
        db.add(User(name=f"a{i}", email=f"a{i}@alpha.test", organization_id=ORG_A,
                    billing_state=worker_state.ACTIVE_BILLABLE, is_active=True, role="employee"))
    db.add(User(name="a-inv", email="a-inv@alpha.test", organization_id=ORG_A,
                billing_state=worker_state.INVITED_PENDING, is_active=True, role="employee"))
    for i in range(6):
        db.add(User(name=f"b{i}", email=f"b{i}@beta.test", organization_id=ORG_B,
                    billing_state=worker_state.ACTIVE_BILLABLE, is_active=True, role="employee"))
    db.add(User(name="b-gone", email="b-gone@beta.test", organization_id=ORG_B,
                billing_state=worker_state.DEACTIVATED, is_active=False, role="employee"))
    db.commit()
    yield
    _clean()


def _admin(db, org_id, email):
    u = User(name="Admin", email=email, organization_id=org_id, role="admin",
             billing_state=worker_state.ACTIVE_BILLABLE, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _client(db, actor, router):
    from app.auth.service import get_current_user
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/workspace/billing-summary
# ══════════════════════════════════════════════════════════════════════════════
def _billing_summary(db, actor):
    from app.workspace.router import router as workspace_router
    with _client(db, actor, workspace_router) as c:
        return c.get("/api/workspace/billing-summary")


def test_billing_summary_returns_org_a_data_to_org_a(db, seeded):
    actor = _admin(db, ORG_A, "admin@alpha.test")
    r = _billing_summary(db, actor)
    assert r.status_code == 200
    body = r.json()
    assert body["subscription"]["plan_code"] == "verified"
    assert body["subscription"]["billing_cycle"] == "monthly"


def test_billing_summary_returns_org_b_data_to_org_b(db, seeded):
    actor = _admin(db, ORG_B, "admin@beta.test")
    r = _billing_summary(db, actor)
    assert r.status_code == 200
    body = r.json()
    assert body["subscription"]["plan_code"] == "governed"
    assert body["subscription"]["billing_cycle"] == "annual"


def test_billing_summary_seat_count_is_org_scoped(db, seeded):
    """Previously counted every active user on the platform."""
    a = _billing_summary(db, _admin(db, ORG_A, "seat-a@alpha.test")).json()
    b = _billing_summary(db, _admin(db, ORG_B, "seat-b@beta.test")).json()

    a_seats = _find_seat_count(a)
    b_seats = _find_seat_count(b)
    # Each org sees only its own billable staff (+1 for the admin created above).
    assert a_seats == 3      # 2 billable + this admin; invited/deactivated excluded
    assert b_seats == 7      # 6 billable + this admin
    assert a_seats != b_seats


def _find_seat_count(body: dict) -> int:
    """The seat figure, wherever the response nests it."""
    for section in body.values():
        if isinstance(section, dict):
            for key in ("billable_users", "billable", "billable_seats", "active_users"):
                if key in section:
                    return section[key]
    for key in ("billable_users", "billable", "billable_seats"):
        if key in body:
            return body[key]
    raise AssertionError(f"no seat count in response: {list(body)}")


def test_billing_summary_rejects_user_with_no_organization(db, seeded):
    orphan = User(name="Nobody", email="orphan@nowhere.test", organization_id=None,
                  role="admin", is_active=True)
    db.add(orphan)
    db.commit()
    db.refresh(orphan)

    r = _billing_summary(db, orphan)
    assert r.status_code == 400
    assert "organization" in r.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# GET /org/linked-status
# ══════════════════════════════════════════════════════════════════════════════
def test_linked_status_reports_each_orgs_own_plan(db, seeded):
    from app.org.router import router as org_router

    def _plan_for(actor):
        with _client(db, actor, org_router) as c:
            r = c.get("/org/linked-status")
            assert r.status_code == 200
            return r.text

    a_body = _plan_for(_admin(db, ORG_A, "ls-a@alpha.test"))
    b_body = _plan_for(_admin(db, ORG_B, "ls-b@beta.test"))

    # Org A is on Verified and must not be shown Governed, and vice versa.
    assert "Governed" not in a_body
    assert "Verified" not in b_body


# ══════════════════════════════════════════════════════════════════════════════
# Missing Stripe price must not grant a free upgrade
# ══════════════════════════════════════════════════════════════════════════════
def test_missing_stripe_price_id_refuses_upgrade_and_changes_nothing(db, seeded, monkeypatch):
    from app.billing.router import router as billing_router

    # Guarantee no price is configured for the requested plan/cycle.
    monkeypatch.delenv("STRIPE_PRICE_GOVERNED_MONTHLY", raising=False)

    actor = _admin(db, ORG_A, "upgrade@alpha.test")
    actor.role = "owner"          # billing management per §15
    db.commit()

    before = db.query(BillingSubscription).filter_by(organization_id=ORG_A).one()
    plan_before, status_before, cycle_before = (
        before.plan_id, before.subscription_status, before.billing_cycle)

    with _client(db, actor, billing_router) as c:
        r = c.post("/api/admin/billing/upgrade",
                   json={"plan_code": "governed", "billing_cycle": "monthly"})

    assert r.status_code == 500, r.text
    assert "not fully configured" in r.json()["detail"]

    after = db.query(BillingSubscription).filter_by(organization_id=ORG_A).one()
    assert after.plan_id == plan_before               # plan unchanged
    assert after.subscription_status == status_before  # not marked active
    assert after.billing_cycle == cycle_before         # cycle unchanged
