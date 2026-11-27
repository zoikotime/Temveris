"""Phase 8 — downgrades are reviewed, capacity-checked, and actually applied.

``billing_downgrade_requests`` was write-only: the endpoint created a row, promised
"a billing specialist will review it within one business day", and no code, job or
screen ever read the table — 21 rows accumulated over two and a half months.

The review gate was also decorative. ``/upgrade`` had no tier check, so a customer
could request a *cheaper* plan there and it applied to Stripe immediately: review
bypassed, capacity unchecked. Org 1 has 57 billable users and Verified allows 25, so
that path could have moved an organization onto a plan that cannot hold it while
Stripe billed 57 seats.

Now: lower tier via /upgrade is refused, downgrades need super-admin approval, and
capacity is verified at approval *and* again at apply time because headcount moves
between the two.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import downgrades as billing_downgrades
from app.billing import router as billing_router
from app.billing import service as billing_service
from app.billing import worker_state
from database.database import get_db
from database.models import (
    BillingDowngradeRequest, BillingPlan, BillingSubscription, Organization, User,
)

API = "/api/admin/billing"
SA = "/api/super-admin"
ORG_A, ORG_B, ORG_INT = 8401, 8402, 8403
SUB_A, CUS_A = "sub_dgA", "cus_dgA"
ITEM_A = "si_dgA"


class StubStripe:
    """Records what reached Stripe, so "no duplicate subscription" is provable."""

    def __init__(self, *, status="active", price="price_governed_monthly"):
        self.modify_calls, self.create_calls = [], []
        outer = self

        class _Subscription:
            @staticmethod
            def retrieve(sid):
                return {"id": sid, "customer": CUS_A, "status": status,
                        "cancel_at_period_end": False,
                        "current_period_end": 1790000000,
                        "items": {"data": [{"id": ITEM_A, "quantity": 3,
                                            "price": {"id": price}}]}}

            @staticmethod
            def modify(sid, **kw):
                outer.modify_calls.append((sid, kw))
                return {"id": sid, "status": "active",
                        "items": {"data": [{"id": ITEM_A,
                                            "quantity": kw.get("items", [{}])[0].get("quantity", 3),
                                            "price": {"id": kw.get("items", [{}])[0].get("price")}}]}}

            @staticmethod
            def create(**kw):
                outer.create_calls.append(kw)
                raise AssertionError("a downgrade must never create a subscription")

        class _Session:
            @staticmethod
            def create(**kw):
                outer.create_calls.append(kw)
                raise AssertionError("a downgrade must never open a Checkout Session")

        self.Subscription = _Subscription
        self.checkout = type("C", (), {"Session": _Session})


@pytest.fixture
def seeded(engine, db, monkeypatch):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)
    # Price ids the apply path looks up.
    for k, v in (("STRIPE_PRICE_VERIFIED_MONTHLY", "price_verified_monthly"),
                 ("STRIPE_PRICE_GOVERNED_MONTHLY", "price_governed_monthly"),
                 ("STRIPE_PRICE_SOVEREIGN_MONTHLY", "price_sovereign_monthly")):
        monkeypatch.setenv(k, v)

    def _clean():
        db.query(BillingDowngradeRequest).delete()
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        for o in (ORG_A, ORG_B, ORG_INT):
            db.query(User).filter(User.organization_id == o).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B, ORG_INT])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="Downgrade A", billing_type="customer"))
    db.add(Organization(id=ORG_B, name="Downgrade B", billing_type="customer"))
    db.add(Organization(id=ORG_INT, name="Temveris", billing_type="internal"))
    verified = BillingPlan(code="verified", name="Verified", tier_order=1,
                          monthly_price_per_user=8.0, annual_price_per_user=76.80,
                          user_limit=25)
    governed = BillingPlan(code="governed", name="Governed", tier_order=2,
                          monthly_price_per_user=15.0, annual_price_per_user=144.0,
                          user_limit=100)
    sovereign = BillingPlan(code="sovereign", name="Sovereign", tier_order=3,
                           monthly_price_per_user=25.0, annual_price_per_user=240.0,
                           user_limit=None)
    db.add_all([verified, governed, sovereign])
    db.flush()
    db.add(BillingSubscription(organization_id=ORG_A, plan_id=governed.id,
                               billing_cycle="monthly", subscription_status="active",
                               stripe_subscription_id=SUB_A, stripe_customer_id=CUS_A,
                               active_users=3, currency="USD"))
    db.add(BillingSubscription(organization_id=ORG_INT, plan_id=governed.id,
                               billing_cycle="monthly", subscription_status="active",
                               stripe_subscription_id="sub_int", stripe_customer_id="cus_int",
                               active_users=8, currency="USD"))
    for i in range(3):
        db.add(User(name=f"g{i}", email=f"g{i}@dg.test", organization_id=ORG_A,
                    role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    yield {"verified": verified, "governed": governed, "sovereign": sovereign}
    _clean()


def _actor(db, org, email, role="admin"):
    u = User(name="A", email=email, organization_id=org, role=role,
             billing_state=worker_state.ACTIVE_BILLABLE, is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _client(db, actor, router=None):
    from app.auth.service import get_current_user
    app = FastAPI(); app.include_router(router or billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _sa_client(db, actor):
    from app.auth.service import get_current_user, require_super_admin
    from app.super_admin.router import router as sa_router
    app = FastAPI(); app.include_router(sa_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[require_super_admin] = lambda: actor
    return TestClient(app)


def _request_row(db, seeded, *, target="verified", org=ORG_A, effective=None,
                 status="pending"):
    req = BillingDowngradeRequest(
        organization_id=org,
        current_plan_id=seeded["governed"].id,
        requested_plan_id=seeded[target].id,
        reason="cost reduction", acknowledged_risk=True, status=status,
        preferred_effective_date=effective,
    )
    db.add(req); db.commit(); db.refresh(req)
    return req


def _bulk_billable(db, org, n):
    for i in range(n):
        db.add(User(name=f"b{i}", email=f"b{i}-{org}@dg.test", organization_id=org,
                    role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# /upgrade must refuse a lower tier
# ══════════════════════════════════════════════════════════════════════════════
def test_upgrade_refuses_a_lower_tier(db, seeded):
    """The hole that made the review table decorative."""
    actor = _actor(db, ORG_A, "lower@dg.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/upgrade", json={"plan_code": "verified",
                                          "billing_cycle": "monthly"})
    assert r.status_code == 409
    detail = r.json()["detail"].lower()
    assert "downgrade" in detail and "request" in detail


def test_upgrade_still_allows_a_higher_tier(db, seeded, monkeypatch):
    stub = StubStripe()
    monkeypatch.setattr(billing_router.stripe, "Subscription", stub.Subscription)
    actor = _actor(db, ORG_A, "higher@dg.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/upgrade", json={"plan_code": "sovereign",
                                          "billing_cycle": "monthly"})
    assert r.status_code == 200, r.text
    assert len(stub.modify_calls) == 1
    assert stub.create_calls == []


def test_upgrade_refuses_a_plan_that_cannot_hold_the_headcount(db, seeded):
    """Same tier direction is irrelevant — capacity is checked regardless."""
    _bulk_billable(db, ORG_A, 30)                # 33 billable
    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    sub.plan_id = seeded["verified"].id          # pretend currently on Verified
    db.commit()
    actor = _actor(db, ORG_A, "cap@dg.test")
    with _client(db, actor) as c:
        # Verified -> Verified annual: not a downgrade, but Verified holds only 25.
        r = c.post(f"{API}/upgrade", json={"plan_code": "verified",
                                          "billing_cycle": "annual"})
    assert r.status_code == 409
    assert "25" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════════
# Request creation
# ══════════════════════════════════════════════════════════════════════════════
def test_a_downgrade_request_is_recorded_as_pending(db, seeded):
    actor = _actor(db, ORG_A, "req@dg.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/downgrade-request",
                   json={"requested_plan_code": "verified",
                         "reason": "reducing team size next quarter",
                         "acknowledged_risk": True})
    assert r.status_code == 200
    rows = db.query(BillingDowngradeRequest).filter_by(organization_id=ORG_A).all()
    assert len(rows) == 1 and rows[0].status == "pending"


# ══════════════════════════════════════════════════════════════════════════════
# Super-admin approval
# ══════════════════════════════════════════════════════════════════════════════
def test_only_super_admin_can_list_requests(db, seeded):
    from app.super_admin.router import router as sa_router
    from app.auth.service import get_current_user
    _request_row(db, seeded)
    admin = _actor(db, ORG_A, "notsa@dg.test", role="admin")
    app = FastAPI(); app.include_router(sa_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin      # real guard active
    with TestClient(app) as c:
        assert c.get(f"{SA}/billing/downgrade-requests").status_code == 403


def test_super_admin_sees_the_capacity_blocker_before_approving(db, seeded):
    _bulk_billable(db, ORG_A, 30)                # 33 billable, Verified holds 25
    req = _request_row(db, seeded, target="verified")
    sa = _actor(db, ORG_A, "sa1@dg.test", role="super_admin")
    with _sa_client(db, sa) as c:
        body = c.get(f"{SA}/billing/downgrade-requests").json()
    item = next(i for i in body["items"] if i["id"] == req.id)
    assert item["capacity_blocked"] is True
    # Derived, not hardcoded: the reviewer's own account is billable too, so a literal
    # count here would silently drift with the fixture.
    billable = billing_service.count_billable_seats(db, ORG_A)
    assert billable > 25
    assert item["billable_users"] == billable
    assert str(billable) in item["capacity_detail"]
    assert "25" in item["capacity_detail"]


def test_approval_is_refused_when_capacity_is_exceeded(db, seeded):
    _bulk_billable(db, ORG_A, 30)
    req = _request_row(db, seeded, target="verified")
    sa = _actor(db, ORG_A, "sa2@dg.test", role="super_admin")
    with _sa_client(db, sa) as c:
        r = c.post(f"{SA}/billing/downgrade-requests/{req.id}/approve", json={})
    assert r.status_code == 409
    db.expire_all()
    assert db.query(BillingDowngradeRequest).filter_by(id=req.id).first().status == "pending"


def test_approval_records_the_reviewer(db, seeded):
    req = _request_row(db, seeded)
    sa = _actor(db, ORG_A, "sa3@dg.test", role="super_admin")
    with _sa_client(db, sa) as c:
        r = c.post(f"{SA}/billing/downgrade-requests/{req.id}/approve",
                   json={"notes": "agreed with the customer"})
    assert r.status_code == 200
    db.expire_all()
    row = db.query(BillingDowngradeRequest).filter_by(id=req.id).first()
    assert row.status == "approved"
    assert row.reviewed_by_user_id == sa.id
    assert row.review_notes == "agreed with the customer"


def test_rejection_closes_the_request(db, seeded):
    req = _request_row(db, seeded)
    sa = _actor(db, ORG_A, "sa4@dg.test", role="super_admin")
    with _sa_client(db, sa) as c:
        r = c.post(f"{SA}/billing/downgrade-requests/{req.id}/reject",
                   json={"notes": "duplicate"})
    assert r.status_code == 200
    db.expire_all()
    assert db.query(BillingDowngradeRequest).filter_by(id=req.id).first().status == "rejected"


def test_an_already_closed_request_cannot_be_approved(db, seeded):
    req = _request_row(db, seeded, status="rejected")
    sa = _actor(db, ORG_A, "sa5@dg.test", role="super_admin")
    with _sa_client(db, sa) as c:
        assert c.post(f"{SA}/billing/downgrade-requests/{req.id}/approve",
                      json={}).status_code == 409


# ══════════════════════════════════════════════════════════════════════════════
# Application
# ══════════════════════════════════════════════════════════════════════════════
def test_applying_modifies_the_existing_subscription_and_creates_none(db, seeded):
    """Phase 2.1's guarantee must hold on the downgrade path too."""
    req = _request_row(db, seeded, target="verified", status="approved")
    stub = StubStripe()
    result = billing_downgrades.apply_request(db, req.id, stripe_client=stub)

    assert result["status"] == "applied"
    assert len(stub.modify_calls) == 1
    assert stub.create_calls == []
    sid, kw = stub.modify_calls[0]
    assert sid == SUB_A                                  # the EXISTING subscription
    assert kw["items"][0]["price"] == "price_verified_monthly"
    db.expire_all()
    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    assert sub.plan_id == seeded["verified"].id
    assert sub.stripe_subscription_id == SUB_A           # never reassigned


