"""Phase 2.1 — plan changes modify the existing Stripe subscription.

The defect: ``/upgrade`` always opened a Checkout Session in
``mode='subscription'``, so every plan change created a *second* Stripe
subscription that billed alongside the first, while the local row's
``stripe_subscription_id`` was overwritten and the original was forgotten. One
customer accumulated three concurrent subscriptions ($100 + $15 + $25).

These tests pin the corrected lifecycle: one organization, one Stripe
subscription, modified in place, with the subscription and customer ids
preserved across every transition.

Stripe is stubbed — no network, no live resources.
"""

from __future__ import annotations

import pytest

from app.billing import service as billing_service
from app.billing import worker_state
from database.models import (
    BillingPlan, BillingSubscription, Organization, User,
)

ORG_A = 8401
ORG_B = 8402

SUB_A = "sub_orgA_original"
CUS_A = "cus_orgA"
ITEM_A = "si_orgA_item"

PRICE = {
    ("verified", "monthly"): "price_verified_monthly",
    ("verified", "annual"): "price_verified_annual",
    ("governed", "monthly"): "price_governed_monthly",
    ("governed", "annual"): "price_governed_annual",
    ("sovereign", "monthly"): "price_sovereign_monthly",
    ("sovereign", "annual"): "price_sovereign_annual",
}


# ── Stripe stub ───────────────────────────────────────────────────────────────
class StubStripe:
    """Records calls so tests can assert *which* Stripe operation was used."""

    def __init__(self, *, status="active", customer=CUS_A, items=None, fail_on=None):
        self._status = status
        self._customer = customer
        self._items = items if items is not None else [{"id": ITEM_A}]
        self._fail_on = fail_on            # "retrieve" | "modify" | None
        self.retrieve_calls = []
        self.modify_calls = []
        self.session_create_calls = []

        outer = self

        class _Subscription:
            @staticmethod
            def retrieve(sid):
                outer.retrieve_calls.append(sid)
                if outer._fail_on == "retrieve":
                    raise RuntimeError("stripe unreachable")
                return {
                    "id": sid,
                    "customer": outer._customer,
                    "status": outer._status,
                    "items": {"data": outer._items},
                }

            @staticmethod
            def modify(sid, **kwargs):
                outer.modify_calls.append((sid, kwargs))
                if outer._fail_on == "modify":
                    raise RuntimeError("card_declined")
                return {"id": sid, "status": "active"}

        class _Session:
            @staticmethod
            def create(**kwargs):
                outer.session_create_calls.append(kwargs)
                return type("S", (), {"id": "cs_stub", "url": "https://stub/checkout"})()

        class _Checkout:
            Session = _Session

        self.Subscription = _Subscription
        self.checkout = _Checkout


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def billing_env(engine, db):
    for model in (Organization, BillingPlan, BillingSubscription):
        model.__table__.create(engine, checkfirst=True)

    def _clean():
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add_all([Organization(id=ORG_A, name="Org A"), Organization(id=ORG_B, name="Org B")])
    plans = {}
    for i, (code, monthly, annual, cap) in enumerate(
        [("verified", 8.0, 79.0, 25), ("governed", 15.0, 149.0, 100),
         ("sovereign", 25.0, 240.0, None)], start=1
    ):
        p = BillingPlan(code=code, name=code.title(), tier_order=i,
                        monthly_price_per_user=monthly, annual_price_per_user=annual,
                        user_limit=cap)
        db.add(p)
        plans[code] = p
    db.flush()

    sub_a = BillingSubscription(
        organization_id=ORG_A, plan_id=plans["governed"].id, billing_cycle="monthly",
        subscription_status="active", stripe_customer_id=CUS_A,
        stripe_subscription_id=SUB_A, billable_users=1, active_users=1,
    )
    sub_b = BillingSubscription(
        organization_id=ORG_B, plan_id=plans["verified"].id, billing_cycle="monthly",
        subscription_status="active", stripe_customer_id="cus_orgB",
        stripe_subscription_id="sub_orgB", billable_users=1, active_users=1,
    )
    db.add_all([sub_a, sub_b])
    db.commit()
    yield {"plans": plans, "sub_a": sub_a, "sub_b": sub_b}
    _clean()


def _sub(db, org):
    return db.query(BillingSubscription).filter_by(organization_id=org).one()


