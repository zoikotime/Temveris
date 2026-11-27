"""Phase 2.3, Steps 1-5 — payment truthfulness, retired fake payment, correct
Stripe status mapping, seat-sync payment behaviour, and removal of the invented
tax rate.

Each block below pins a specific way the billing surface used to tell the
customer something untrue:

1. A Checkout Session was treated as paid when ``status == "complete"``, which
   Stripe also reports for sessions whose ``payment_status`` is ``"unpaid"``;
   the frontend then showed "Payment successful" even on an HTTP 400.
2. ``POST /invoices/{id}/pay`` stamped an invoice ``paid`` with no Stripe call.
3. Stripe's terminal ``canceled`` was stored as ``cancel_pending``, and
   ``incomplete``/``incomplete_expired`` were dropped entirely, so a
   never-paid-for subscription stayed ``active`` locally.
4. Seat syncs omitted ``payment_behavior``, so Stripe returned 200 for a seat
   increase whose proration invoice could not be collected.
5. Both invoice-estimate paths added a hardcoded 8% "tax" that matched no
   jurisdiction.
"""

from __future__ import annotations

import itertools
import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import router as billing_router
from app.billing import webhooks as billing_webhooks
from app.billing import seats, worker_state
from database.database import get_db
from database.models import (
    BillingInvoice, BillingPlan, BillingSubscription, Organization, User,
)

ORG_A = 7501
ORG_B = 7502
SUB_A = "sub_phase23A"
CUS_A = "cus_phase23A"
ITEM_A = "si_phase23A"

API = "/api/admin/billing"
_JSX = (pathlib.Path(__file__).resolve().parents[2]
        / "frontend/src/components/BillingModule.jsx")


def _jsx_source(*, strip_comments: bool = False) -> str:
    src = _JSX.read_text(encoding="utf-8")
    if strip_comments:
        # Assertions about what the UI *does* must not be satisfied — or broken —
        # by prose in a comment explaining what was removed.
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("//"))
    return src


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════════════════════
class _Obj:
    """Attribute-access stand-in for a StripeObject (which has no usable .get)."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingInvoice).delete()
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="Alpha Ltd"))
    db.add(Organization(id=ORG_B, name="Beta Ltd"))
    governed = BillingPlan(code="governed", name="Governed", tier_order=2,
                           monthly_price_per_user=15.0, annual_price_per_user=144.0,
                           user_limit=100)
    db.add(governed)
    db.flush()

    db.add(BillingSubscription(
        organization_id=ORG_A, plan_id=governed.id, billing_cycle="monthly",
        subscription_status="active", stripe_subscription_id=SUB_A,
        stripe_customer_id=CUS_A, active_users=1, currency="USD"))
    db.add(BillingSubscription(
        organization_id=ORG_B, plan_id=governed.id, billing_cycle="monthly",
        subscription_status="active"))
    db.commit()
    yield
    _clean()


def _actor(db, org_id, email, role="owner"):
    u = User(name="Billing Owner", email=email, organization_id=org_id, role=role,
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


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — a Checkout Session is only paid when payment_status says so
# ══════════════════════════════════════════════════════════════════════════════
def _confirm(db, monkeypatch, actor, *, payment_status, status="complete",
             org_ref=None, sub_id="sub_new", raise_exc=None):
    session = _Obj(
        payment_status=payment_status,
        status=status,
        client_reference_id=str(org_ref if org_ref is not None else actor.organization_id),
        metadata=_Obj(plan_code="governed", billing_cycle="monthly"),
        subscription=sub_id,
        customer="cus_new",
    )

    def _retrieve(session_id, *a, **kw):
        if raise_exc:
            raise raise_exc
        return session

    monkeypatch.setattr(billing_router.stripe.checkout.Session, "retrieve",
                        staticmethod(_retrieve))
    with _client(db, actor) as c:
        return c.get(f"{API}/upgrade/confirm", params={"session_id": "cs_test_1"})


@pytest.mark.parametrize("payment_status", ["paid", "no_payment_required"])
def test_confirm_accepts_genuinely_paid_sessions(db, seeded, monkeypatch, payment_status):
    actor = _actor(db, ORG_A, f"ok-{payment_status}@alpha.test")
    r = _confirm(db, monkeypatch, actor, payment_status=payment_status)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert _sub(db, ORG_A).subscription_status == "active"


def test_complete_but_unpaid_session_is_rejected(db, seeded, monkeypatch):
    """The core regression: status == "complete" is NOT proof of payment.

    Stripe reports a session as complete once the customer finishes the flow,
    including when the payment is unpaid. The old `and` condition let this
    through and activated the plan for free.
    """
    actor = _actor(db, ORG_A, "unpaid@alpha.test")
    _sub(db, ORG_A).subscription_status = "trial"
    db.commit()

    r = _confirm(db, monkeypatch, actor, payment_status="unpaid", status="complete")

    assert r.status_code == 400
    assert "not completed" in r.json()["detail"].lower()
    # The plan must not have been granted.
    db.expire_all()
    assert _sub(db, ORG_A).subscription_status == "trial"


@pytest.mark.parametrize("payment_status", ["unpaid", None])
def test_unpaid_or_missing_payment_status_never_confirms(db, seeded, monkeypatch,
                                                        payment_status):
    actor = _actor(db, ORG_A, f"no-{payment_status}@alpha.test")
    r = _confirm(db, monkeypatch, actor, payment_status=payment_status, status="open")
    assert r.status_code == 400


def test_confirm_reports_400_when_stripe_lookup_fails(db, seeded, monkeypatch):
    """Drives the frontend's error branch — no success may be shown for this."""
    actor = _actor(db, ORG_A, "boom@alpha.test")
    r = _confirm(db, monkeypatch, actor, payment_status="paid",
                 raise_exc=RuntimeError("no such session"))
    assert r.status_code == 400