def test_a_pending_request_cannot_be_applied(db, seeded):
    req = _request_row(db, seeded, status="pending")
    with pytest.raises(billing_downgrades.DowngradeError) as exc:
        billing_downgrades.apply_request(db, req.id, stripe_client=StubStripe())
    assert exc.value.status_code == 409
    assert "approved first" in str(exc.value)


def test_applying_twice_is_idempotent(db, seeded):
    req = _request_row(db, seeded, target="verified", status="approved")
    stub = StubStripe()
    billing_downgrades.apply_request(db, req.id, stripe_client=stub)
    billing_downgrades.apply_request(db, req.id, stripe_client=stub)
    assert len(stub.modify_calls) == 1, "a second call must not change Stripe again"


def test_capacity_is_rechecked_at_apply_time(db, seeded):
    """Approved when it fit; headcount grew before the effective date."""
    req = _request_row(db, seeded, target="verified", status="approved")
    _bulk_billable(db, ORG_A, 30)
    stub = StubStripe()
    with pytest.raises(billing_downgrades.DowngradeError) as exc:
        billing_downgrades.apply_request(db, req.id, stripe_client=stub)
    assert exc.value.status_code == 409
    assert stub.modify_calls == [], "Stripe must not be touched"


def test_apply_due_only_touches_approved_requests_past_their_date(db, seeded):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=5)
    due = _request_row(db, seeded, target="verified", status="approved", effective=past)
    not_yet = _request_row(db, seeded, target="verified", status="approved", effective=future)
    pending = _request_row(db, seeded, target="verified", status="pending", effective=past)

    stub = StubStripe()
    tally = billing_downgrades.apply_due(db, stripe_client=stub)
    assert tally["applied"] == 1
    db.expire_all()
    get = lambda i: db.query(BillingDowngradeRequest).filter_by(id=i).first().status
    assert get(due.id) == "applied"
    assert get(not_yet.id) == "approved"
    assert get(pending.id) == "pending", "a pending request must never be auto-applied"


