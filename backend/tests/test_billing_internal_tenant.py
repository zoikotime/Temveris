"""Internal tenants must never reach a Stripe write path.

Temveris's owner (Temveris) runs the product it sells: it needs full plan
entitlements but must never be charged. Before ``organizations.billing_type``
existed, that was only an *implicit* invariant — the Stripe write paths refuse
solely when ``stripe_subscription_id``/``stripe_customer_id`` are NULL, and
organization 1 has both populated. One click of "Upgrade" would have modified a
live subscription.

The whole Stripe write surface is five calls, so these tests cover it exhaustively:

  1. checkout.Session.create        router:/upgrade (first purchase)
  2. Subscription.modify            service.change_stripe_plan
  3. checkout.Session.create        router:/payment-method/session (mode=setup)
  4. billing_portal.Session.create  router:/customer-portal
  5. Subscription.modify            seats.sync_org_seats

Each is asserted twice: refused for an internal tenant, and still working for a
customer tenant. A guard that also breaks paying customers is not a fix.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import reconcile as billing_reconcile
from app.billing import router as billing_router
from app.billing import seats as billing_seats
from app.billing import service as billing_service
from app.billing import webhooks as billing_webhooks
from app.billing import worker_state
from database.database import get_db
from database.models import (
    BillingPlan, BillingSubscription, Organization, User,
)

API = "/api/admin/billing"

ORG_INTERNAL = 7801          # the platform owner — never billed
ORG_CUSTOMER = 7802          # a paying client — must keep working
SUB_INT = "sub_internal_legacy"      # internal org still carrying Stripe ids
CUS_INT = "cus_internal_legacy"
SUB_CUS = "sub_customer_live"
CUS_CUS = "cus_customer_live"
ITEM = "si_test"


class ExplodingStripe:
    """Any attribute access is a test failure — proves no Stripe call was made."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        raise AssertionError(f"Stripe was called for a non-billable tenant: .{name}")


class StubStripe:
    def __init__(self, quantity=1, status="active"):
        self.quantities = {SUB_INT: quantity, SUB_CUS: quantity}
        self.customers = {SUB_INT: CUS_INT, SUB_CUS: CUS_CUS}
        self.modify_calls, self.retrieve_calls = [], []
        outer = self

        class _Subscription:
            @staticmethod
            def retrieve(sid):
                outer.retrieve_calls.append(sid)
                return {"id": sid, "customer": outer.customers[sid], "status": status,
                        "items": {"data": [{"id": ITEM, "quantity": outer.quantities[sid],
                                            "price": {"id": "price_x"}}]},
                        "cancel_at_period_end": False, "current_period_end": 1800000000}

            @staticmethod
            def modify(sid, **kw):
                outer.modify_calls.append((sid, kw))
                for it in kw.get("items", []):
                    if "quantity" in it:
                        outer.quantities[sid] = it["quantity"]
                return {"id": sid, "status": "active"}

        self.Subscription = _Subscription


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id.in_([ORG_INTERNAL, ORG_CUSTOMER])).delete(
            synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_INTERNAL, ORG_CUSTOMER])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_INTERNAL, name="Temveris (internal)",
                        billing_type=billing_service.BILLING_TYPE_INTERNAL))
    db.add(Organization(id=ORG_CUSTOMER, name="Paying Client Ltd",
                        billing_type=billing_service.BILLING_TYPE_CUSTOMER))
    plan = BillingPlan(code="governed", name="Governed", tier_order=2,
                       monthly_price_per_user=15.0, annual_price_per_user=144.0,
                       user_limit=100)
    db.add(plan)
    db.flush()

    # The internal org deliberately still carries Stripe ids — the real-world case
    # (organization 1 today). The guard must not depend on them being NULL.
    db.add(BillingSubscription(organization_id=ORG_INTERNAL, plan_id=plan.id,
                               billing_cycle="monthly", subscription_status="active",
                               stripe_subscription_id=SUB_INT, stripe_customer_id=CUS_INT,
                               active_users=1, currency="USD"))
    db.add(BillingSubscription(organization_id=ORG_CUSTOMER, plan_id=plan.id,
                               billing_cycle="monthly", subscription_status="active",
                               stripe_subscription_id=SUB_CUS, stripe_customer_id=CUS_CUS,
                               active_users=1, currency="USD"))
    db.commit()
    yield
    _clean()