# ══════════════════════════════════════════════════════════════════════════════
# The core guarantee: modify, never duplicate
# ══════════════════════════════════════════════════════════════════════════════
def test_existing_subscription_is_modified_not_duplicated(db, billing_env):
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[("sovereign", "monthly")], seats=4,
        target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
        stripe_client=stub,
    )
    assert len(stub.modify_calls) == 1, "existing subscription must be modified"
    assert stub.session_create_calls == [], "no second subscription may be created"


def test_stripe_subscription_id_is_preserved(db, billing_env):
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[("sovereign", "annual")], seats=2,
        target_plan=billing_env["plans"]["sovereign"], billing_cycle="annual",
        stripe_client=stub,
    )
    row = _sub(db, ORG_A)
    assert row.stripe_subscription_id == SUB_A, "the original subscription id is authoritative"
    assert stub.modify_calls[0][0] == SUB_A


def test_stripe_customer_id_is_preserved(db, billing_env):
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[("verified", "monthly")], seats=1,
        target_plan=billing_env["plans"]["verified"], billing_cycle="monthly",
        stripe_client=stub,
    )
    assert _sub(db, ORG_A).stripe_customer_id == CUS_A


def test_modify_sends_correct_price_quantity_and_item(db, billing_env):
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[("governed", "annual")], seats=7,
        target_plan=billing_env["plans"]["governed"], billing_cycle="annual",
        stripe_client=stub,
    )
    _, kwargs = stub.modify_calls[0]
    assert kwargs["items"] == [
        {"id": ITEM_A, "price": PRICE[("governed", "annual")], "quantity": 7}
    ]


def test_proration_and_payment_behaviour_are_explicit(db, billing_env):
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[("sovereign", "monthly")], seats=1,
        target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
        stripe_client=stub,
    )
    _, kwargs = stub.modify_calls[0]
    assert kwargs["proration_behavior"] == "create_prorations"
    assert kwargs["payment_behavior"] == "error_if_incomplete"
    assert kwargs["metadata"]["organization_id"] == str(ORG_A)


@pytest.mark.parametrize("code,cycle", [
    ("governed", "monthly"), ("governed", "annual"),
    ("sovereign", "monthly"), ("sovereign", "annual"),
    ("verified", "monthly"), ("verified", "annual"),
])
def test_all_plan_and_cycle_transitions_use_the_right_price(db, billing_env, code, cycle):
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[(code, cycle)], seats=3,
        target_plan=billing_env["plans"][code], billing_cycle=cycle,
        stripe_client=stub,
    )
    _, kwargs = stub.modify_calls[0]
    assert kwargs["items"][0]["price"] == PRICE[(code, cycle)]
    row = _sub(db, ORG_A)
    assert row.billing_cycle == cycle
    assert row.plan_id == billing_env["plans"][code].id
    assert row.stripe_subscription_id == SUB_A          # never replaced


# ══════════════════════════════════════════════════════════════════════════════
# Failure paths must leave the database untouched
# ══════════════════════════════════════════════════════════════════════════════
def test_failed_stripe_modify_leaves_local_subscription_unchanged(db, billing_env):
    before = _sub(db, ORG_A)
    plan_before, cycle_before, status_before = (
        before.plan_id, before.billing_cycle, before.subscription_status)

    stub = StubStripe(fail_on="modify")
    with pytest.raises(billing_service.StripePlanChangeError) as exc:
        billing_service.change_stripe_plan(
            db, before,
            price_id=PRICE[("sovereign", "monthly")], seats=9,
            target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
            stripe_client=stub,
        )
    assert exc.value.status_code == 402

    db.rollback()
    after = _sub(db, ORG_A)
    assert after.plan_id == plan_before
    assert after.billing_cycle == cycle_before
    assert after.subscription_status == status_before
    assert after.stripe_subscription_id == SUB_A


def test_stripe_unreachable_leaves_local_subscription_unchanged(db, billing_env):
    stub = StubStripe(fail_on="retrieve")
    with pytest.raises(billing_service.StripePlanChangeError) as exc:
        billing_service.change_stripe_plan(
            db, billing_env["sub_a"],
            price_id=PRICE[("sovereign", "monthly")], seats=1,
            target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
            stripe_client=stub,
        )
    assert exc.value.status_code == 502
    assert stub.modify_calls == [], "must not attempt a modify it cannot verify"


