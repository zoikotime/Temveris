"""Phase 7 — cards come from Stripe, and Temveris never sees a card number.

``POST /payment-method`` used to accept a brand chosen from a dropdown, four digits
typed by hand and an expiry date, then store the row with ``provider="stripe"`` and
``verification_status="verified"``. Stripe knew nothing about it and it could not be
charged — while the UI told the customer their card was tokenized through Stripe.

Meanwhile real cards existed: every customer who completed Checkout has one attached
to their Stripe customer and set as ``invoice_settings.default_payment_method``.
``billing_payment_methods`` held zero rows, so the UI showed "no card on file" to
customers whose card Stripe was already charging.

Cards are now collected by Stripe's hosted Setup Session and mirrored from Stripe,
keyed by ``stripe_payment_method_id``. Manual entry returns 410 Gone.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import payment_methods as billing_pm
from app.billing import router as billing_router
from app.billing import service as billing_service
from app.billing import worker_state
from database.database import get_db
from database.models import (
    BillingPaymentMethod, BillingPlan, BillingSubscription, Organization, User,
)

API = "/api/admin/billing"
ORG_A, ORG_B, ORG_INT = 8201, 8202, 8203
CUS_A, CUS_B, CUS_INT = "cus_pmA", "cus_pmB", "cus_pmINT"
SUB_A, SUB_B = "sub_pmA", "sub_pmB"

_JSX = (pathlib.Path(__file__).resolve().parents[2]
        / "frontend/src/components/BillingModule.jsx")


def _jsx(strip_comments=True):
    src = _JSX.read_text(encoding="utf-8")
    if strip_comments:
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    return src


def _pm(pid, *, customer=CUS_A, brand="visa", last4="4242", exp=(8, 2029),
        name="A Customer"):
    return {
        "id": pid, "object": "payment_method", "type": "card", "customer": customer,
        "card": {"brand": brand, "last4": last4, "exp_month": exp[0], "exp_year": exp[1]},
        "billing_details": {"name": name, "address": {"country": "GB"}},
    }


class StubStripe:
    """Mirrors real Stripe surface: PaymentMethod.list/detach + Customer.retrieve."""

    def __init__(self, pms_by_customer=None, defaults=None, fail=None):
        self.by_customer = pms_by_customer or {}
        self.defaults = defaults or {}
        self.detached = []
        self.list_calls = []
        outer = self

        class _PaymentMethod:
            @staticmethod
            def list(customer=None, type=None):
                outer.list_calls.append(customer)
                if fail == "list":
                    raise RuntimeError("stripe unreachable")
                return {"data": outer.by_customer.get(customer, [])}

            @staticmethod
            def detach(pm_id):
                if fail == "detach":
                    raise RuntimeError("stripe detach failed")
                outer.detached.append(pm_id)
                for cust, pms in outer.by_customer.items():
                    outer.by_customer[cust] = [p for p in pms if p["id"] != pm_id]
                return {"id": pm_id, "customer": None}

        class _Customer:
            @staticmethod
            def retrieve(cid):
                if fail == "customer":
                    raise RuntimeError("stripe unreachable")
                return {"id": cid,
                        "invoice_settings": {"default_payment_method": outer.defaults.get(cid)}}

        self.PaymentMethod = _PaymentMethod
        self.Customer = _Customer


class ExplodingStripe:
    def __getattr__(self, n):
        raise AssertionError(f"Stripe called for a non-billable tenant: .{n}")


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingPaymentMethod).delete()
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        for o in (ORG_A, ORG_B, ORG_INT):
            db.query(User).filter(User.organization_id == o).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B, ORG_INT])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="PM A", billing_type="customer"))
    db.add(Organization(id=ORG_B, name="PM B", billing_type="customer"))
    db.add(Organization(id=ORG_INT, name="Owner", billing_type="internal"))
    plan = BillingPlan(code="governed", name="Governed", tier_order=2,
                       monthly_price_per_user=15.0, annual_price_per_user=144.0,
                       user_limit=100)
    db.add(plan); db.flush()
    for org, sid, cid in ((ORG_A, SUB_A, CUS_A), (ORG_B, SUB_B, CUS_B),
                          (ORG_INT, "sub_int", CUS_INT)):
        db.add(BillingSubscription(organization_id=org, plan_id=plan.id,
                                   billing_cycle="monthly", subscription_status="active",
                                   stripe_subscription_id=sid, stripe_customer_id=cid,
                                   active_users=3, currency="USD"))
    db.commit()
    yield plan
    _clean()


def _actor(db, org, email, role="admin"):
    u = User(name="A", email=email, organization_id=org, role=role,
             billing_state=worker_state.ACTIVE_BILLABLE, is_active=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _client(db, actor):
    from app.auth.service import get_current_user
    app = FastAPI(); app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _rows(db, org):
    return db.query(BillingPaymentMethod).filter_by(organization_id=org).all()


# ══════════════════════════════════════════════════════════════════════════════
# Mirror
# ══════════════════════════════════════════════════════════════════════════════
def test_card_is_mirrored_from_stripe(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_1", brand="mastercard", last4="4444", exp=(3, 2030))]},
                      defaults={CUS_A: "pm_1"})
    res = billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    assert res["status"] == "synced" and res["created"] == 1

    row = _rows(db, ORG_A)[0]
    assert row.stripe_payment_method_id == "pm_1"
    assert row.token_reference == "pm_1"
    assert row.brand == "mastercard"
    assert row.last_four == "4444"
    assert row.expiry_month == 3 and row.expiry_year == 2030
    assert row.is_default is True
    assert row.provider == "stripe"


def test_default_flag_comes_from_stripe_invoice_settings(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_1"), _pm("pm_2", last4="1111")]},
                      defaults={CUS_A: "pm_2"})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    by_id = {r.stripe_payment_method_id: r for r in _rows(db, ORG_A)}
    assert by_id["pm_2"].is_default is True
    assert by_id["pm_1"].is_default is False


def test_resync_is_idempotent(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_1")]}, defaults={CUS_A: "pm_1"})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    res = billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    assert res["created"] == 0 and res["updated"] == 1
    assert len(_rows(db, ORG_A)) == 1


def test_card_detached_at_stripe_disappears_locally(db, seeded):
    """Otherwise the UI keeps claiming a card is on file after it was removed."""
    stub = StubStripe({CUS_A: [_pm("pm_gone")]}, defaults={CUS_A: "pm_gone"})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    assert len(_rows(db, ORG_A)) == 1

    stub.by_customer[CUS_A] = []
    res = billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    assert res["removed"] == 1
    assert _rows(db, ORG_A) == []


def test_legacy_hand_entered_rows_are_left_alone(db, seeded):
    legacy = BillingPaymentMethod(organization_id=ORG_A, provider="stripe", brand="visa",
                                 last_four="9999", verification_status="verified")
    db.add(legacy); db.commit()
    lid = legacy.id
    billing_pm.sync_org_payment_methods(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [_pm("pm_real")]}))
    db.expire_all()
    assert db.query(BillingPaymentMethod).filter_by(id=lid).first() is not None
    assert len(_rows(db, ORG_A)) == 2


def test_stripe_objects_are_coerced_to_json_serialisable_values(db, seeded):
    """Regression: real Stripe returns ``billing_details.address`` as an ``Address``
    object, not a dict, and assigning it to the JSON column raised
    ``TypeError: Object of type Address is not JSON serializable`` at INSERT.

    A hand-built dict fixture cannot catch that, so this test builds a genuine
    StripeObject — the shape the mirror actually receives in production.
    """
    from stripe import StripeObject

    addr = StripeObject.construct_from(
        {"city": "London", "country": "GB", "line1": "1 Test St",
         "line2": None, "postal_code": "E1 6AN", "state": None}, "sk_test_x")
    details = StripeObject.construct_from({"name": "Real Customer", "address": addr},
                                          "sk_test_x")
    pm = StripeObject.construct_from(
        {"id": "pm_stripeobj", "type": "card", "customer": CUS_A,
         "card": {"brand": "visa", "last4": "4242", "exp_month": 8, "exp_year": 2029},
         "billing_details": details}, "sk_test_x")

    res = billing_pm.sync_org_payment_methods(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [pm]}, defaults={CUS_A: "pm_stripeobj"}))
    assert res["status"] == "synced", res
    row = _rows(db, ORG_A)[0]
    assert isinstance(row.billing_address_json, dict)
    assert row.billing_address_json["city"] == "London"
    assert row.billing_name == "Real Customer"


def test_no_raw_card_data_is_ever_stored(db, seeded):
    """Only brand/last4/expiry — never a number or CVC, because Stripe never sends them."""
    billing_pm.sync_org_payment_methods(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [_pm("pm_1")]}))
    cols = set(BillingPaymentMethod.__table__.columns.keys())
    for forbidden in ("card_number", "number", "cvc", "cvv", "security_code", "pan"):
        assert forbidden not in cols


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
def test_only_the_orgs_own_customer_is_queried(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_a")], CUS_B: [_pm("pm_b", customer=CUS_B)]})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    assert stub.list_calls == [CUS_A]
    assert _rows(db, ORG_B) == []


def test_foreign_customers_card_is_refused(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_foreign", customer="cus_someone_else")]})
    res = billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    assert res["skipped"] == 1 and res["created"] == 0
    assert _rows(db, ORG_A) == []


def test_resync_cannot_move_a_card_between_tenants(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_shared")]})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    stub.by_customer[CUS_B] = [_pm("pm_shared", customer=CUS_B)]
    res = billing_pm.sync_org_payment_methods(db, ORG_B, stripe_client=stub)
    assert res["skipped"] == 1
    db.expire_all()
    row = db.query(BillingPaymentMethod).filter_by(stripe_payment_method_id="pm_shared").first()
    assert row.organization_id == ORG_A


def test_internal_tenant_is_skipped_with_no_stripe_call(db, seeded):
    res = billing_pm.sync_org_payment_methods(db, ORG_INT, stripe_client=ExplodingStripe())
    assert res["status"] == "internal_tenant"


def test_sweep_excludes_internal_tenants(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_a")], CUS_B: [_pm("pm_b", customer=CUS_B)],
                       CUS_INT: [_pm("pm_int", customer=CUS_INT)]})
    tally = billing_pm.sync_all_orgs(db, stripe_client=stub)
    assert tally["checked"] == 2
    assert CUS_INT not in stub.list_calls
    assert _rows(db, ORG_INT) == []


@pytest.mark.parametrize("failure", ["list", "customer"])
def test_stripe_outage_is_reported_not_raised(db, seeded, failure):
    res = billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=StubStripe(fail=failure))
    assert res["status"] == "stripe_unavailable"
    assert _rows(db, ORG_A) == []


def test_commit_false_does_not_persist(db, seeded):
    stub = StubStripe({CUS_A: [_pm("pm_nc")]})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub, commit=False)
    assert len(_rows(db, ORG_A)) == 1
    db.rollback()
    assert _rows(db, ORG_A) == []


# ══════════════════════════════════════════════════════════════════════════════
# Manual entry is retired
# ══════════════════════════════════════════════════════════════════════════════
def test_manual_card_entry_returns_410(db, seeded):
    actor = _actor(db, ORG_A, "manual@a.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/payment-method", json={
            "payment_type": "card", "brand": "visa", "last_four": "4242",
            "expiry_month": 12, "expiry_year": 2030, "billing_name": "Faker",
        })
    assert r.status_code == 410
    assert "disabled" in r.json()["detail"].lower()
    assert _rows(db, ORG_A) == [], "nothing may be stored"


def test_manual_entry_is_refused_even_with_an_empty_body(db, seeded):
    actor = _actor(db, ORG_A, "manual2@a.test")
    with _client(db, actor) as c:
        assert c.post(f"{API}/payment-method").status_code == 410


# ══════════════════════════════════════════════════════════════════════════════
# Setup session + confirmation
# ══════════════════════════════════════════════════════════════════════════════
def _stub_checkout(monkeypatch, *, created=None, retrieve=None, create_raises=None):
    calls = {"create": []}

    class _Session:
        @staticmethod
        def create(**kw):
            calls["create"].append(kw)
            if create_raises:
                raise create_raises
            return type("S", (), {"url": "https://checkout.stripe.com/setup/test"})()

        @staticmethod
        def retrieve(sid):
            if retrieve is None:
                raise RuntimeError("no session")
            return retrieve

    monkeypatch.setattr(billing_router.stripe.checkout, "Session", _Session)
    return calls


class _Obj:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_setup_session_attaches_to_the_existing_customer(db, seeded, monkeypatch):
    """Without customer=, completing the session would create a NEW Stripe customer,
    orphaned from the one this org is billed against."""
    calls = _stub_checkout(monkeypatch)
    actor = _actor(db, ORG_A, "setup@a.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/payment-method/session")
    assert r.status_code == 200 and r.json()["session_url"]
    kw = calls["create"][0]
    assert kw["customer"] == CUS_A
    assert kw["mode"] == "setup"
    assert kw["client_reference_id"] == str(ORG_A)


def test_setup_session_failure_is_an_error_not_a_fake_success(db, seeded, monkeypatch):
    _stub_checkout(monkeypatch, create_raises=RuntimeError("stripe down"))
    actor = _actor(db, ORG_A, "setupfail@a.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/payment-method/session")
    assert r.status_code == 502
    assert r.json()["detail"] != ""
    assert "ok" not in r.json()


def test_setup_session_refused_for_internal_tenant(db, seeded, monkeypatch):
    _stub_checkout(monkeypatch)
    actor = _actor(db, ORG_INT, "setupint@owner.test")
    with _client(db, actor) as c:
        assert c.post(f"{API}/payment-method/session").status_code == 403


def test_setup_session_refused_without_a_billing_account(db, seeded, monkeypatch):
    _stub_checkout(monkeypatch)
    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    sub.stripe_customer_id = None
    db.commit()
    actor = _actor(db, ORG_A, "nocust@a.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/payment-method/session")
    assert r.status_code == 409


def test_confirm_mirrors_the_card(db, seeded, monkeypatch):
    session = _Obj(client_reference_id=str(ORG_A), customer=CUS_A, status="complete")
    _stub_checkout(monkeypatch, retrieve=session)
    stub = StubStripe({CUS_A: [_pm("pm_confirmed")]}, defaults={CUS_A: "pm_confirmed"})

    # Capture the real function first, then inject the stub client into it — patching
    # with a lambda that re-reads the patched name would recurse.
    real_sync = billing_pm.sync_org_payment_methods
    monkeypatch.setattr(
        billing_router.billing_payment_methods, "sync_org_payment_methods",
        lambda d, o, **kw: real_sync(d, o, stripe_client=stub,
                                     **{k: v for k, v in kw.items() if k != "stripe_client"}),
    )

    actor = _actor(db, ORG_A, "confirm@a.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/payment-method/confirm", params={"setup_intent": "cs_test_x"})
    assert r.status_code == 200, r.text
    assert [x.stripe_payment_method_id for x in _rows(db, ORG_A)] == ["pm_confirmed"]


def test_confirm_rejects_another_tenants_setup_session(db, seeded, monkeypatch):
    session = _Obj(client_reference_id=str(ORG_B), customer=CUS_B, status="complete")
    _stub_checkout(monkeypatch, retrieve=session)
    actor = _actor(db, ORG_A, "xtenant@a.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/payment-method/confirm", params={"setup_intent": "cs_other"})
    assert r.status_code == 403
    assert _rows(db, ORG_A) == []


def test_confirm_rejects_a_wrong_org_reference_even_when_the_customer_matches(db, seeded, monkeypatch):
    """Isolates the org-reference check.

    The two ownership checks are defence in depth, so each must be provably load-bearing
    on its own. Here client_reference_id names another org while the customer happens to
    be this org's — only the org-reference check can catch it.
    """
    session = _Obj(client_reference_id=str(ORG_B), customer=CUS_A, status="complete")
    _stub_checkout(monkeypatch, retrieve=session)
    actor = _actor(db, ORG_A, "orgref@a.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/payment-method/confirm", params={"setup_intent": "cs_orgref"})
    assert r.status_code == 403
    assert "another organization" in r.json()["detail"].lower()
    assert _rows(db, ORG_A) == []


def test_confirm_rejects_a_customer_mismatch(db, seeded, monkeypatch):
    """Right org reference, wrong billing account — still refused."""
    session = _Obj(client_reference_id=str(ORG_A), customer="cus_unexpected", status="complete")
    _stub_checkout(monkeypatch, retrieve=session)
    actor = _actor(db, ORG_A, "custmismatch@a.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/payment-method/confirm", params={"setup_intent": "cs_x"})
    assert r.status_code == 403


def test_confirm_rejects_an_incomplete_session(db, seeded, monkeypatch):
    session = _Obj(client_reference_id=str(ORG_A), customer=CUS_A, status="open")
    _stub_checkout(monkeypatch, retrieve=session)
    actor = _actor(db, ORG_A, "incomplete@a.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/payment-method/confirm", params={"setup_intent": "cs_open"})
    assert r.status_code == 400
    assert _rows(db, ORG_A) == []


def test_confirm_refused_for_internal_tenant(db, seeded, monkeypatch):
    session = _Obj(client_reference_id=str(ORG_INT), customer=CUS_INT, status="complete")
    _stub_checkout(monkeypatch, retrieve=session)
    actor = _actor(db, ORG_INT, "confint@owner.test")
    with _client(db, actor) as c:
        assert c.get(f"{API}/payment-method/confirm",
                     params={"setup_intent": "cs_int"}).status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# Delete
# ══════════════════════════════════════════════════════════════════════════════
def test_delete_refuses_the_only_card_on_an_active_subscription(db, seeded, monkeypatch):
    stub = StubStripe({CUS_A: [_pm("pm_only")]}, defaults={CUS_A: "pm_only"})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    row = _rows(db, ORG_A)[0]
    monkeypatch.setattr(billing_router.stripe, "PaymentMethod", stub.PaymentMethod)

    actor = _actor(db, ORG_A, "delonly@a.test")
    with _client(db, actor) as c:
        r = c.delete(f"{API}/payment-method/{row.id}")
    assert r.status_code == 409
    assert "replacement" in r.json()["detail"].lower()
    assert stub.detached == [], "must not detach at Stripe either"
    assert len(_rows(db, ORG_A)) == 1


def test_delete_succeeds_when_a_replacement_exists(db, seeded, monkeypatch):
    stub = StubStripe({CUS_A: [_pm("pm_old"), _pm("pm_new", last4="1111")]},
                      defaults={CUS_A: "pm_new"})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    old = next(r for r in _rows(db, ORG_A) if r.stripe_payment_method_id == "pm_old")
    monkeypatch.setattr(billing_router.stripe, "PaymentMethod", stub.PaymentMethod)

    actor = _actor(db, ORG_A, "delrepl@a.test")
    with _client(db, actor) as c:
        r = c.delete(f"{API}/payment-method/{old.id}")
    assert r.status_code == 200
    assert stub.detached == ["pm_old"], "must detach at Stripe"
    assert [x.stripe_payment_method_id for x in _rows(db, ORG_A)] == ["pm_new"]


def test_delete_keeps_the_local_row_when_stripe_detach_fails(db, seeded, monkeypatch):
    """Deleting locally while the card stays attached would tell the customer it was
    removed while Stripe kept charging it."""
    stub = StubStripe({CUS_A: [_pm("pm_1"), _pm("pm_2", last4="1111")]},
                      defaults={CUS_A: "pm_2"})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    target = next(r for r in _rows(db, ORG_A) if r.stripe_payment_method_id == "pm_1")

    failing = StubStripe({CUS_A: []}, fail="detach")
    monkeypatch.setattr(billing_router.stripe, "PaymentMethod", failing.PaymentMethod)
    actor = _actor(db, ORG_A, "delfail@a.test")
    with _client(db, actor) as c:
        r = c.delete(f"{API}/payment-method/{target.id}")
    assert r.status_code == 502
    db.expire_all()
    assert db.query(BillingPaymentMethod).filter_by(id=target.id).first() is not None


def test_delete_allowed_when_no_live_subscription(db, seeded, monkeypatch):
    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    sub.subscription_status = "canceled"
    db.commit()
    stub = StubStripe({CUS_A: [_pm("pm_last")]}, defaults={CUS_A: "pm_last"})
    billing_pm.sync_org_payment_methods(db, ORG_A, stripe_client=stub)
    row = _rows(db, ORG_A)[0]
    monkeypatch.setattr(billing_router.stripe, "PaymentMethod", stub.PaymentMethod)

    actor = _actor(db, ORG_A, "delcancelled@a.test")
    with _client(db, actor) as c:
        assert c.delete(f"{API}/payment-method/{row.id}").status_code == 200


def test_delete_is_tenant_scoped(db, seeded):
    billing_pm.sync_org_payment_methods(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [_pm("pm_iso")]}))
    row = _rows(db, ORG_A)[0]
    intruder = _actor(db, ORG_B, "delintruder@b.test")
    with _client(db, intruder) as c:
        assert c.delete(f"{API}/payment-method/{row.id}").status_code == 404


def test_delete_refused_for_internal_tenant(db, seeded):
    legacy = BillingPaymentMethod(organization_id=ORG_INT, provider="stripe",
                                 brand="visa", last_four="4242",
                                 stripe_payment_method_id="pm_int_legacy")
    db.add(legacy); db.commit(); db.refresh(legacy)
    actor = _actor(db, ORG_INT, "delint@owner.test")
    with _client(db, actor) as c:
        assert c.delete(f"{API}/payment-method/{legacy.id}").status_code == 403


def test_legacy_rows_do_not_satisfy_the_keep_one_card_rule(db, seeded):
    """A hand-entered row cannot be charged, so it must not count as a fallback."""
    db.add(BillingPaymentMethod(organization_id=ORG_A, provider="stripe",
                                brand="visa", last_four="9999"))
    db.commit()
    assert billing_pm.countable_cards(db, ORG_A) == 0


# ══════════════════════════════════════════════════════════════════════════════
# API + frontend contract
# ══════════════════════════════════════════════════════════════════════════════
def test_list_reports_stripe_vs_legacy_source(db, seeded):
    db.add(BillingPaymentMethod(organization_id=ORG_A, provider="stripe",
                                brand="visa", last_four="9999"))
    db.commit()
    billing_pm.sync_org_payment_methods(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [_pm("pm_real")]}))
    actor = _actor(db, ORG_A, "src@a.test")
    with _client(db, actor) as c:
        body = c.get(f"{API}/payment-method").json()
    sources = {p["last_four"]: p["source"] for p in body["payment_methods"]}
    assert sources["9999"] == "legacy"
    assert sources["4242"] == "stripe"


def test_frontend_has_no_card_entry_form():
    src = _jsx()
    for token in ("PaymentMethodModal", "last_four: \"\"", "billingLast4",
                  "billingCardBrand", "billingSaveCard"):
        assert token not in src, f"card-entry remnant: {token}"


def test_frontend_starts_the_stripe_hosted_setup(db=None):
    src = _jsx()
    assert "payment-method/session" in src
    assert "startCardSetup" in src
    assert "window.location.href = d.session_url" in src


def test_frontend_confirms_the_setup_instead_of_claiming_success():
    src = _jsx()
    assert "payment-method/confirm?setup_intent=" in src
    block = src[src.index("payment-method/confirm?setup_intent="):]
    block = block[:block.index("}, []);")]
    assert "if (r.ok)" in block, "success must be gated on r.ok"
    catch = block[block.index(".catch("):]
    assert "billingSetupSuccess" not in catch


def test_frontend_no_longer_claims_tokenization_it_does_not_do():
    src = _jsx()
    assert "In production, this opens a Stripe-hosted setup session" not in src


def test_no_migration_was_required():
    """The audit found stripe_payment_method_id and token_reference already present."""
    cols = BillingPaymentMethod.__table__.columns
    assert "stripe_payment_method_id" in cols
    assert "token_reference" in cols