def test_confirm_still_rejects_another_tenants_session(db, seeded, monkeypatch):
    """Tenant ownership protection must survive the tightened gate."""
    actor = _actor(db, ORG_A, "xtenant@alpha.test")
    r = _confirm(db, monkeypatch, actor, payment_status="paid", org_ref=ORG_B)
    assert r.status_code == 403
    # Org B's subscription untouched.
    assert _sub(db, ORG_B).stripe_subscription_id is None


@pytest.mark.parametrize("role", ["employee", "hr"])
def test_confirm_requires_manage_role(db, seeded, monkeypatch, role):
    """Non-manage roles cannot confirm a checkout.

    Previously parameterised on "admin". Admin now holds billing-manage rights by
    the documented ZT-COM-BILL-001 §15 deviation (approved 2026-08-12), so the
    assertion moved to roles that genuinely have no billing access at all.
    """
    viewer = _actor(db, ORG_A, f"viewer-{role}@alpha.test", role=role)
    r = _confirm(db, monkeypatch, viewer, payment_status="paid")
    assert r.status_code == 403


def test_frontend_only_shows_success_when_response_is_ok():
    """The success toast must be inside an `r.ok` branch, and the network-failure
    path (.catch) must not claim success."""
    src = _jsx_source(strip_comments=True)
    # The whole useEffect that handles the Stripe redirect.
    block = src[src.index("upgrade/confirm?session_id="):]
    block = block[:block.index("}, []);")]

    assert "if (r.ok)" in block, "confirm handler must gate success on r.ok"

    # Success may only be claimed inside the r.ok branch...
    ok_branch = block[block.index("if (r.ok)"):block.index(".catch(")]
    assert "billingCheckoutSuccess" in ok_branch

    # ...never in the network-failure handler.
    catch_body = block[block.index(".catch("):]
    assert "billingCheckoutSuccess" not in catch_body, \
        "network failure must not report a successful payment"
    assert '"error"' in catch_body, "network failure must raise an error toast"


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — the fake invoice payment endpoint is retired
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def invoice_a(db, seeded):
    inv = BillingInvoice(organization_id=ORG_A, invoice_number="A-900",
                         subtotal=15.0, tax_amount=0.0, total_amount=15.0,
                         status="issued", currency="USD")
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_pay_invoice_returns_410_gone(db, invoice_a):
    actor = _actor(db, ORG_A, "pay410@alpha.test")
    with _client(db, actor) as c:
        r = c.post(f"{API}/invoices/{invoice_a.id}/pay")
    assert r.status_code == 410
    assert "disabled" in r.json()["detail"].lower()


def test_unpaid_invoice_stays_unpaid_after_pay_attempt(db, invoice_a):
    actor = _actor(db, ORG_A, "stays@alpha.test")
    with _client(db, actor) as c:
        c.post(f"{API}/invoices/{invoice_a.id}/pay")
    db.expire_all()
    fresh = db.query(BillingInvoice).filter(BillingInvoice.id == invoice_a.id).first()
    assert fresh.status == "issued"
    assert fresh.paid_date is None