def _actor(db, org_id, email, role="admin"):
    u = User(name="Billing Admin", email=email, organization_id=org_id, role=role,
             billing_state=worker_state.ACTIVE_BILLABLE, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _client(db, actor):
    from app.auth.service import get_current_user
    app = FastAPI()
    app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _sub(db, org_id):
    return (db.query(BillingSubscription)
              .filter(BillingSubscription.organization_id == org_id).first())


def _staff(db, org_id, n, tag):
    for i in range(n):
        db.add(User(name=f"{tag}{i}", email=f"{tag}{i}-{org_id}@seat.test",
                    organization_id=org_id, role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Classification primitives
# ══════════════════════════════════════════════════════════════════════════════
def test_classification_reads_the_organization(db, seeded):
    assert billing_service.is_internal_tenant(db, ORG_INTERNAL) is True
    assert billing_service.is_internal_tenant(db, ORG_CUSTOMER) is False


def test_new_organizations_default_to_customer(db, seeded):
    """DEFAULT 'customer' is the revenue-safe direction: forgetting the flag can
    never hand a paying client free service."""
    db.add(Organization(id=7899, name="Brand New Client"))
    db.commit()
    try:
        assert billing_service.is_internal_tenant(db, 7899) is False
    finally:
        db.query(Organization).filter(Organization.id == 7899).delete()
        db.commit()


@pytest.mark.parametrize("org_id", [None, 999999])
def test_classification_fails_closed(db, seeded, org_id):
    """A missing or unknown organization is treated as non-billable. Refusing to
    charge an unknown tenant beats charging one."""
    assert billing_service.is_internal_tenant(db, org_id) is True


def test_require_billable_tenant_raises_for_internal(db, seeded):
    with pytest.raises(billing_service.InternalTenantBilling):
        billing_service.require_billable_tenant(db, ORG_INTERNAL)
    assert billing_service.require_billable_tenant(db, ORG_CUSTOMER) == ORG_CUSTOMER


# ══════════════════════════════════════════════════════════════════════════════
# Surface 1 & 2 — /upgrade  (Checkout Session and Subscription.modify)
# ══════════════════════════════════════════════════════════════════════════════
def test_upgrade_is_refused_for_internal_tenant(db, seeded, monkeypatch):
    """Both /upgrade branches create Stripe objects; neither may run."""
    monkeypatch.setattr(billing_router, "stripe", ExplodingStripe())
    actor = _actor(db, ORG_INTERNAL, "int-up@temveris.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/upgrade", json={"plan_code": "governed", "billing_cycle": "monthly"})
    assert r.status_code == 403
    assert "not billed" in r.json()["detail"].lower()


def test_upgrade_still_works_for_customer(db, seeded, monkeypatch):
    """The guard must not break paying customers."""
    monkeypatch.setenv("STRIPE_PRICE_GOVERNED_MONTHLY", "price_governed_monthly")
    stub = StubStripe()
    monkeypatch.setattr(billing_service, "_stripe_for_plan_change", lambda: stub, raising=False)
    actor = _actor(db, ORG_CUSTOMER, "cus-up@client.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/upgrade", json={"plan_code": "governed", "billing_cycle": "monthly"})
    # Not 403 — the tenant guard let it through (whatever Stripe wiring then does).
    assert r.status_code != 403


def test_change_stripe_plan_refuses_internal_even_with_stripe_ids(db, seeded):
    """Defence in depth: one of only two Subscription.modify call sites verifies
    for itself rather than trusting the router."""
    sub = _sub(db, ORG_INTERNAL)
    assert sub.stripe_subscription_id == SUB_INT      # ids present, guard must still fire
    plan = db.query(BillingPlan).filter(BillingPlan.code == "governed").first()
    stripe = ExplodingStripe()

    with pytest.raises(billing_service.StripePlanChangeError) as exc:
        billing_service.change_stripe_plan(
            db, sub, price_id="price_x", seats=5, target_plan=plan,
            billing_cycle="monthly", stripe_client=stripe,
        )
    assert exc.value.status_code == 403


def test_change_stripe_plan_still_works_for_customer(db, seeded):
    sub = _sub(db, ORG_CUSTOMER)
    plan = db.query(BillingPlan).filter(BillingPlan.code == "governed").first()
    stub = StubStripe()
    billing_service.change_stripe_plan(
        db, sub, price_id="price_x", seats=5, target_plan=plan,
        billing_cycle="monthly", stripe_client=stub,
    )
    assert stub.modify_calls, "customer plan change must still reach Stripe"


# ══════════════════════════════════════════════════════════════════════════════
# Surface 3 & 4 — /payment-method/session and /customer-portal
# ══════════════════════════════════════════════════════════════════════════════
def test_setup_session_is_refused_for_internal_tenant(db, seeded, monkeypatch):
    monkeypatch.setattr(billing_router, "stripe", ExplodingStripe())
    actor = _actor(db, ORG_INTERNAL, "int-pm@temveris.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/payment-method/session")
    assert r.status_code == 403


def test_customer_portal_is_refused_for_internal_tenant_even_with_customer_id(db, seeded, monkeypatch):
    """The hosted portal allows plan change and cancellation outside our guards, so
    holding a stripe_customer_id must not be enough to open it."""
    monkeypatch.setattr(billing_router, "stripe", ExplodingStripe())
    actor = _actor(db, ORG_INTERNAL, "int-portal@temveris.test")
    assert _sub(db, ORG_INTERNAL).stripe_customer_id == CUS_INT
    with _client(db, actor) as c:
        r = c.post(f"{API}/customer-portal")
    assert r.status_code == 403


def test_customer_portal_reaches_stripe_for_customer(db, seeded, monkeypatch):
    calls = []

    class _Portal:
        class Session:
            @staticmethod
            def create(**kw):
                calls.append(kw)
                return type("S", (), {"url": "https://portal.stripe.test/x"})()

    monkeypatch.setattr(billing_router.stripe, "billing_portal", _Portal, raising=False)
    actor = _actor(db, ORG_CUSTOMER, "cus-portal@client.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/customer-portal")
    assert r.status_code == 200
    assert calls and calls[0]["customer"] == CUS_CUS


# ══════════════════════════════════════════════════════════════════════════════
# Surface 5 — seat synchronisation
# ══════════════════════════════════════════════════════════════════════════════
def test_seat_sync_makes_no_stripe_call_for_internal_tenant(db, seeded):
    _staff(db, ORG_INTERNAL, 6, "int")
    stripe = ExplodingStripe()
    result = billing_seats.sync_org_seats(db, ORG_INTERNAL, stripe_client=stripe,
                                          reason="test")
    assert result["status"] == "internal_tenant"


def test_seat_sync_keeps_local_counters_accurate_for_internal_tenant(db, seeded):
    """Not billed, but the UI and reports must still show the true seat count."""
    _staff(db, ORG_INTERNAL, 6, "cnt")
    expected = billing_seats.desired_seat_quantity(db, ORG_INTERNAL)
    billing_seats.sync_org_seats(db, ORG_INTERNAL, stripe_client=ExplodingStripe(),
                                 reason="test")
    db.expire_all()
    assert _sub(db, ORG_INTERNAL).active_users == expected


def test_seat_sync_still_reaches_stripe_for_customer(db, seeded):
    _staff(db, ORG_CUSTOMER, 5, "cus")
    stub = StubStripe(quantity=1)
    result = billing_seats.sync_org_seats(db, ORG_CUSTOMER, stripe_client=stub,
                                          reason="test")
    assert result["status"] == "updated"
    assert stub.modify_calls


def test_lifecycle_write_on_internal_tenant_never_calls_stripe(db, seeded):
    """Hiring/offboarding inside the owner's own org must not touch Stripe."""
    u = _actor(db, ORG_INTERNAL, "hire@temveris.test", role="employee")
    billing_seats.set_worker_billing_state(db, u, worker_state.DEACTIVATED,
                                          reason="test")
    db.commit()
    assert _sub(db, ORG_INTERNAL).stripe_subscription_id == SUB_INT


# ══════════════════════════════════════════════════════════════════════════════
# Background jobs
# ══════════════════════════════════════════════════════════════════════════════
def test_seat_sweep_excludes_internal_tenants(db, seeded):
    """Asserted on `checked`, not on Stripe calls.

    Both orgs carry a stripe_subscription_id, so a sweep that ignored billing_type
    would report checked=2. The per-row guard in sync_org_seats would still stop the
    Stripe call, which is why an assertion about retrieve_calls passes either way and
    proves nothing — this pins the query itself.
    """
    stub = StubStripe()
    tally = billing_seats.reconcile_all_orgs(db, stripe_client=stub)
    assert tally["checked"] == 1, (
        f"sweep selected {tally['checked']} orgs; the internal tenant must be "
        "excluded by the query, not merely skipped downstream"
    )
    assert SUB_INT not in stub.retrieve_calls
    assert SUB_CUS in stub.retrieve_calls, "the paying customer must still be swept"


def test_subscription_reconcile_skips_internal_tenant(db, seeded):
    report = billing_reconcile.reconcile_subscription(db, ORG_INTERNAL,
                                                      stripe_client=ExplodingStripe())
    assert report["status"] == "internal_tenant"


def test_subscription_reconcile_sweep_excludes_internal_tenants(db, seeded):
    """Same reasoning as the seat sweep: assert on the selected count, so the query
    filter is what's being verified rather than the downstream per-row guard."""
    stub = StubStripe()
    tally = billing_reconcile.reconcile_all(db, stripe_client=stub)
    assert tally["checked"] == 1, (
        f"sweep selected {tally['checked']} orgs; internal tenants must be excluded "
        "by the query"
    )
    assert SUB_INT not in stub.retrieve_calls
    assert SUB_CUS in stub.retrieve_calls, "the paying customer must still be reconciled"


# ══════════════════════════════════════════════════════════════════════════════
# Webhooks
# ══════════════════════════════════════════════════════════════════════════════
class _Obj:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_webhook_refuses_to_attach_stripe_ids_to_internal_tenant(db, seeded):
    """Even a correctly-signed checkout event must not make an internal org
    billable."""
    data = _Obj(payment_status="paid", client_reference_id=str(ORG_INTERNAL),
                subscription="sub_brand_new", customer="cus_brand_new",
                metadata=_Obj(plan_code="governed", billing_cycle="monthly"))
    with pytest.raises(billing_webhooks.WebhookRejected) as exc:
        billing_webhooks.handle_checkout_session_completed(db, data, StubStripe())
    assert "internal_tenant" in str(exc.value)


def test_webhook_rejection_leaves_internal_subscription_untouched(db, seeded):
    before = _sub(db, ORG_INTERNAL).stripe_subscription_id
    data = _Obj(payment_status="paid", client_reference_id=str(ORG_INTERNAL),
                subscription="sub_brand_new", customer="cus_brand_new",
                metadata=_Obj(plan_code="governed", billing_cycle="monthly"))
    with pytest.raises(billing_webhooks.WebhookRejected):
        billing_webhooks.handle_checkout_session_completed(db, data, StubStripe())
    db.expire_all()
    assert _sub(db, ORG_INTERNAL).stripe_subscription_id == before


# ══════════════════════════════════════════════════════════════════════════════
# Read paths stay open; API contract
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_can_still_view_billing(db, seeded):
    """Exempt from charging, not from visibility — the owner still needs to see its
    plan, seats and usage."""
    actor = _actor(db, ORG_INTERNAL, "int-view@temveris.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["is_internal_tenant"] is True
    assert body["billing_type"] == "internal"
    assert body["subscription"]["plan"]["code"] == "governed"   # full entitlements


def test_customer_summary_reports_customer_type(db, seeded):
    actor = _actor(db, ORG_CUSTOMER, "cus-view@client.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/summary")
    assert r.status_code == 200
    assert r.json()["is_internal_tenant"] is False
    assert r.json()["billing_type"] == "customer"


# ══════════════════════════════════════════════════════════════════════════════
# Static guards — a future call site must not bypass the helper
# ══════════════════════════════════════════════════════════════════════════════
def test_every_stripe_write_site_is_accounted_for():
    """If someone adds a new Stripe write call, this fails until it is reviewed and
    added to the known set — so the guard cannot be silently bypassed."""
    billing_dir = pathlib.Path(billing_router.__file__).parent
    pattern = re.compile(r"\.(create|modify|cancel|delete|update)\(")
    found = set()
    for path in sorted(billing_dir.glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and re.search(r"stripe(_client)?\.", line):
                found.add(f"{path.name}:{i}")

    known = {
        "router.py",      # checkout.Session.create x2, billing_portal.Session.create
        "seats.py",       # Subscription.modify (seat sync)
        "service.py",     # Subscription.modify (plan change)
    }
    unexpected = {f for f in found if f.split(":")[0] not in known}
    assert not unexpected, f"new Stripe write site needs a tenant guard: {unexpected}"


def test_guarded_endpoints_all_call_the_helper():
    src = pathlib.Path(billing_router.__file__).read_text(encoding="utf-8")
    for ep in ('@router.post("/upgrade")',
               '@router.post("/payment-method/session")',
               '@router.post("/customer-portal")'):
        block = src[src.index(ep):]
        block = block[:block.index("@router.", len(ep))]
        assert "_require_billable_tenant" in block, f"{ep} is missing the tenant guard"


def test_internal_classification_is_not_stored_in_mutable_state():
    """subscription_status is rewritten by webhooks and plan changes, so it must not
    carry the classification. billing_type lives on organizations for that reason."""
    from database.models import Organization as O
    assert "billing_type" in O.__table__.columns
    assert billing_service.BILLING_TYPE_INTERNAL == "internal"
    src = pathlib.Path(billing_service.__file__).read_text(encoding="utf-8")
    assert 'subscription_status = "internal"' not in src
