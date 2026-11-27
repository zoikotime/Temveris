"""Blocker 3 — detect Stripe subscriptions Temveris has no record of.

``reconcile`` already reports duplicates, but only for a customer it already knows: it
starts from ``BillingSubscription.stripe_customer_id``. A subscription whose *customer*
is also unknown is invisible to it, and that is the shape the real incidents took —
four active sandbox subscriptions billed for 11-16 days, each under its own Stripe
customer that appeared in no local row.

Every test here uses a fake Stripe client. Nothing contacts Stripe, nothing is created,
modified or cancelled, and no mail is sent.
"""

from __future__ import annotations

import pytest

from app.billing import orphans
from database.models import BillingPlan, BillingSubscription, Organization

ORG_CUST, ORG_INT = 9701, 9702


# ══════════════════════════════════════════════════════════════════════════════
# Fake Stripe — supports starting_after pagination like the real API
# ══════════════════════════════════════════════════════════════════════════════
def _sub(sub_id, customer, *, status="active", amount=15000, qty=1,
         currency="usd", interval="year", created=1_760_000_000, price_id="price_x"):
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "created": created,
        "items": {"data": [{
            "id": f"si_{sub_id}",
            "quantity": qty,
            "price": {"id": price_id, "unit_amount": amount,
                      "currency": currency, "recurring": {"interval": interval}},
        }]},
    }


class FakeSubscriptions:
    def __init__(self, pages, *, raise_on_call=None):
        # pages: list of lists of subscription dicts, in the order Stripe would serve
        self.pages = pages
        self.raise_on_call = raise_on_call
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_call is not None and len(self.calls) >= self.raise_on_call:
            raise RuntimeError("stripe is down")
        status = kwargs.get("status")
        # Only "active" is populated in these fixtures; other statuses come back empty,
        # which is what Stripe does.
        pages = self.pages if status == "active" else [[]]
        idx = 0
        after = kwargs.get("starting_after")
        if after:
            for i, page in enumerate(pages):
                if any(s["id"] == after for s in page):
                    idx = i + 1
                    break
        page = pages[idx] if idx < len(pages) else []
        return {"data": page, "has_more": idx < len(pages) - 1 and bool(page)}


class FakeStripe:
    def __init__(self, pages, *, raise_on_call=None):
        self.Subscription = FakeSubscriptions(pages, raise_on_call=raise_on_call)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(Organization).filter(
            Organization.id.in_([ORG_CUST, ORG_INT])).delete(synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG_CUST, name="Paying Co", billing_type="customer"))
    db.add(Organization(id=ORG_INT, name="Temveris", billing_type="internal"))
    plan = BillingPlan(code="governed", name="Governed", tier_order=2,
                       monthly_price_per_user=15.0, annual_price_per_user=144.0,
                       user_limit=250)
    db.add(plan)
    db.flush()
    db.add(BillingSubscription(organization_id=ORG_CUST, plan_id=plan.id,
                               billing_cycle="annual", subscription_status="active",
                               stripe_subscription_id="sub_known",
                               stripe_customer_id="cus_known", currency="USD"))
    db.add(BillingSubscription(organization_id=ORG_INT, plan_id=plan.id,
                               billing_cycle="monthly", subscription_status="active",
                               stripe_subscription_id="sub_internal",
                               stripe_customer_id="cus_internal", currency="USD"))
    db.commit()
    yield plan
    _clean()


# ══════════════════════════════════════════════════════════════════════════════
# 1-2. Matched vs orphan
# ══════════════════════════════════════════════════════════════════════════════
def test_subscription_with_a_local_row_is_not_an_orphan(db, seeded):
    stripe = FakeStripe([[_sub("sub_known", "cus_known")]])
    report = orphans.scan(db, stripe_client=stripe)
    assert report["status"] == "ok"
    assert report["orphan_count"] == 0
    assert report["matched"] == 1
    row = report["subscriptions"][0]
    assert row["orphan"] is False
    assert row["classification"] == orphans.MATCHED
    assert row["organization_id"] == ORG_CUST


def test_subscription_with_no_local_row_is_an_orphan(db, seeded):
    stripe = FakeStripe([[_sub("sub_ghost", "cus_ghost", amount=61500)]])
    report = orphans.scan(db, stripe_client=stripe)
    assert report["orphan_count"] == 1
    o = report["orphans"][0]
    assert o["stripe_subscription_id"] == "sub_ghost"
    assert o["stripe_customer_id"] == "cus_ghost"
    assert o["organization_id"] is None
    assert o["orphan"] is True