def test_cross_tenant_pay_cannot_touch_another_orgs_invoice(db, invoice_a):
    intruder = _actor(db, ORG_B, "intruder@beta.test")
    with _client(db, intruder) as c:
        r = c.post(f"{API}/invoices/{invoice_a.id}/pay")
    assert r.status_code >= 400
    db.expire_all()
    fresh = db.query(BillingInvoice).filter(BillingInvoice.id == invoice_a.id).first()
    assert fresh.status == "issued"
    assert fresh.paid_date is None


def test_pay_endpoint_makes_no_stripe_call_and_no_db_write(db, invoice_a, monkeypatch):
    """It must not reach Stripe *or* the database — it only raises."""
    def _explode(*a, **kw):
        raise AssertionError("retired endpoint must not call Stripe")

    monkeypatch.setattr(billing_router.stripe.Subscription, "modify", staticmethod(_explode))
    actor = _actor(db, ORG_A, "nostripe@alpha.test")
    with _client(db, actor) as c:
        assert c.post(f"{API}/invoices/{invoice_a.id}/pay").status_code == 410


def test_frontend_no_longer_exposes_invoice_payment():
    src = _jsx_source(strip_comments=True)
    assert "payInvoice(" not in src, "the Pay-now handler must be gone"
    assert re.search(r"invoices/\$\{[^}]+\}/pay", src) is None, \
        "frontend must not call the retired pay endpoint"


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Stripe subscription status mapping
# ══════════════════════════════════════════════════════════════════════════════
_wh_seq = itertools.count(1)