def test_subscription_belonging_to_another_customer_is_rejected(db, billing_env):
    """If the row's ids disagree with Stripe, refuse — never modify another
    tenant's subscription."""
    stub = StubStripe(customer="cus_someone_else")
    with pytest.raises(billing_service.StripePlanChangeError) as exc:
        billing_service.change_stripe_plan(
            db, billing_env["sub_a"],
            price_id=PRICE[("sovereign", "monthly")], seats=1,
            target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
            stripe_client=stub,
        )
    assert exc.value.status_code == 409
    assert stub.modify_calls == []


@pytest.mark.parametrize("status", ["canceled", "incomplete_expired", "unpaid"])
def test_non_modifiable_subscription_is_rejected(db, billing_env, status):
    stub = StubStripe(status=status)
    with pytest.raises(billing_service.StripePlanChangeError) as exc:
        billing_service.change_stripe_plan(
            db, billing_env["sub_a"],
            price_id=PRICE[("sovereign", "monthly")], seats=1,
            target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
            stripe_client=stub,
        )
    assert exc.value.status_code == 409
    assert stub.modify_calls == [], "must not create a replacement subscription"


def test_multi_item_subscription_is_rejected_rather_than_guessed(db, billing_env):
    stub = StubStripe(items=[{"id": "si_one"}, {"id": "si_two"}])
    with pytest.raises(billing_service.StripePlanChangeError):
        billing_service.change_stripe_plan(
            db, billing_env["sub_a"],
            price_id=PRICE[("sovereign", "monthly")], seats=1,
            target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
            stripe_client=stub,
        )
    assert stub.modify_calls == []


def test_no_stripe_subscription_cannot_be_modified(db, billing_env):
    """A first-time buyer has no subscription to modify — the router sends them
    down the Checkout path instead."""
    sub_b = billing_env["sub_b"]
    sub_b.stripe_subscription_id = None
    sub_b.stripe_customer_id = None
    db.commit()

    stub = StubStripe()
    with pytest.raises(billing_service.StripePlanChangeError) as exc:
        billing_service.change_stripe_plan(
            db, sub_b,
            price_id=PRICE[("governed", "monthly")], seats=1,
            target_plan=billing_env["plans"]["governed"], billing_cycle="monthly",
            stripe_client=stub,
        )
    assert exc.value.status_code == 409
    assert stub.retrieve_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
def test_changing_org_a_never_touches_org_b(db, billing_env):
    b_before = _sub(db, ORG_B)
    b_plan, b_sub_id, b_cus = (
        b_before.plan_id, b_before.stripe_subscription_id, b_before.stripe_customer_id)

    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[("sovereign", "annual")], seats=5,
        target_plan=billing_env["plans"]["sovereign"], billing_cycle="annual",
        stripe_client=stub,
    )

    # Only org A's Stripe subscription was addressed.
    assert stub.modify_calls[0][0] == SUB_A
    assert stub.retrieve_calls == [SUB_A]

    b_after = _sub(db, ORG_B)
    assert (b_after.plan_id, b_after.stripe_subscription_id, b_after.stripe_customer_id) \
        == (b_plan, b_sub_id, b_cus)


def test_each_org_keeps_exactly_one_subscription_after_a_change(db, billing_env):
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, billing_env["sub_a"],
        price_id=PRICE[("sovereign", "monthly")], seats=1,
        target_plan=billing_env["plans"]["sovereign"], billing_cycle="monthly",
        stripe_client=stub,
    )
    assert db.query(BillingSubscription).filter_by(organization_id=ORG_A).count() == 1
    assert db.query(BillingSubscription).filter_by(organization_id=ORG_B).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# Webhook / confirm must not replace a different subscription id (§7 minimum)