def test_apply_due_never_auto_applies_pending_rows(db, seeded):
    """The 21 pre-existing production rows are all pending and must stay untouched."""
    for _ in range(5):
        _request_row(db, seeded, target="verified", status="pending",
                     effective=datetime.now(timezone.utc) - timedelta(days=90))
    stub = StubStripe()
    tally = billing_downgrades.apply_due(db, stripe_client=stub)
    assert tally["applied"] == 0
    assert stub.modify_calls == []
    assert {r.status for r in db.query(BillingDowngradeRequest).all()} == {"pending"}


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation / internal tenant
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_downgrade_is_refused(db, seeded):
    """Refused at apply. Note this is defence in depth: change_stripe_plan carries its
    own internal-tenant guard from Phase 7, so either layer alone would stop it."""
    req = _request_row(db, seeded, target="verified", org=ORG_INT, status="approved")
    with pytest.raises(billing_downgrades.DowngradeError) as exc:
        billing_downgrades.apply_request(db, req.id, stripe_client=StubStripe())
    assert exc.value.status_code == 403
    assert "internal" in str(exc.value).lower()


def test_internal_tenant_downgrade_is_refused_at_approval_too(db, seeded):
    """Isolates the downgrades-module guard.

    Approval never reaches change_stripe_plan, so only ``_validate``'s internal-tenant
    check can refuse here — proving that layer is load-bearing on its own rather than
    being masked by the Stripe-facing guard.
    """
    req = _request_row(db, seeded, target="verified", org=ORG_INT, status="pending")
    sa = _actor(db, ORG_A, "sa-int@dg.test", role="super_admin")
    with _sa_client(db, sa) as c:
        r = c.post(f"{SA}/billing/downgrade-requests/{req.id}/approve", json={})
    assert r.status_code == 403
    assert "internal" in r.json()["detail"].lower()
    db.expire_all()
    assert db.query(BillingDowngradeRequest).filter_by(id=req.id).first().status == "pending"


def test_listing_can_be_scoped_to_one_organization(db, seeded):
    a = _request_row(db, seeded, org=ORG_A)
    b = _request_row(db, seeded, org=ORG_B)
    sa = _actor(db, ORG_A, "sa6@dg.test", role="super_admin")
    with _sa_client(db, sa) as c:
        body = c.get(f"{SA}/billing/downgrade-requests",
                     params={"organization_id": ORG_A}).json()
    ids = {i["id"] for i in body["items"]}
    assert a.id in ids and b.id not in ids


def test_a_downgrade_request_is_scoped_to_the_callers_org(db, seeded):
    """The customer endpoint must not be able to file against another tenant."""
    actor = _actor(db, ORG_B, "scoped@dg.test")
    with _client(db, actor) as c:
        c.post(f"{API}/downgrade-request",
               json={"requested_plan_code": "verified", "reason": "testing scope",
                     "acknowledged_risk": True})
    rows = db.query(BillingDowngradeRequest).all()
    assert all(r.organization_id == ORG_B for r in rows)