def _webhook(db, monkeypatch, event_type, obj, *, stripe_status=None):
    """Deliver one webhook event through the real endpoint.

    Phase 3 changed the handler to treat an event as a trigger and re-read the
    authoritative subscription from Stripe, so the payload's ``status`` is no
    longer what gets applied. The stub below therefore reports the same status
    Stripe would — which is what these tests were always really asserting: that a
    given *Stripe* status maps to the right local status.

    Each delivery gets a unique event id because the ledger now rejects replays.
    """
    event = {"id": f"evt_p23_{next(_wh_seq)}", "type": event_type,
             "created": 1_700_000_000, "data": {"object": obj}}
    monkeypatch.setattr(billing_router.stripe.Webhook, "construct_event",
                        staticmethod(lambda payload, sig, secret: event))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_p23")

    remote_status = stripe_status or getattr(obj, "status", None) or "canceled"

    class _Stub:
        class Subscription:
            @staticmethod
            def retrieve(sid, *a, **k):
                return {"id": sid, "customer": CUS_A, "status": remote_status,
                        "cancel_at_period_end": False, "canceled_at": None,
                        "current_period_end": 1893456000,
                        "items": {"data": [{"id": "si_p23", "quantity": 1,
                                            "price": {"id": "price_p23",
                                                      "recurring": {"interval": "month"}}}]}}

    real = billing_webhooks.process_event
    monkeypatch.setattr("app.billing.webhooks.process_event",
                        lambda _db, ev, **kw: real(_db, ev, stripe_client=_Stub))

    actor = _actor(db, ORG_A, f"wh-{event_type}-{id(obj)}@alpha.test")
    with _client(db, actor) as c:
        return c.post(f"{API}/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})


@pytest.mark.parametrize("stripe_status,expected", [
    ("active",             "active"),
    ("trialing",           "trial"),
    ("past_due",           "past_due"),
    ("unpaid",             "past_due"),
    ("canceled",           "canceled"),            # was wrongly "cancel_pending"
    ("incomplete",         "incomplete"),          # was silently ignored
    ("incomplete_expired", "incomplete_expired"),  # was silently ignored
])
def test_subscription_updated_maps_stripe_status(db, seeded, monkeypatch,
                                                 stripe_status, expected):
    r = _webhook(db, monkeypatch, "customer.subscription.updated",
                 _Obj(id=SUB_A, status=stripe_status), stripe_status=stripe_status)
    assert r.status_code == 200
    db.expire_all()
    assert _sub(db, ORG_A).subscription_status == expected


def test_subscription_deleted_is_terminal_not_pending(db, seeded, monkeypatch):
    r = _webhook(db, monkeypatch, "customer.subscription.deleted", _Obj(id=SUB_A),
                 stripe_status="canceled")
    assert r.status_code == 200
    db.expire_all()
    assert _sub(db, ORG_A).subscription_status == "canceled"


def test_canceled_never_maps_to_cancel_pending(db, seeded, monkeypatch):
    """cancel_pending means "still live, ends later" — the opposite of canceled."""
    _webhook(db, monkeypatch, "customer.subscription.updated",
             _Obj(id=SUB_A, status="canceled"), stripe_status="canceled")
    db.expire_all()
    assert _sub(db, ORG_A).subscription_status != "cancel_pending"


def test_incomplete_does_not_leave_subscription_active(db, seeded, monkeypatch):
    """An unpaid first invoice must not read as a healthy paid subscription."""
    assert _sub(db, ORG_A).subscription_status == "active"
    _webhook(db, monkeypatch, "customer.subscription.updated",
             _Obj(id=SUB_A, status="incomplete"), stripe_status="incomplete")
    db.expire_all()
    assert _sub(db, ORG_A).subscription_status == "incomplete"


def test_status_mapping_fits_the_column_and_needs_no_migration():
    """All mapped values fit subscription_status without a schema change."""
    col = BillingSubscription.__table__.columns["subscription_status"]
    assert col.type.length >= max(
        len(v) for v in ("canceled", "incomplete", "incomplete_expired"))


def test_frontend_labels_every_new_status():
    """Without a tint entry the pill falls back to a *neutral* label, never the
    green "Active" it used to show for unknown statuses."""
    src = _jsx_source()
    tints = src[src.index("const STATUS_TINTS"):src.index("// ─── Toast")]
    for key in ("canceled:", "incomplete:", "incomplete_expired:"):
        assert key in tints, f"STATUS_TINTS missing {key}"
    assert "STATUS[subStatus] || STATUS.active" not in src, \
        "unknown status must not fall back to the green Active pill"


# ══════════════════════════════════════════════════════════════════════════════
# Step 4 — seat sync sends payment_behavior
# ══════════════════════════════════════════════════════════════════════════════
class StubStripe:
    """Mirrors tests/test_billing_seat_sync.py's double, for one subscription."""

    def __init__(self, quantity=1, *, status="active", fail_on=None):
        self.quantities = {SUB_A: quantity}
        self.modify_calls = []
        outer = self

        class _Subscription:
            @staticmethod
            def retrieve(sid):
                return {"id": sid, "customer": CUS_A, "status": status,
                        "items": {"data": [{"id": ITEM_A,
                                            "quantity": outer.quantities[sid]}]}}

            @staticmethod
            def modify(sid, **kwargs):
                outer.modify_calls.append((sid, kwargs))
                if fail_on == "modify":
                    raise RuntimeError("card_declined: proration invoice unpaid")
                for it in kwargs.get("items", []):
                    if "quantity" in it:
                        outer.quantities[sid] = it["quantity"]
                return {"id": sid, "status": "active"}

        self.Subscription = _Subscription


def _staff(db, org_id, n, tag):
    for i in range(n):
        db.add(User(name=f"{tag}{i}", email=f"{tag}{i}-{org_id}@seat.test",
                    organization_id=org_id, role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()


def test_seat_increase_sends_error_if_incomplete(db, seeded):
    _staff(db, ORG_A, 5, "up")
    stub = StubStripe(quantity=1)
    result = seats.sync_org_seats(db, ORG_A, stripe_client=stub, reason="test")

    assert result["status"] == "updated"
    _, kwargs = stub.modify_calls[0]
    assert kwargs["payment_behavior"] == "error_if_incomplete"
    assert kwargs["proration_behavior"] == "create_prorations"


def test_seat_decrease_also_sends_error_if_incomplete(db, seeded):
    _staff(db, ORG_A, 2, "down")
    stub = StubStripe(quantity=9)
    result = seats.sync_org_seats(db, ORG_A, stripe_client=stub, reason="test")

    assert result["status"] == "updated"
    assert stub.modify_calls[0][1]["payment_behavior"] == "error_if_incomplete"


def test_seat_sync_never_sends_a_price(db, seeded):
    """A seat change must not be able to move the customer between plans."""
    _staff(db, ORG_A, 4, "np")
    stub = StubStripe(quantity=1)
    seats.sync_org_seats(db, ORG_A, stripe_client=stub, reason="test")

    for it in stub.modify_calls[0][1]["items"]:
        assert "price" not in it
        assert set(it) == {"id", "quantity"}


def test_rejected_seat_change_is_not_reported_as_success(db, seeded):
    """Stripe refusing an uncollectable proration must surface as failure, and the
    local quantity must not advance as though the seats were paid for."""
    _staff(db, ORG_A, 6, "decl")
    before = _sub(db, ORG_A).active_users
    stub = StubStripe(quantity=1, fail_on="modify")

    result = seats.sync_org_seats(db, ORG_A, stripe_client=stub, reason="test")

    assert result["status"] == "failed"
    assert "card_declined" in result["error"]
    db.expire_all()
    assert _sub(db, ORG_A).active_users == before


def test_failed_seat_sync_preserves_subscription_and_customer_ids(db, seeded):
    _staff(db, ORG_A, 6, "ids")
    stub = StubStripe(quantity=1, fail_on="modify")
    seats.sync_org_seats(db, ORG_A, stripe_client=stub, reason="test")

    db.expire_all()
    sub = _sub(db, ORG_A)
    assert sub.stripe_subscription_id == SUB_A
    assert sub.stripe_customer_id == CUS_A


def test_seat_sync_stays_within_one_tenant(db, seeded):
    """Org B has no Stripe subscription; syncing A must never modify it."""
    _staff(db, ORG_A, 3, "iso")
    stub = StubStripe(quantity=1)
    seats.sync_org_seats(db, ORG_A, stripe_client=stub, reason="test")

    assert all(sid == SUB_A for sid, _ in stub.modify_calls)
    for _, kwargs in stub.modify_calls:
        assert kwargs["metadata"]["organization_id"] == str(ORG_A)
    db.expire_all()
    assert _sub(db, ORG_B).stripe_subscription_id is None


def test_reconciliation_still_converges_with_payment_behavior(db, seeded):
    _staff(db, ORG_A, 7, "rec")
    stub = StubStripe(quantity=2)
    seats.reconcile_org_seat_quantity(db, ORG_A, stripe_client=stub)

    desired = seats.desired_seat_quantity(db, ORG_A)
    assert stub.quantities[SUB_A] == desired
    # Second pass is a no-op — already converged.
    calls = len(stub.modify_calls)
    seats.reconcile_org_seat_quantity(db, ORG_A, stripe_client=stub)
    assert len(stub.modify_calls) == calls


def test_plan_change_and_seat_sync_agree_on_payment_behavior():
    """Phase 2.1 chose error_if_incomplete; seat sync must not be laxer."""
    from app.billing import service
    svc = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    seat_src = pathlib.Path(seats.__file__).read_text(encoding="utf-8")
    assert 'payment_behavior="error_if_incomplete"' in svc
    assert 'payment_behavior="error_if_incomplete"' in seat_src


# ══════════════════════════════════════════════════════════════════════════════
# Step 5 — the invented 8% tax is gone
# ══════════════════════════════════════════════════════════════════════════════
def test_no_hardcoded_tax_rate_remains_in_billing_code():
    billing_dir = pathlib.Path(billing_router.__file__).parent
    offenders = []
    for path in billing_dir.glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])0\.0[1-9]\d*\b", line) and "tax" in line.lower():
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, "hardcoded tax rate still present:\n" + "\n".join(offenders)


def test_next_invoice_quotes_zero_tax_and_says_why(db, seeded):
    """15.00 x 1 seat -> subtotal 15.00, tax 0, total 15.00 (was 16.20)."""
    actor = _actor(db, ORG_A, "tax@alpha.test", role="admin")   # view is enough
    with _client(db, actor) as c:
        r = c.get(f"{API}/summary")
    assert r.status_code == 200, r.text

    ni = r.json()["next_invoice"]
    assert ni["subtotal"] == 15.0
    assert ni["tax_amount"] == 0
    assert ni["amount"] == 15.0
    # Phase 6 (2026-08-12): prices are final and tax-inclusive, so the note no longer
    # says tax will be "calculated at invoicing" — nothing adds tax later, and implying
    # a charge still to come would be a fresh inaccuracy.
    note = ni["tax_note"].lower()
    assert "calculated at invoicing" not in note
    assert "tax-inclusive" in note or "final" in note


def test_the_invoice_seeder_can_no_longer_fabricate_anything(db, seeded):
    """Superseded by Phase 6: invoices are mirrored from Stripe, never generated.

    This previously asserted the seeder produced rows with no invented tax. The seeder
    itself is now retired — a fabricated invoice is wrong regardless of its tax — so
    the assertion became "it cannot create anything at all".
    """
    from app.billing import service
    sub = _sub(db, ORG_A)
    with pytest.raises(RuntimeError, match="retired"):
        service.seed_demo_invoices_if_empty(db, sub)

    invoices = (db.query(BillingInvoice)
                  .filter(BillingInvoice.subscription_id == sub.id).all())
    assert invoices == [], "nothing may be fabricated"


def test_frontend_states_prices_are_tax_inclusive():
    """Superseded by Phase 6: the label changed from "Tax calculated at invoicing" to
    stating prices are final, because no tax is ever added."""
    src = _jsx_source()
    assert "Tax calculated at invoicing" not in src
    assert "tax-inclusive" in src.lower()