def test_the_report_carries_every_field_an_operator_needs(db, seeded):
    stripe = FakeStripe([[_sub("sub_ghost", "cus_ghost", amount=14400, qty=3,
                              currency="usd", interval="year", price_id="price_gov")]])
    o = orphans.scan(db, stripe_client=stripe)["orphans"][0]
    assert o["stripe_status"] == "active"
    assert o["price_id"] == "price_gov"
    assert o["unit_amount"] == 144.00
    assert o["quantity"] == 3
    assert o["amount"] == 432.00          # 144.00 x 3
    assert o["currency"] == "USD"
    assert o["interval"] == "year"
    assert o["created"] is not None


# ══════════════════════════════════════════════════════════════════════════════
# 3. Scope — which statuses are in play
# ══════════════════════════════════════════════════════════════════════════════
def test_only_the_requested_status_is_asked_of_stripe(db, seeded):
    """A canceled subscription bills nobody, so the default scan does not ask for it.

    Reporting cancelled subscriptions would be noise, and noise is what teaches an
    operator to stop reading the report.
    """
    stripe = FakeStripe([[_sub("sub_known", "cus_known")]])
    orphans.scan(db, stripe_client=stripe)
    assert stripe.Subscription.calls[0]["status"] == "active"
    assert "canceled" not in orphans.BILLABLE_STATUSES


