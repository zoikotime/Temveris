"""Phase 6 — invoices come from Stripe, never from Temveris.

Previously ``seed_demo_invoices_if_empty`` fabricated three rows per subscription,
priced from the local plan and stamped with a hardcoded 8% "tax". Production still
holds three such rows claiming $9.72 each while the real Stripe invoice for that same
subscription is $615 — customers were shown numbers matching nothing Stripe charged.

Invoices are now mirrored: Stripe is authoritative, reads stay local, rows are keyed by
``stripe_invoice_id`` so a re-sync updates in place.

**Tax:** none, by business decision 2026-08-12 — plan prices are final and
tax-inclusive. Stripe Tax is not enabled and no manual rates exist. ``tax_amount`` is
mirrored from Stripe (0), never computed.

Also covers a defect this phase uncovered: both invoice webhook handlers read
``invoice.subscription``, which does not exist on API version 2026-07-29.dahlia (it
moved to ``parent.subscription_details.subscription``). Every real invoice event
resolved None and returned early, so ``invoice.paid`` never recovered a past_due
subscription and ``invoice.payment_failed`` never marked one.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import invoices as billing_invoices
from app.billing import router as billing_router
from app.billing import service as billing_service
from app.billing import webhooks as billing_webhooks
from app.billing import worker_state
from database.database import get_db
from database.models import (
    BillingInvoice, BillingPlan, BillingSubscription, Organization, User,
)

API = "/api/admin/billing"
ORG_A = 8101          # customer, has a Stripe customer
ORG_B = 8102          # a different customer — cross-tenant checks
ORG_INT = 8103        # internal tenant
CUS_A, CUS_B = "cus_mirrorA", "cus_mirrorB"
SUB_A, SUB_B = "sub_mirrorA", "sub_mirrorB"

_JSX = (pathlib.Path(__file__).resolve().parents[2]
        / "frontend/src/components/BillingModule.jsx")


def _inv(iid, *, customer=CUS_A, number="ZT-0001", status="paid", subtotal=4500,
         total=4500, tax=None, period=(1786000000, 1788000000), paid_at=1786500000,
         pdf="https://pay.stripe.com/invoice/test.pdf", subscription=SUB_A,
         nested=True, discounts=None):
    """A Stripe invoice shaped like the real thing (api 2026-07-29.dahlia)."""
    d = {
        "id": iid, "object": "invoice", "number": number, "customer": customer,
        "status": status, "subtotal": subtotal, "total": total,
        "currency": "usd", "amount_due": total, "amount_paid": total,
        "period_start": period[0], "period_end": period[1], "due_date": None,
        "created": period[0], "invoice_pdf": pdf,
        "hosted_invoice_url": "https://invoice.stripe.com/i/test",
        "status_transitions": {"finalized_at": paid_at, "paid_at": paid_at,
                              "voided_at": None, "marked_uncollectible_at": None},
        "total_discount_amounts": discounts or [],
        "automatic_tax": {"enabled": False},
    }
    if tax is not None:
        d["tax"] = tax
    if subscription:
        if nested:
            # Where it actually lives on the current API version.
            d["parent"] = {"type": "subscription_details",
                          "subscription_details": {"subscription": subscription,
                                                   "metadata": {}}}
        else:
            d["subscription"] = subscription      # legacy shape
    return d


class StubStripe:
    def __init__(self, invoices_by_customer=None, fail=False):
        self.by_customer = invoices_by_customer or {}
        self.list_calls = []
        outer = self

        class _Invoice:
            @staticmethod
            def list(customer=None, limit=None):
                outer.list_calls.append(customer)
                if fail:
                    raise RuntimeError("stripe unreachable")
                return {"data": outer.by_customer.get(customer, [])}

        class _Subscription:
            @staticmethod
            def retrieve(sid):
                return {"id": sid, "customer": CUS_A if sid == SUB_A else CUS_B,
                        "status": "active", "cancel_at_period_end": False,
                        "current_period_end": 1788000000,
                        "items": {"data": [{"id": "si_x", "quantity": 1,
                                            "price": {"id": "price_x"}}]}}

        self.Invoice = _Invoice
        self.Subscription = _Subscription


class ExplodingStripe:
    def __getattr__(self, n):
        raise AssertionError(f"Stripe called for a non-billable tenant: .{n}")


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingInvoice).delete()
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        for o in (ORG_A, ORG_B, ORG_INT):
            db.query(User).filter(User.organization_id == o).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id.in_([ORG_A, ORG_B, ORG_INT])).delete(
            synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_A, name="Mirror A", billing_type="customer"))
    db.add(Organization(id=ORG_B, name="Mirror B", billing_type="customer"))
    db.add(Organization(id=ORG_INT, name="Owner", billing_type="internal"))
    plan = BillingPlan(code="governed", name="Governed", tier_order=2,
                       monthly_price_per_user=15.0, annual_price_per_user=144.0,
                       user_limit=100)
    db.add(plan)
    db.flush()
    for org, sid, cid in ((ORG_A, SUB_A, CUS_A), (ORG_B, SUB_B, CUS_B)):
        db.add(BillingSubscription(organization_id=org, plan_id=plan.id,
                                   billing_cycle="monthly", subscription_status="active",
                                   stripe_subscription_id=sid, stripe_customer_id=cid,
                                   active_users=3, currency="USD"))
    db.add(BillingSubscription(organization_id=ORG_INT, plan_id=plan.id,
                               billing_cycle="monthly", subscription_status="active",
                               stripe_subscription_id="sub_int", stripe_customer_id="cus_int",
                               active_users=8, currency="USD"))
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
    return db.query(BillingInvoice).filter_by(organization_id=org).all()


# ══════════════════════════════════════════════════════════════════════════════
# Mirroring
# ══════════════════════════════════════════════════════════════════════════════
def test_stripe_invoice_is_mirrored_with_stripe_numbers(db, seeded):
    stub = StubStripe({CUS_A: [_inv("in_1", number="ZT-9001", subtotal=4500, total=4500)]})
    res = billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    assert res["status"] == "synced" and res["created"] == 1

    row = _rows(db, ORG_A)[0]
    assert row.stripe_invoice_id == "in_1"
    assert row.invoice_number == "ZT-9001"        # Stripe's number, not ours
    assert row.subtotal == 45.00
    assert row.total_amount == 45.00
    assert row.status == "paid"
    assert row.pdf_url == "https://pay.stripe.com/invoice/test.pdf"
    assert row.currency == "USD"


def test_no_tax_is_ever_added(db, seeded):
    """Prices are final and tax-inclusive; tax is mirrored, never computed."""
    stub = StubStripe({CUS_A: [_inv("in_tax", subtotal=4500, total=4500)]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    row = _rows(db, ORG_A)[0]
    assert row.tax_amount == 0
    assert row.total_amount == row.subtotal


def test_resync_updates_in_place_and_never_duplicates(db, seeded):
    stub = StubStripe({CUS_A: [_inv("in_dup", status="open", total=4500)]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    assert len(_rows(db, ORG_A)) == 1
    assert _rows(db, ORG_A)[0].status == "issued"          # Stripe "open"

    # Same invoice, now paid.
    stub.by_customer[CUS_A] = [_inv("in_dup", status="paid", total=4500)]
    res = billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    assert res["created"] == 0 and res["updated"] == 1
    assert len(_rows(db, ORG_A)) == 1
    assert _rows(db, ORG_A)[0].status == "paid"


@pytest.mark.parametrize("stripe_status,expected", [
    ("draft", "draft"), ("open", "issued"), ("paid", "paid"),
    ("void", "void"), ("uncollectible", "failed"),
])
def test_status_mapping(db, seeded, stripe_status, expected):
    stub = StubStripe({CUS_A: [_inv(f"in_{stripe_status}", status=stripe_status)]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    assert _rows(db, ORG_A)[0].status == expected


def test_legacy_fabricated_rows_are_never_touched(db, seeded):
    """The 3 production rows with stripe_invoice_id IS NULL are historical records of
    what the customer was shown. Correcting them is a finance decision."""
    legacy = BillingInvoice(organization_id=ORG_A, invoice_number="ZT-202601-1001",
                            subtotal=9.0, tax_amount=0.72, total_amount=9.72,
                            status="paid", currency="USD", stripe_invoice_id=None)
    db.add(legacy); db.commit()
    legacy_id = legacy.id

    stub = StubStripe({CUS_A: [_inv("in_new")]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)

    db.expire_all()
    kept = db.query(BillingInvoice).filter_by(id=legacy_id).first()
    assert kept is not None, "legacy row must not be deleted"
    assert kept.tax_amount == 0.72 and kept.total_amount == 9.72, "must not be rewritten"
    assert len(_rows(db, ORG_A)) == 2, "the Stripe invoice is added alongside it"


def test_discounts_are_mirrored(db, seeded):
    stub = StubStripe({CUS_A: [_inv("in_disc", subtotal=4500, total=4000,
                                    discounts=[{"amount": 500}])]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    row = _rows(db, ORG_A)[0]
    assert row.discount_amount == 5.00
    assert row.total_amount == 40.00


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
def test_only_the_orgs_own_customer_is_queried(db, seeded):
    stub = StubStripe({CUS_A: [_inv("in_a")], CUS_B: [_inv("in_b", customer=CUS_B)]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    assert stub.list_calls == [CUS_A]
    assert [r.stripe_invoice_id for r in _rows(db, ORG_A)] == ["in_a"]
    assert _rows(db, ORG_B) == []


def test_invoice_for_another_customer_is_refused(db, seeded):
    """Defence in depth: even if Stripe returned a foreign invoice, it is not attached."""
    stub = StubStripe({CUS_A: [_inv("in_foreign", customer="cus_someone_else")]})
    res = billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    assert res["skipped"] == 1 and res["created"] == 0
    assert _rows(db, ORG_A) == []


def test_resync_cannot_move_an_invoice_between_tenants(db, seeded):
    stub = StubStripe({CUS_A: [_inv("in_shared")]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    # Same invoice id now offered under org B's customer.
    stub.by_customer[CUS_B] = [_inv("in_shared", customer=CUS_B)]
    res = billing_invoices.sync_org_invoices(db, ORG_B, stripe_client=stub)
    assert res["skipped"] == 1
    db.expire_all()
    row = db.query(BillingInvoice).filter_by(stripe_invoice_id="in_shared").first()
    assert row.organization_id == ORG_A, "must stay with the original tenant"


def test_internal_tenant_is_skipped_with_no_stripe_call(db, seeded):
    res = billing_invoices.sync_org_invoices(db, ORG_INT, stripe_client=ExplodingStripe())
    assert res["status"] == "internal_tenant"


def test_sweep_excludes_internal_tenants(db, seeded):
    stub = StubStripe({CUS_A: [_inv("in_s1")], CUS_B: [_inv("in_s2", customer=CUS_B)]})
    tally = billing_invoices.sync_all_orgs(db, stripe_client=stub)
    assert tally["checked"] == 2, "internal tenant must be excluded by the query"
    assert "cus_int" not in stub.list_calls


def test_stripe_outage_is_reported_not_raised(db, seeded):
    res = billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=StubStripe(fail=True))
    assert res["status"] == "stripe_unavailable"
    assert _rows(db, ORG_A) == []


def test_org_without_stripe_customer_is_a_noop(db, seeded):
    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    sub.stripe_customer_id = None
    db.commit()
    res = billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=ExplodingStripe())
    assert res["status"] == "no_stripe_customer"


# ══════════════════════════════════════════════════════════════════════════════
# The subscription-reference defect this phase uncovered
# ══════════════════════════════════════════════════════════════════════════════
def test_subscription_reference_resolves_from_the_nested_location(db, seeded):
    """API 2026-07-29.dahlia has no top-level invoice.subscription."""
    data = _inv("in_nested", nested=True)
    assert "subscription" not in data
    assert billing_webhooks._invoice_subscription_id(data) == SUB_A


def test_subscription_reference_still_resolves_from_the_legacy_location(db, seeded):
    data = _inv("in_legacy", nested=False)
    assert billing_webhooks._invoice_subscription_id(data) == SUB_A


def test_invoice_with_no_subscription_resolves_none():
    data = _inv("in_oneoff", subscription=None)
    assert billing_webhooks._invoice_subscription_id(data) is None


def test_invoice_paid_recovers_a_past_due_subscription(db, seeded):
    """The whole point of the fix: before it, this handler returned early and the
    subscription stayed past_due forever."""
    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    sub.subscription_status = "past_due"
    db.commit()

    stub = StubStripe({CUS_A: [_inv("in_paid")]})
    org = billing_webhooks.handle_invoice_paid(db, _inv("in_paid"), stub)
    assert org == ORG_A
    db.commit()          # process_event owns the commit in the real flow
    db.expire_all()
    assert db.query(BillingSubscription).filter_by(organization_id=ORG_A).first() \
             .subscription_status == "active"


def test_invoice_paid_also_refreshes_the_invoice_mirror(db, seeded):
    stub = StubStripe({CUS_A: [_inv("in_mirror_me")]})
    billing_webhooks.handle_invoice_paid(db, _inv("in_mirror_me"), stub)
    db.commit()
    assert [r.stripe_invoice_id for r in _rows(db, ORG_A)] == ["in_mirror_me"]


def test_invoice_mirror_failure_does_not_break_the_webhook(db, seeded):
    """Invoices are a display mirror; failing to refresh them must not make Stripe
    retry a subscription update that already succeeded."""
    broken = StubStripe({CUS_A: [_inv("in_x")]})

    class _BrokenInvoice:
        @staticmethod
        def list(**kwargs):
            raise RuntimeError("invoice listing broken")

    broken.Invoice = _BrokenInvoice        # subscription retrieve still works

    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    sub.subscription_status = "past_due"
    db.commit()
    org = billing_webhooks.handle_invoice_paid(db, _inv("in_x"), broken)
    assert org == ORG_A
    db.commit()          # the state write survives a failed mirror
    db.expire_all()
    assert db.query(BillingSubscription).filter_by(organization_id=ORG_A).first() \
             .subscription_status == "active"


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════
def test_invoice_list_reports_source_and_pdf_availability(db, seeded):
    db.add(BillingInvoice(organization_id=ORG_A, invoice_number="LEGACY-1",
                          subtotal=9.0, tax_amount=0.72, total_amount=9.72,
                          status="paid", currency="USD"))
    db.commit()
    billing_invoices.sync_org_invoices(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [_inv("in_real")]}))

    actor = _actor(db, ORG_A, "list@a.test")
    with _client(db, actor) as c:
        body = c.get(f"{API}/invoices").json()
    by_num = {i["invoice_number"]: i for i in body["invoices"]}
    assert by_num["LEGACY-1"]["source"] == "legacy"
    assert by_num["LEGACY-1"]["has_pdf"] is False
    assert by_num["ZT-0001"]["source"] == "stripe"
    assert by_num["ZT-0001"]["has_pdf"] is True


def test_pdf_returns_the_stripe_hosted_url(db, seeded):
    billing_invoices.sync_org_invoices(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [_inv("in_pdf")]}))
    row = _rows(db, ORG_A)[0]
    actor = _actor(db, ORG_A, "pdf@a.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/invoices/{row.id}/pdf")
    assert r.status_code == 200
    assert r.json()["url"] == "https://pay.stripe.com/invoice/test.pdf"


def test_pdf_for_a_legacy_row_is_refused_not_fabricated(db, seeded):
    legacy = BillingInvoice(organization_id=ORG_A, invoice_number="LEGACY-2",
                            subtotal=9.0, tax_amount=0.72, total_amount=9.72,
                            status="paid", currency="USD")
    db.add(legacy); db.commit(); db.refresh(legacy)
    actor = _actor(db, ORG_A, "legacypdf@a.test")
    with _client(db, actor) as c:
        r = c.get(f"{API}/invoices/{legacy.id}/pdf")
    assert r.status_code == 409
    assert "predates" in r.json()["detail"].lower()


def test_pdf_is_still_tenant_scoped(db, seeded):
    billing_invoices.sync_org_invoices(
        db, ORG_A, stripe_client=StubStripe({CUS_A: [_inv("in_iso")]}))
    row = _rows(db, ORG_A)[0]
    intruder = _actor(db, ORG_B, "intruder@b.test")
    with _client(db, intruder) as c:
        assert c.get(f"{API}/invoices/{row.id}/pdf").status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Fabrication is impossible
# ══════════════════════════════════════════════════════════════════════════════
def test_the_invoice_fabricator_is_retired(db, seeded):
    sub = db.query(BillingSubscription).filter_by(organization_id=ORG_A).first()
    with pytest.raises(RuntimeError, match="retired"):
        billing_service.seed_demo_invoices_if_empty(db, sub)
    assert _rows(db, ORG_A) == []


def test_no_hardcoded_tax_rate_anywhere_in_billing():
    billing_dir = pathlib.Path(billing_router.__file__).parent
    offenders = []
    for path in billing_dir.glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])0\.0[1-9]\d*\b", line) and "tax" in line.lower():
                offenders.append(f"{path.name}:{i}")
    assert not offenders, offenders


def test_tax_note_no_longer_promises_tax_later(db, seeded):
    """Prices are final, so "tax calculated at invoicing" would be a fresh inaccuracy."""
    actor = _actor(db, ORG_A, "note@a.test")
    with _client(db, actor) as c:
        ni = c.get(f"{API}/summary").json()["next_invoice"]
    assert ni["tax_amount"] == 0
    assert ni["amount"] == ni["subtotal"]
    note = ni["tax_note"].lower()
    assert "calculated at invoicing" not in note
    assert "tax-inclusive" in note or "final" in note


def test_frontend_only_offers_a_pdf_when_one_exists():
    src = _JSX.read_text(encoding="utf-8")
    src = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    assert "inv.has_pdf" in src
    assert "window.open(d.url" in src, "must open Stripe's URL, not fetch it"


def test_mirror_does_not_commit_when_the_caller_owns_the_transaction(db, seeded):
    """Phase 3 processes an event as claim -> handle -> mark processed -> ONE commit.

    A commit inside the mirror would end that transaction mid-handler, and would also
    make the subscription-state write depend on invoice mirroring succeeding.
    """
    stub = StubStripe({CUS_A: [_inv("in_nocommit")]})
    res = billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub, commit=False)
    assert res["status"] == "synced" and res["created"] == 1

    # Visible inside the transaction...
    assert [r.stripe_invoice_id for r in _rows(db, ORG_A)] == ["in_nocommit"]
    # ...but not yet durable: rolling back must discard it.
    db.rollback()
    assert _rows(db, ORG_A) == []


def test_mirror_commits_when_called_standalone(db, seeded):
    """The sweep owns no outer transaction, so it must persist by itself."""
    stub = StubStripe({CUS_A: [_inv("in_commit")]})
    billing_invoices.sync_org_invoices(db, ORG_A, stripe_client=stub)
    db.rollback()
    assert [r.stripe_invoice_id for r in _rows(db, ORG_A)] == ["in_commit"]