# ══════════════════════════════════════════════════════════════════════════════
def test_confirm_and_webhook_never_overwrite_a_different_subscription_id():
    """Both handlers now fill in a missing id but refuse to replace a different
    one — replacing is how a duplicate became the record of truth."""
    import pathlib
    billing = pathlib.Path(__file__).resolve().parent.parent / "app" / "billing"
    # Phase 3 moved the webhook handler out of router.py into webhooks.py, so the
    # two assignment sites now live in different files. Check both.
    router_src = (billing / "router.py").read_text(encoding="utf-8")
    webhook_src = (billing / "webhooks.py").read_text(encoding="utf-8")

    for name, src in (("router.py", router_src), ("webhooks.py", webhook_src)):
        stripped = src.replace(
            "if stripe_sub_id and not sub.stripe_subscription_id:\n"
            "        sub.stripe_subscription_id = stripe_sub_id\n", ""
        ).replace(
            "if stripe_sub_id and not sub.stripe_subscription_id:\n"
            "                    sub.stripe_subscription_id = stripe_sub_id\n", ""
        )
        assert "sub.stripe_subscription_id = stripe_sub_id\n" not in stripped, (
            f"an unguarded overwrite of stripe_subscription_id remains in {name}")
        assert "not sub.stripe_subscription_id" in src, (
            f"{name} lost the fill-in-only guard")

    # /upgrade/confirm logs and continues; the webhook now refuses outright,
    # recording the event rather than silently carrying on.
    assert "refusing to overwrite subscription id" in router_src
    assert "would_replace_subscription_id" in webhook_src


# ══════════════════════════════════════════════════════════════════════════════
# Router branch: existing subscriber → modify; first purchase → Checkout Session
# ══════════════════════════════════════════════════════════════════════════════
def _upgrade_client(db, actor):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth.service import get_current_user
    from app.billing.router import router as billing_router
    from database.database import get_db

    app = FastAPI()
    app.include_router(billing_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _owner(db, org, email):
    u = User(name="Owner", email=email, organization_id=org, role="owner",
             billing_state=worker_state.ACTIVE_BILLABLE, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_router_modifies_when_a_subscription_already_exists(db, billing_env, monkeypatch):
    """The whole point of Phase 2.1: an existing subscriber must not be sent to
    Checkout, because that mints a second subscription."""
    monkeypatch.setenv("STRIPE_PRICE_SOVEREIGN_MONTHLY", PRICE[("sovereign", "monthly")])

    called = {}

    def _fake_change(db_, sub_, **kwargs):
        called["args"] = kwargs
        called["org"] = sub_.organization_id
        sub_.plan_id = billing_env["plans"]["sovereign"].id
        sub_.billing_cycle = "monthly"
        db_.commit()
        return sub_

    monkeypatch.setattr(billing_service, "change_stripe_plan", _fake_change)

    session_created = []
    import app.billing.router as br
    monkeypatch.setattr(br.stripe.checkout.Session, "create",
                        lambda **kw: session_created.append(kw))

    actor = _owner(db, ORG_A, "owner-a@plan.test")
    with _upgrade_client(db, actor) as c:
        r = c.post("/api/admin/billing/upgrade",
                   json={"plan_code": "sovereign", "billing_cycle": "monthly"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert "url" not in body, "must NOT redirect an existing subscriber to Checkout"
    assert body["ok"] is True
    assert called["org"] == ORG_A
    assert session_created == [], "no Checkout Session may be created for a plan change"


def test_router_uses_checkout_session_for_a_first_purchase(db, billing_env, monkeypatch):
    """Unchanged behaviour: with no Stripe subscription yet, Checkout is correct."""
    monkeypatch.setenv("STRIPE_PRICE_GOVERNED_MONTHLY", PRICE[("governed", "monthly")])

    sub_b = billing_env["sub_b"]
    sub_b.stripe_subscription_id = None
    sub_b.stripe_customer_id = None
    db.commit()

    modify_called = []
    monkeypatch.setattr(billing_service, "change_stripe_plan",
                        lambda *a, **k: modify_called.append(k))

    created = []
    import app.billing.router as br

    def _fake_create(**kw):
        created.append(kw)
        return type("S", (), {"id": "cs_test_stub", "url": "https://stub/checkout"})()

    monkeypatch.setattr(br.stripe.checkout.Session, "create", _fake_create)

    actor = _owner(db, ORG_B, "owner-b@plan.test")
    with _upgrade_client(db, actor) as c:
        r = c.post("/api/admin/billing/upgrade",
                   json={"plan_code": "governed", "billing_cycle": "monthly"})

    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://stub/checkout"
    assert len(created) == 1, "first purchase still goes through Checkout"
    assert created[0]["mode"] == "subscription"
    assert modify_called == [], "nothing to modify on a first purchase"