def test_scan_billable_covers_every_status_that_can_still_charge(db, seeded):
    stripe = FakeStripe([[_sub("sub_ghost", "cus_ghost")]])
    report = orphans.scan_billable(db, stripe_client=stripe)
    asked = {c["status"] for c in stripe.Subscription.calls}
    assert asked == set(orphans.BILLABLE_STATUSES)
    # The same subscription surfacing under two statuses must be counted once.
    assert report["unique_subscriptions"] == 1
    assert report["orphan_count"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. Pagination
# ══════════════════════════════════════════════════════════════════════════════
def test_every_page_is_inspected_not_just_the_first(db, seeded):
    """Taking only page 1 found 2 of the 4 real orphans and called the rest clean."""
    stripe = FakeStripe([
        [_sub("sub_p1a", "cus_a"), _sub("sub_p1b", "cus_b")],
        [_sub("sub_p2a", "cus_c"), _sub("sub_known", "cus_known")],
        [_sub("sub_p3a", "cus_d")],
    ])
    report = orphans.scan(db, stripe_client=stripe)
    assert report["inspected"] == 5
    assert report["matched"] == 1
    assert report["orphan_count"] == 4
    ids = {o["stripe_subscription_id"] for o in report["orphans"]}
    assert ids == {"sub_p1a", "sub_p1b", "sub_p2a", "sub_p3a"}


def test_pagination_uses_starting_after_from_the_previous_page(db, seeded):
    stripe = FakeStripe([[_sub("sub_1", "cus_1")], [_sub("sub_2", "cus_2")]])
    orphans.scan(db, stripe_client=stripe)
    calls = stripe.Subscription.calls
    assert "starting_after" not in calls[0]
    assert calls[1]["starting_after"] == "sub_1"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Stripe failure must never read as "no orphans"
# ══════════════════════════════════════════════════════════════════════════════
def test_stripe_failure_reports_unavailable_and_not_zero_orphans(db, seeded):
    stripe = FakeStripe([[_sub("sub_ghost", "cus_ghost")]], raise_on_call=1)
    report = orphans.scan(db, stripe_client=stripe)
    assert report["status"] == "unavailable"
    assert "stripe is down" in report["error"]
    # orphans is None, deliberately NOT [] — an empty list would read as "clean".
    assert report["orphans"] is None
    assert report["orphan_count"] is None


def test_failure_partway_through_pagination_is_still_unavailable(db, seeded):
    stripe = FakeStripe([[_sub("sub_1", "cus_1")], [_sub("sub_2", "cus_2")]],
                        raise_on_call=2)
    report = orphans.scan(db, stripe_client=stripe)
    assert report["status"] == "unavailable"
    assert report["orphans"] is None
    assert report["inspected"] == 1        # partial progress is reported honestly


def test_scan_billable_reports_partial_rather_than_hiding_a_failure(db, seeded):
    stripe = FakeStripe([[_sub("sub_ghost", "cus_ghost")]], raise_on_call=3)
    report = orphans.scan_billable(db, stripe_client=stripe)
    assert report["status"] in ("partial", "unavailable")
    if report["status"] == "partial":
        assert report["errors"]


# ══════════════════════════════════════════════════════════════════════════════
# 6. Internal tenant
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_subscription_is_not_reported_as_a_customer_orphan(db, seeded):
    stripe = FakeStripe([[_sub("sub_internal", "cus_internal")]])
    report = orphans.scan(db, stripe_client=stripe)
    assert report["orphan_count"] == 0
    assert report["internal"] == 1
    assert report["subscriptions"][0]["classification"] == orphans.INTERNAL


def test_unknown_subscription_on_an_internal_customer_is_internal_not_orphan(db, seeded):
    """The leftover-sandbox case: org 1's customer with a subscription we never stored.

    The organization cannot be resolved from an unknown subscription id, so the customer
    id is what identifies it as internal.
    """
    stripe = FakeStripe([[_sub("sub_leftover", "cus_internal")]])
    report = orphans.scan(db, stripe_client=stripe)
    assert report["orphan_count"] == 0
    assert report["internal"] == 1


def test_internal_classification_uses_the_shared_helper(db, seeded, monkeypatch):
    """Not a second definition of "internal". If the helper says customer, so does this."""
    from app.billing import service as billing_service
    monkeypatch.setattr(billing_service, "is_internal_tenant", lambda db, org: False)
    stripe = FakeStripe([[_sub("sub_internal", "cus_internal")]])
    report = orphans.scan(db, stripe_client=stripe)
    assert report["internal"] == 0
    assert report["matched"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 7. Malformed Stripe data
# ══════════════════════════════════════════════════════════════════════════════
def test_a_malformed_subscription_does_not_abort_the_scan(db, seeded):
    bad = {"customer": "cus_bad"}                     # no id at all
    stripe = FakeStripe([[bad, _sub("sub_ghost", "cus_ghost")]])
    report = orphans.scan(db, stripe_client=stripe)
    assert report["status"] == "ok"
    assert report["unparsed"] == 1
    assert report["orphan_count"] == 1                # the good one still surfaced
    assert report["orphans"][0]["stripe_subscription_id"] == "sub_ghost"


def test_a_subscription_with_no_items_is_reported_without_money_fields(db, seeded):
    stripe = FakeStripe([[{"id": "sub_noitems", "customer": "cus_x",
                           "status": "active", "created": 1_760_000_000,
                           "items": {"data": []}}]])
    o = orphans.scan(db, stripe_client=stripe)["orphans"][0]
    assert o["stripe_subscription_id"] == "sub_noitems"
    assert o["amount"] is None and o["price_id"] is None


def test_an_expanded_customer_object_is_reduced_to_its_id(db, seeded):
    s = _sub("sub_ghost", {"id": "cus_expanded", "object": "customer"})
    report = orphans.scan(db, stripe_client=FakeStripe([[s]]))
    assert report["orphans"][0]["stripe_customer_id"] == "cus_expanded"


# ══════════════════════════════════════════════════════════════════════════════
# 8. A NULL local id must not create a false match
# ══════════════════════════════════════════════════════════════════════════════
def test_local_row_with_null_stripe_id_creates_no_false_match(db, seeded):
    """A NULL would otherwise key the index under None and swallow a real orphan."""
    plan = seeded
    db.add(Organization(id=9703, name="Trialing Co", billing_type="customer"))
    db.add(BillingSubscription(organization_id=9703, plan_id=plan.id,
                               billing_cycle="monthly", subscription_status="trial",
                               stripe_subscription_id=None, stripe_customer_id=None,
                               currency="USD"))
    db.commit()

    index = orphans._local_index(db)
    assert None not in index
    assert all(k is not None for k in index)

    report = orphans.scan(db, stripe_client=FakeStripe([[_sub("sub_ghost", "cus_ghost")]]))
    assert report["orphan_count"] == 1

    db.query(BillingSubscription).filter_by(organization_id=9703).delete()
    db.query(Organization).filter_by(id=9703).delete()
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Read-only guarantees
# ══════════════════════════════════════════════════════════════════════════════
def test_the_scan_writes_nothing_to_the_database(db, seeded):
    before = db.query(BillingSubscription).count()
    orphans.scan(db, stripe_client=FakeStripe([[_sub("sub_ghost", "cus_ghost")]]))
    assert db.query(BillingSubscription).count() == before


def test_the_scan_only_ever_calls_list_on_stripe(db, seeded):
    """Any create/modify/cancel would raise AttributeError on this double."""
    class ListOnly:
        class Subscription:
            calls = []
            @staticmethod
            def list(**kw):
                ListOnly.Subscription.calls.append(kw)
                return {"data": [_sub("sub_ghost", "cus_ghost")], "has_more": False}
    report = orphans.scan(db, stripe_client=ListOnly)
    assert report["orphan_count"] == 1
    assert len(ListOnly.Subscription.calls) == 1


def test_module_contains_no_stripe_mutation_calls():
    """Structural guard: the file must never gain a write call."""
    import inspect
    src = inspect.getsource(orphans)
    for forbidden in (".create(", ".modify(", ".cancel(", ".delete(",
                      ".detach(", "db.add(", "db.commit("):
        assert forbidden not in src, forbidden
