"""Phase 3 — webhook idempotency, ordering immunity, retry/dead-letter, tenancy.

Covers the defects the Phase 3 audit proved empirically:

* the same ``event.id`` executed business logic on every delivery, advancing
  ``renewal_date`` each time (``now() + 30 days`` was recomputed per delivery);
* an event created *earlier* could overwrite newer state, because the handler
  applied the payload instead of re-reading Stripe;
* a late ``invoice.payment_failed`` could resurrect a cancelled tenant;
* ``checkout.session.completed`` granted ``active`` without checking
  ``payment_status``;
* nothing verified that the Stripe ids in an event belonged to the tenant named
  by the event.

The atomic-claim test runs against PostgreSQL where available: SQLite cannot
demonstrate two concurrent transactions racing for one UNIQUE row.
"""

from __future__ import annotations

import os
import pathlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.billing import reconcile as billing_reconcile
from app.billing import seats as billing_seats
from app.billing import webhooks as wh
from app.billing import worker_state
from database.models import (
    BillingPlan, BillingSubscription, BillingWebhookEvent, Organization, User,
)

ORG_A = 7601
ORG_B = 7602
SUB_A = "sub_p3A"
CUS_A = "cus_p3A"
ITEM_A = "si_p3A"
SUB_B = "sub_p3B"
CUS_B = "cus_p3B"
PRICE_GOV_MONTHLY = "price_gov_monthly_p3"
PRICE_GOV_ANNUAL = "price_gov_annual_p3"
PRICE_VER_MONTHLY = "price_ver_monthly_p3"

# A fixed, far-future period end so assertions never depend on the clock.
PERIOD_END_TS = 1893456000                     # 2030-01-01T00:00:00Z
PERIOD_END = datetime.fromtimestamp(PERIOD_END_TS, tz=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# Stripe double
# ══════════════════════════════════════════════════════════════════════════════
class StubStripe:
    """Authoritative Stripe state, independent of whatever an event claims.

    Every handler re-fetches through this, which is exactly the property under
    test: a stale event must not be able to change what we read here.
    """

    def __init__(self, *, status="active", customer=CUS_A, quantity=3,
                 price=PRICE_GOV_MONTHLY, interval="month",
                 cancel_at_period_end=False, canceled_at=None,
                 period_end=PERIOD_END_TS, fail_retrieve=False,
                 extra_active=(), subscription_id=SUB_A):
        self.state = dict(
            status=status, customer=customer, quantity=quantity, price=price,
            interval=interval, cancel_at_period_end=cancel_at_period_end,
            canceled_at=canceled_at, period_end=period_end,
            subscription_id=subscription_id,
        )
        self.fail_retrieve = fail_retrieve
        self.extra_active = list(extra_active)
        self.retrieve_calls, self.modify_calls = [], []
        outer = self

        class _Subscription:
            @staticmethod
            def retrieve(sid, *a, **k):
                outer.retrieve_calls.append(sid)
                if outer.fail_retrieve:
                    raise RuntimeError("stripe unreachable")
                s = outer.state
                return {
                    "id": sid,
                    "customer": s["customer"],
                    "status": s["status"],
                    "cancel_at_period_end": s["cancel_at_period_end"],
                    "canceled_at": s["canceled_at"],
                    "current_period_end": s["period_end"],
                    "items": {"data": [{
                        "id": ITEM_A,
                        "quantity": s["quantity"],
                        "current_period_end": s["period_end"],
                        "price": {"id": s["price"],
                                  "recurring": {"interval": s["interval"]}},
                    }]},
                }

            @staticmethod
            def modify(sid, **kwargs):
                outer.modify_calls.append((sid, kwargs))
                for it in kwargs.get("items", []):
                    if "quantity" in it:
                        outer.state["quantity"] = it["quantity"]
                return {"id": sid, "status": outer.state["status"]}

            @staticmethod
            def list(**kwargs):
                return {"data": [{"id": s} for s in
                                 [outer.state["subscription_id"], *outer.extra_active]]}

        self.Subscription = _Subscription


def event(event_type, obj, *, event_id="evt_1", created=1_700_000_000):
    return {"id": event_id, "type": event_type, "created": created,
            "api_version": "2024-06-20", "data": {"object": obj}}


def session_obj(*, org=ORG_A, payment_status="paid", subscription=SUB_A,
                customer=CUS_A, mode="subscription"):
    return {"id": "cs_1", "object": "checkout.session",
            "client_reference_id": str(org), "payment_status": payment_status,
            "subscription": subscription, "customer": customer, "mode": mode}


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def price_env(monkeypatch):
    """The price->plan catalog is read from env; there is no local price column."""
    monkeypatch.setenv("STRIPE_PRICE_GOVERNED_MONTHLY", PRICE_GOV_MONTHLY)
    monkeypatch.setenv("STRIPE_PRICE_GOVERNED_ANNUAL", PRICE_GOV_ANNUAL)
    monkeypatch.setenv("STRIPE_PRICE_VERIFIED_MONTHLY", PRICE_VER_MONTHLY)


@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingWebhookEvent).delete()
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
    gov = BillingPlan(code="governed", name="Governed", tier_order=2,
                      monthly_price_per_user=15.0, annual_price_per_user=144.0,
                      user_limit=100)
    ver = BillingPlan(code="verified", name="Verified", tier_order=1,
                      monthly_price_per_user=8.0, annual_price_per_user=76.80,
                      user_limit=25)
    db.add_all([gov, ver])
    db.flush()
    db.add(BillingSubscription(
        organization_id=ORG_A, plan_id=gov.id, billing_cycle="monthly",
        subscription_status="active", stripe_subscription_id=SUB_A,
        stripe_customer_id=CUS_A, active_users=3, currency="USD"))
    db.add(BillingSubscription(
        organization_id=ORG_B, plan_id=gov.id, billing_cycle="monthly",
        subscription_status="active", stripe_subscription_id=SUB_B,
        stripe_customer_id=CUS_B, active_users=1, currency="USD"))
    db.commit()
    yield
    _clean()


def _utc(value):
    """Normalise a stored timestamp for comparison.

    conftest runs the suite on SQLite, which drops tzinfo from
    DateTime(timezone=True); PostgreSQL keeps it. Compare instants, not tzinfo.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def sub_a(db):
    db.expire_all()
    return (db.query(BillingSubscription)
              .filter(BillingSubscription.organization_id == ORG_A).first())


def sub_b(db):
    db.expire_all()
    return (db.query(BillingSubscription)
              .filter(BillingSubscription.organization_id == ORG_B).first())


def ledger(db, event_id):
    db.expire_all()
    return (db.query(BillingWebhookEvent)
              .filter(BillingWebhookEvent.stripe_event_id == event_id).first())


# ══════════════════════════════════════════════════════════════════════════════
# 1-3, 15-16 — idempotency
# ══════════════════════════════════════════════════════════════════════════════
def test_same_event_three_times_executes_business_logic_once(db, seeded):
    stub = StubStripe(quantity=3)
    ev = event("customer.subscription.updated", {"id": SUB_A, "status": "past_due"},
               event_id="evt_triple")
    stub.state["status"] = "past_due"

    results = [wh.process_event(db, ev, stripe_client=stub) for _ in range(3)]

    assert [r["status"] for r in results] == ["processed", "duplicate", "duplicate"]
    # One claim row, and Stripe was re-fetched only for the delivery that ran.
    assert db.query(BillingWebhookEvent).filter(
        BillingWebhookEvent.stripe_event_id == "evt_triple").count() == 1
    assert stub.retrieve_calls == [SUB_A]
    assert sub_a(db).subscription_status == "past_due"


def test_duplicate_delivery_does_not_change_renewal_date(db, seeded):
    """The headline Phase 3 defect: renewal_date advanced on every re-delivery."""
    stub = StubStripe()
    ev = event("checkout.session.completed", session_obj(), event_id="evt_renew")

    assert wh.process_event(db, ev, stripe_client=stub)["status"] == "processed"
    first = sub_a(db).renewal_date

    for _ in range(4):
        assert wh.process_event(db, ev, stripe_client=stub)["status"] == "duplicate"

    assert sub_a(db).renewal_date == first


def test_successful_processing_marks_processed(db, seeded):
    stub = StubStripe()
    ev = event("invoice.paid", {"subscription": SUB_A}, event_id="evt_ok")
    wh.process_event(db, ev, stripe_client=stub)

    row = ledger(db, "evt_ok")
    assert row.status == wh.PROCESSED
    assert row.processed_at is not None
    assert row.organization_id == ORG_A
    assert row.last_error is None
    assert row.event_type == "invoice.paid"
    assert row.api_version == "2024-06-20"


def test_processed_event_replay_is_a_no_op(db, seeded):
    stub = StubStripe(status="active")
    ev = event("customer.subscription.updated", {"id": SUB_A, "status": "active"},
               event_id="evt_replay")
    wh.process_event(db, ev, stripe_client=stub)

    # Stripe state moves on, but replaying the old event must change nothing.
    stub.state["status"] = "past_due"
    result = wh.process_event(db, ev, stripe_client=stub)

    assert result["status"] == "duplicate"
    assert sub_a(db).subscription_status == "active"


def test_event_without_id_is_refused(db, seeded):
    """No id means no idempotency key — processing blind is not an option."""
    result = wh.process_event(db, {"type": "invoice.paid", "data": {"object": {}}},
                              stripe_client=StubStripe())
    assert result["status"] == "rejected"
    assert "no id" in result["reason"]


def test_stored_payload_is_redacted_to_a_whitelist(db, seeded):
    stub = StubStripe()
    obj = dict(session_obj())
    obj["payment_method_details"] = {"card": {"number": "4242424242424242"}}
    obj["client_secret"] = "secret_value"
    wh.process_event(db, event("checkout.session.completed", obj, event_id="evt_redact"),
                     stripe_client=stub)

    payload = ledger(db, "evt_redact").payload
    assert "payment_method_details" not in payload
    assert "client_secret" not in payload
    assert payload["payment_status"] == "paid"
    assert "4242424242424242" not in str(payload)


# ══════════════════════════════════════════════════════════════════════════════
# 2, 11 — database-enforced atomic claim (PostgreSQL)
# ══════════════════════════════════════════════════════════════════════════════
def _pg_url():
    """The DEV PostgreSQL URL, read from backend/.env.

    conftest deliberately forces ``DATABASE_URL`` to in-memory SQLite for the
    whole suite, so the env var cannot be used here. SQLite cannot demonstrate two
    concurrent transactions racing for one UNIQUE row — the property these tests
    exist to prove — so the real DEV database is addressed directly.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:                              # pragma: no cover
        return None
    env_path = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return None
    url = (dotenv_values(env_path) or {}).get("DATABASE_URL") or ""
    # Never let these tests touch anything but a local dev database.
    if not url.startswith("postgresql"):
        return None
    if not any(h in url for h in ("127.0.0.1", "localhost")):
        return None
    return url


pg_only = pytest.mark.skipif(
    _pg_url() is None,
    reason="atomic-claim race needs the local PostgreSQL DEV database")


@pg_only
def test_duplicate_event_insert_is_rejected_by_the_database(db, seeded):
    eng = create_engine(_pg_url())
    try:
        with eng.begin() as c:
            c.execute(text("DELETE FROM billing_webhook_events "
                           "WHERE stripe_event_id = 'evt_pg_dup'"))
            c.execute(text("INSERT INTO billing_webhook_events "
                           "(stripe_event_id, event_type) VALUES ('evt_pg_dup','x')"))
        with pytest.raises(IntegrityError):
            with eng.begin() as c:
                c.execute(text("INSERT INTO billing_webhook_events "
                               "(stripe_event_id, event_type) VALUES ('evt_pg_dup','x')"))
        with eng.begin() as c:
            c.execute(text("DELETE FROM billing_webhook_events "
                           "WHERE stripe_event_id = 'evt_pg_dup'"))
    finally:
        eng.dispose()


@pg_only
def test_concurrent_duplicate_deliveries_yield_exactly_one_claim(db, seeded):
    """12 threads, one event id. The UNIQUE constraint is the arbiter."""
    url = _pg_url()
    admin = create_engine(url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM billing_webhook_events "
                       "WHERE stripe_event_id = 'evt_pg_race'"))

    claimed, duplicate, errors = [], [], []
    lock = threading.Lock()

    def worker(i):
        eng = create_engine(url)
        Session = sessionmaker(bind=eng)
        s = Session()
        try:
            row, claimed_ok = wh.claim_event(
                s, event("invoice.paid", {"subscription": SUB_A},
                         event_id="evt_pg_race"))
            if claimed_ok:
                # The real protocol commits the claim together with the business
                # update. Holding the transaction open here is what forces the
                # other 11 deliveries to block on the UNIQUE index and lose.
                row.status = wh.PROCESSED
                s.commit()
            with lock:
                (claimed if claimed_ok else duplicate).append(i)
        except Exception as exc:                       # pragma: no cover
            s.rollback()
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            s.close()
            eng.dispose()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    with admin.connect() as c:
        rows = c.execute(text("SELECT count(*) FROM billing_webhook_events "
                              "WHERE stripe_event_id = 'evt_pg_race'")).scalar()
    with admin.begin() as c:
        c.execute(text("DELETE FROM billing_webhook_events "
                       "WHERE stripe_event_id = 'evt_pg_race'"))
    admin.dispose()

    assert not errors, errors
    assert len(claimed) == 1, f"expected 1 winner, got {len(claimed)}"
    assert len(duplicate) == 11
    assert rows == 1


@pg_only
def test_claim_is_invisible_to_other_deliveries_until_business_work_commits():
    """The claim must not be committed ahead of the business update.

    If it were, a second delivery would find a committed ``received`` row and be
    unable to tell "another worker is mid-flight" from "a worker crashed half
    way" — so it would process the event a second time. Keeping claim and
    business in one transaction removes that ambiguity: until the work lands,
    no other session can see the claim at all.
    """
    url = _pg_url()
    writer_eng, reader_eng = create_engine(url), create_engine(url)
    writer = sessionmaker(bind=writer_eng)()
    ev = event("invoice.paid", {"subscription": SUB_A}, event_id="evt_pg_visible")

    def reader_sees():
        with reader_eng.connect() as c:
            return c.execute(text(
                "SELECT status FROM billing_webhook_events "
                "WHERE stripe_event_id = 'evt_pg_visible'")).fetchone()

    try:
        with writer_eng.begin() as c:
            c.execute(text("DELETE FROM billing_webhook_events "
                           "WHERE stripe_event_id = 'evt_pg_visible'"))

        row, claimed = wh.claim_event(writer, ev)
        assert claimed
        # Business work would happen here; nothing is committed yet.
        assert reader_sees() is None, \
            "claim leaked to another session before the business update committed"

        row.status = wh.PROCESSED
        writer.commit()
        assert reader_sees()[0] == wh.PROCESSED
    finally:
        writer.close()
        with writer_eng.begin() as c:
            c.execute(text("DELETE FROM billing_webhook_events "
                           "WHERE stripe_event_id = 'evt_pg_visible'"))
        writer_eng.dispose()
        reader_eng.dispose()


def test_no_committed_row_is_ever_left_in_received(db, seeded):
    """``received`` is a transient, uncommitted state by construction.

    Every terminal path must leave the ledger in processed / failed / dead_letter,
    so a committed ``received`` row can never be mistaken for retryable work.
    """
    stub_ok = StubStripe()
    stub_bad = StubStripe(fail_retrieve=True)

    wh.process_event(db, event("invoice.paid", {"subscription": SUB_A},
                               event_id="evt_state_ok"), stripe_client=stub_ok)
    wh.process_event(db, event("invoice.paid", {"subscription": SUB_A},
                               event_id="evt_state_fail"), stripe_client=stub_bad)
    wh.process_event(db, event("customer.subscription.updated",
                               {"id": SUB_A}, event_id="evt_state_rej"),
                     stripe_client=StubStripe(customer="cus_WRONG"))

    db.expire_all()
    statuses = {r.stripe_event_id: r.status
                for r in db.query(BillingWebhookEvent).all()}
    assert statuses["evt_state_ok"] == wh.PROCESSED
    assert statuses["evt_state_fail"] == wh.FAILED
    assert statuses["evt_state_rej"] == wh.DEAD_LETTER
    assert wh.RECEIVED not in statuses.values()


@pg_only
def test_duplicate_stripe_subscription_id_is_rejected_by_db_constraint():
    """Phase 1.5's partial UNIQUE index is what makes webhook routing unambiguous.

    Runs wholly inside the DEV database with organization_id NULL, so it needs no
    seeded tenants and cannot disturb existing dev rows. PostgreSQL permits many
    NULLs under UNIQUE(organization_id), which is what makes that possible.
    """
    eng = create_engine(_pg_url())
    ins = text("INSERT INTO billing_subscriptions (organization_id, stripe_subscription_id) "
               "VALUES (NULL, :sid)")
    try:
        with eng.begin() as c:
            c.execute(text("DELETE FROM billing_subscriptions "
                           "WHERE stripe_subscription_id = :sid"), {"sid": "sub_pg_uniq"})
            c.execute(ins, {"sid": "sub_pg_uniq"})

        with pytest.raises(IntegrityError):
            with eng.begin() as c:
                c.execute(ins, {"sid": "sub_pg_uniq"})
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM billing_subscriptions "
                           "WHERE stripe_subscription_id = :sid"), {"sid": "sub_pg_uniq"})
        eng.dispose()


# ══════════════════════════════════════════════════════════════════════════════
# 4, 5 — checkout must be paid; period end comes from Stripe
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("payment_status", ["unpaid", "no_payment_required", "paid"])
def test_checkout_activates_only_when_paid(db, seeded, payment_status):
    stub = StubStripe()
    s = sub_a(db)
    s.subscription_status = "trial"
    s.stripe_subscription_id = None
    db.commit()

    result = wh.process_event(
        db, event("checkout.session.completed",
                  session_obj(payment_status=payment_status),
                  event_id=f"evt_pay_{payment_status}"),
        stripe_client=stub)

    if payment_status == "unpaid":
        assert result["status"] == "rejected"
        assert "unpaid_checkout_session" in result["reason"]
        assert sub_a(db).subscription_status == "trial"
        assert sub_a(db).stripe_subscription_id is None
    else:
        assert result["status"] == "processed"
        assert sub_a(db).subscription_status == "active"


def test_unpaid_checkout_is_terminal_not_retried(db, seeded):
    """An unpaid session will not become paid, so retrying is pointless."""
    stub = StubStripe()
    wh.process_event(db, event("checkout.session.completed",
                               session_obj(payment_status="unpaid"),
                               event_id="evt_unpaid_dl"), stripe_client=stub)
    row = ledger(db, "evt_unpaid_dl")
    assert row.status == wh.DEAD_LETTER
    assert "unpaid_checkout_session" in row.last_error


def test_renewal_date_comes_from_stripe_current_period_end(db, seeded):
    stub = StubStripe(period_end=PERIOD_END_TS)
    wh.process_event(db, event("customer.subscription.updated",
                               {"id": SUB_A, "status": "active"},
                               event_id="evt_period"), stripe_client=stub)

    assert _utc(sub_a(db).renewal_date) == PERIOD_END
    # Explicitly not a computed 30/365-day window.
    assert abs((_utc(sub_a(db).renewal_date)
                - (datetime.now(timezone.utc) + timedelta(days=30))).days) > 1


def test_period_end_read_from_subscription_item_when_absent_at_top_level(db, seeded):
    """Newer Stripe API versions moved current_period_end onto the item."""
    stub = StubStripe(period_end=PERIOD_END_TS)
    original = stub.Subscription.retrieve

    def item_only(sid, *a, **k):
        remote = dict(original(sid))
        remote.pop("current_period_end")
        return remote

    stub.Subscription.retrieve = staticmethod(item_only)
    wh.process_event(db, event("customer.subscription.updated",
                               {"id": SUB_A, "status": "active"},
                               event_id="evt_item_period"), stripe_client=stub)
    assert _utc(sub_a(db).renewal_date) == PERIOD_END


# ══════════════════════════════════════════════════════════════════════════════
# 6, 7, 8 — ordering immunity and status precedence
# ══════════════════════════════════════════════════════════════════════════════
def test_old_event_cannot_overwrite_newer_stripe_state(db, seeded):
    """Stripe says active; a stale past_due event must not win."""
    stub = StubStripe(status="active")
    stale = event("customer.subscription.updated",
                  {"id": SUB_A, "status": "past_due"},   # payload is stale
                  event_id="evt_stale", created=1)

    wh.process_event(db, stale, stripe_client=stub)

    # Derived from the re-fetch, not from the payload.
    assert sub_a(db).subscription_status == "active"


def test_canceled_subscription_is_not_resurrected_by_payment_failed(db, seeded):
    stub = StubStripe(status="canceled")
    wh.process_event(db, event("customer.subscription.deleted", {"id": SUB_A},
                               event_id="evt_cancel"), stripe_client=stub)
    assert sub_a(db).subscription_status == "canceled"

    # A late payment_failed arrives afterwards. Stripe still reports canceled.
    result = wh.process_event(db, event("invoice.payment_failed",
                                        {"subscription": SUB_A},
                                        event_id="evt_late_fail"),
                              stripe_client=stub)

    assert sub_a(db).subscription_status == "canceled"
    assert result["status"] == "processed"     # correctly applied as a no-op


def test_backward_transition_out_of_terminal_state_is_refused(db, seeded):
    """Defence in depth: if Stripe somehow reports a live status for a locally
    cancelled subscription, refuse rather than resurrect the tenant."""
    s = sub_a(db)
    s.subscription_status = "canceled"
    db.commit()

    stub = StubStripe(status="active")          # disagrees with local terminal state
    result = wh.process_event(db, event("customer.subscription.updated",
                                        {"id": SUB_A, "status": "active"},
                                        event_id="evt_resurrect"),
                              stripe_client=stub)

    assert result["status"] == "rejected"
    assert "backward_transition" in result["reason"]
    assert sub_a(db).subscription_status == "canceled"
    assert ledger(db, "evt_resurrect").status == wh.DEAD_LETTER


@pytest.mark.parametrize("stripe_status,expected", [
    ("active", "active"), ("trialing", "trial"), ("past_due", "past_due"),
    ("unpaid", "past_due"), ("canceled", "canceled"),
    ("incomplete", "incomplete"), ("incomplete_expired", "incomplete_expired"),
])
def test_status_map_is_applied_from_authoritative_state(db, seeded, stripe_status,
                                                        expected):
    stub = StubStripe(status=stripe_status)
    wh.process_event(db, event("customer.subscription.updated",
                               {"id": SUB_A, "status": stripe_status},
                               event_id=f"evt_map_{stripe_status}"),
                     stripe_client=stub)
    assert sub_a(db).subscription_status == expected


@pytest.mark.parametrize("start", ["past_due", "incomplete"])
def test_invoice_paid_recovers_non_terminal_states(db, seeded, start):
    """The old handler only cleared past_due, so `incomplete` could never recover."""
    s = sub_a(db)
    s.subscription_status = start
    db.commit()

    stub = StubStripe(status="active")          # Stripe confirms payment
    wh.process_event(db, event("invoice.paid", {"subscription": SUB_A},
                               event_id=f"evt_recover_{start}"), stripe_client=stub)

    assert sub_a(db).subscription_status == "active"


def test_invoice_paid_without_subscription_is_a_noop(db, seeded):
    stub = StubStripe()
    result = wh.process_event(db, event("invoice.paid", {"subscription": None},
                                        event_id="evt_oneoff"), stripe_client=stub)
    assert result["status"] == "processed"
    assert result["organization_id"] is None
    assert stub.retrieve_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 9, 10, 12 — tenant isolation and ownership
# ══════════════════════════════════════════════════════════════════════════════
def test_stripe_customer_mismatch_is_rejected(db, seeded):
    """The subscription we hold belongs to a different Stripe customer."""
    stub = StubStripe(customer="cus_SOMEONE_ELSE")
    result = wh.process_event(db, event("customer.subscription.updated",
                                        {"id": SUB_A, "status": "canceled"},
                                        event_id="evt_mismatch"),
                              stripe_client=stub)

    assert result["status"] == "rejected"
    assert "customer_mismatch" in result["reason"]
    assert sub_a(db).subscription_status == "active"      # untouched
    row = ledger(db, "evt_mismatch")
    assert row.status == wh.DEAD_LETTER
    assert "customer_mismatch" in row.last_error


def test_event_for_org_a_cannot_modify_org_b(db, seeded):
    stub = StubStripe(status="canceled")
    b_before = (sub_b(db).subscription_status, sub_b(db).stripe_subscription_id,
                sub_b(db).stripe_customer_id)

    wh.process_event(db, event("customer.subscription.updated",
                               {"id": SUB_A, "status": "canceled"},
                               event_id="evt_isolation"), stripe_client=stub)

    assert sub_a(db).subscription_status == "canceled"
    assert (sub_b(db).subscription_status, sub_b(db).stripe_subscription_id,
            sub_b(db).stripe_customer_id) == b_before


def test_checkout_cannot_assign_another_orgs_subscription_id(db, seeded):
    """Requirement 12: an id in a signed event is not proof of ownership.

    Org A's ids are NULL, and a session claiming Org A carries Org B's
    subscription id. Without the guard, Org A absorbs it and every later lookup
    by that id resolves to an arbitrary one of the two rows.
    """
    s = sub_a(db)
    s.stripe_subscription_id = None
    s.stripe_customer_id = None
    db.commit()

    stub = StubStripe()
    result = wh.process_event(
        db, event("checkout.session.completed",
                  session_obj(org=ORG_A, subscription=SUB_B, customer=CUS_B),
                  event_id="evt_cross"),
        stripe_client=stub)

    assert result["status"] == "rejected"
    assert "cross_tenant" in result["reason"]
    assert sub_a(db).stripe_subscription_id is None
    assert sub_b(db).stripe_subscription_id == SUB_B      # B untouched


def test_checkout_refuses_to_replace_an_existing_subscription_id(db, seeded):
    """Phase 2.1 guarantee: one subscription per org, never silently swapped."""
    stub = StubStripe()
    result = wh.process_event(
        db, event("checkout.session.completed",
                  session_obj(subscription="sub_BRAND_NEW", customer=CUS_A),
                  event_id="evt_replace"),
        stripe_client=stub)

    assert result["status"] == "rejected"
    assert "would_replace_subscription_id" in result["reason"]
    assert sub_a(db).stripe_subscription_id == SUB_A


@pytest.mark.parametrize("bad_ref", ["default", "", "abc", None])
def test_checkout_never_guesses_a_tenant(db, seeded, bad_ref):
    stub = StubStripe()
    obj = dict(session_obj())
    obj["client_reference_id"] = bad_ref
    result = wh.process_event(db, event("checkout.session.completed", obj,
                                        event_id=f"evt_ref_{bad_ref}"),
                              stripe_client=stub)
    assert result["status"] == "rejected"
    assert "unresolvable_tenant" in result["reason"]


def test_setup_mode_checkout_grants_no_plan(db, seeded):
    s = sub_a(db)
    s.subscription_status = "trial"
    db.commit()

    stub = StubStripe()
    result = wh.process_event(
        db, event("checkout.session.completed",
                  session_obj(mode="setup", subscription=None),
                  event_id="evt_setup"), stripe_client=stub)

    assert result["status"] == "processed"
    assert sub_a(db).subscription_status == "trial"      # no plan granted


# ══════════════════════════════════════════════════════════════════════════════
# 12, 13, 14 — retry and dead letter (K5)
# ══════════════════════════════════════════════════════════════════════════════
def test_failed_processing_increments_attempts(db, seeded):
    stub = StubStripe(fail_retrieve=True)
    ev = event("invoice.paid", {"subscription": SUB_A}, event_id="evt_retry")

    for expected in (1, 2, 3):
        result = wh.process_event(db, ev, stripe_client=stub)
        assert result["status"] == "failed"
        row = ledger(db, "evt_retry")
        assert row.attempts == expected
        assert row.status == wh.FAILED
        assert row.processed_at is None
        assert "stripe_retrieve_failed" in row.last_error


def test_fifth_failure_moves_event_to_dead_letter(db, seeded):
    stub = StubStripe(fail_retrieve=True)
    ev = event("invoice.paid", {"subscription": SUB_A}, event_id="evt_dead")

    for _ in range(wh.MAX_ATTEMPTS):
        wh.process_event(db, ev, stripe_client=stub)

    row = ledger(db, "evt_dead")
    assert row.attempts == wh.MAX_ATTEMPTS == 5
    assert row.status == wh.DEAD_LETTER

    # Further deliveries stop processing entirely.
    calls_before = len(stub.retrieve_calls)
    result = wh.process_event(db, ev, stripe_client=stub)
    assert result["status"] == "duplicate"
    assert len(stub.retrieve_calls) == calls_before


def test_retry_after_failure_can_succeed(db, seeded):
    stub = StubStripe(fail_retrieve=True, status="past_due")
    ev = event("invoice.paid", {"subscription": SUB_A}, event_id="evt_recovers")

    assert wh.process_event(db, ev, stripe_client=stub)["status"] == "failed"
    assert ledger(db, "evt_recovers").attempts == 1

    stub.fail_retrieve = False                  # Stripe comes back
    result = wh.process_event(db, ev, stripe_client=stub)

    assert result["status"] == "processed"
    row = ledger(db, "evt_recovers")
    assert row.status == wh.PROCESSED
    assert row.processed_at is not None
    assert row.attempts == 1                    # not incremented by success
    assert sub_a(db).subscription_status == "past_due"


def test_failure_does_not_partially_apply_business_state(db, seeded):
    before = (sub_a(db).subscription_status, sub_a(db).renewal_date,
              sub_a(db).billing_cycle)
    stub = StubStripe(fail_retrieve=True)
    wh.process_event(db, event("customer.subscription.updated",
                               {"id": SUB_A, "status": "canceled"},
                               event_id="evt_partial"), stripe_client=stub)

    assert (sub_a(db).subscription_status, sub_a(db).renewal_date,
            sub_a(db).billing_cycle) == before


def test_unhandled_event_type_is_acknowledged_without_business_logic(db, seeded):
    """K3: acknowledged so Stripe stops retrying, but nothing is applied."""
    stub = StubStripe()
    result = wh.process_event(db, event("invoice.payment_succeeded",
                                        {"subscription": SUB_A},
                                        event_id="evt_unhandled"),
                              stripe_client=stub)

    assert result["status"] == "ignored"
    assert ledger(db, "evt_unhandled").status == wh.PROCESSED
    assert stub.retrieve_calls == []


def test_double_counted_payment_events_are_not_handled(db, seeded):
    """One payment must not be processed through three event types."""
    for t in ("invoice.payment_succeeded", "invoice_payment.paid",
              "payment_intent.succeeded", "charge.succeeded"):
        assert t not in wh.HANDLED_EVENTS
    assert "invoice.paid" in wh.HANDLED_EVENTS
    assert len(wh.HANDLED_EVENTS) == 5


# ══════════════════════════════════════════════════════════════════════════════
# 20, 21 — cancellation representation (K6)
# ══════════════════════════════════════════════════════════════════════════════
def test_subscription_deleted_sets_canceled(db, seeded):
    stub = StubStripe(status="canceled")
    wh.process_event(db, event("customer.subscription.deleted", {"id": SUB_A},
                               event_id="evt_del"), stripe_client=stub)
    assert sub_a(db).subscription_status == "canceled"


def test_subscription_deleted_is_idempotent(db, seeded):
    stub = StubStripe(status="canceled")
    for i in range(3):
        wh.process_event(db, event("customer.subscription.deleted", {"id": SUB_A},
                                   event_id=f"evt_del_{i}"), stripe_client=stub)
    assert sub_a(db).subscription_status == "canceled"


def test_subscription_deleted_applies_even_if_refetch_fails(db, seeded):
    """Deletion is terminal and monotonic, so it cannot move state backwards."""
    stub = StubStripe(fail_retrieve=True)
    result = wh.process_event(db, event("customer.subscription.deleted",
                                        {"id": SUB_A}, event_id="evt_del_nofetch"),
                              stripe_client=stub)
    assert result["status"] == "processed"
    assert sub_a(db).subscription_status == "canceled"


def test_cancel_at_period_end_is_represented_as_cancel_pending(db, seeded):
    """Stripe keeps the subscription active; locally it must read cancel_pending
    and record when cancellation was requested — reusing existing fields."""
    requested_ts = 1_800_000_000
    stub = StubStripe(status="active", cancel_at_period_end=True,
                      canceled_at=requested_ts)
    wh.process_event(db, event("customer.subscription.updated",
                               {"id": SUB_A, "status": "active"},
                               event_id="evt_cap"), stripe_client=stub)

    s = sub_a(db)
    assert s.subscription_status == "cancel_pending"
    assert _utc(s.cancellation_requested_at) == datetime.fromtimestamp(
        requested_ts, tz=timezone.utc)
    assert _utc(s.renewal_date) == PERIOD_END    # service runs to period end


def test_reversing_a_scheduled_cancellation_clears_it(db, seeded):
    stub = StubStripe(status="active", cancel_at_period_end=True,
                      canceled_at=1_800_000_000)
    wh.process_event(db, event("customer.subscription.updated", {"id": SUB_A},
                               event_id="evt_cap_on"), stripe_client=stub)
    assert sub_a(db).subscription_status == "cancel_pending"

    stub.state["cancel_at_period_end"] = False
    stub.state["canceled_at"] = None
    wh.process_event(db, event("customer.subscription.updated", {"id": SUB_A},
                               event_id="evt_cap_off"), stripe_client=stub)

    s = sub_a(db)
    assert s.subscription_status == "active"
    assert s.cancellation_requested_at is None


def test_trialing_with_scheduled_cancellation_is_cancel_pending(db, seeded):
    stub = StubStripe(status="trialing", cancel_at_period_end=True)
    wh.process_event(db, event("customer.subscription.updated", {"id": SUB_A},
                               event_id="evt_cap_trial"), stripe_client=stub)
    assert sub_a(db).subscription_status == "cancel_pending"


# ══════════════════════════════════════════════════════════════════════════════
# 22-25 — reconciliation
# ══════════════════════════════════════════════════════════════════════════════
def test_reconcile_detects_and_corrects_status_divergence(db, seeded):
    s = sub_a(db)
    s.subscription_status = "active"
    db.commit()
    stub = StubStripe(status="past_due")

    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)

    assert report["status"] == "corrected"
    assert report["divergences"]["subscription_status"]["stripe"] == "past_due"
    assert sub_a(db).subscription_status == "past_due"


def test_reconcile_detects_and_corrects_plan_divergence(db, seeded):
    """Stripe moved to the verified price; the local plan still says governed."""
    governed = db.query(BillingPlan).filter(BillingPlan.code == "governed").first()
    verified = db.query(BillingPlan).filter(BillingPlan.code == "verified").first()
    assert sub_a(db).plan_id == governed.id

    stub = StubStripe(price=PRICE_VER_MONTHLY)
    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)

    assert report["status"] == "corrected"
    assert "plan_id" in report["divergences"]
    assert sub_a(db).plan_id == verified.id


def test_reconcile_detects_and_corrects_billing_cycle_divergence(db, seeded):
    assert sub_a(db).billing_cycle == "monthly"
    stub = StubStripe(price=PRICE_GOV_ANNUAL, interval="year")

    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)

    assert report["divergences"]["billing_cycle"]["stripe"] == "annual"
    assert sub_a(db).billing_cycle == "annual"


def test_reconcile_detect_only_mode_writes_nothing(db, seeded):
    stub = StubStripe(status="past_due")
    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub,
                                                      apply=False)
    assert report["status"] == "divergent"
    assert "subscription_status" in report["divergences"]
    assert sub_a(db).subscription_status == "active"     # unchanged


def test_reconcile_reports_customer_mismatch_without_rewriting_it(db, seeded):
    """Identity anomalies are surfaced, never silently 'corrected'."""
    stub = StubStripe(customer="cus_OTHER_TENANT")
    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)

    assert report["status"] == "anomaly"
    fields = [a["field"] for a in report["anomalies"]]
    assert "stripe_customer_id" in fields
    assert all(a["action"] == "reported_only" for a in report["anomalies"])
    assert sub_a(db).stripe_customer_id == CUS_A        # not overwritten


def test_reconcile_reports_duplicate_active_stripe_subscriptions(db, seeded):
    """The Phase 2.1 failure mode: a second subscription billing in parallel."""
    stub = StubStripe(extra_active=["sub_SHADOW_DUPLICATE"])
    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)

    anomaly = next(a for a in report["anomalies"]
                   if a["field"] == "stripe_subscription_id")
    assert "sub_SHADOW_DUPLICATE" in anomaly["stripe_extra_active"]
    assert anomaly["action"] == "reported_only"
    assert sub_a(db).stripe_subscription_id == SUB_A


def test_reconcile_delegates_quantity_to_phase22_seat_sync(db, seeded):
    """Quantity must not be reimplemented here — seats.py owns those writes."""
    for i in range(6):
        db.add(User(name=f"q{i}", email=f"q{i}@alpha.test", organization_id=ORG_A,
                    role="employee", is_active=True,
                    billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    desired = billing_seats.desired_seat_quantity(db, ORG_A)

    stub = StubStripe(quantity=1)
    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)

    assert report["divergences"]["quantity"] == {"local": desired, "stripe": 1}
    assert report["quantity_sync"] == "updated"
    assert stub.state["quantity"] == desired
    # Delegated: the seat-only modify shape from Phase 2.2/2.3 is what ran.
    _sid, kwargs = stub.modify_calls[-1]
    assert kwargs["payment_behavior"] == "error_if_incomplete"
    assert set(kwargs["items"][0]) == {"id", "quantity"}


def test_reconcile_is_quiet_when_everything_agrees(db, seeded):
    stub = StubStripe(status="active", price=PRICE_GOV_MONTHLY, interval="month",
                      quantity=billing_seats.desired_seat_quantity(db, ORG_A))
    # Align renewal_date first so the only remaining dimension is quantity.
    s = sub_a(db)
    s.renewal_date = PERIOD_END
    db.commit()

    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)
    assert report["status"] == "in_sync"
    assert report["divergences"] == {}
    assert stub.modify_calls == []


def test_reconcile_survives_stripe_outage(db, seeded):
    stub = StubStripe(fail_retrieve=True)
    report = billing_reconcile.reconcile_subscription(db, ORG_A, stripe_client=stub)
    assert report["status"] == "stripe_unavailable"
    assert sub_a(db).subscription_status == "active"


def test_reconcile_all_sweeps_only_orgs_with_stripe_subscriptions(db, seeded):
    stub = StubStripe(status="active",
                      quantity=billing_seats.desired_seat_quantity(db, ORG_A))
    tally = billing_reconcile.reconcile_all(db, stripe_client=stub, apply=False)
    assert tally["checked"] == 2          # ORG_A and ORG_B both have Stripe ids


def test_reconcile_skips_org_without_stripe_subscription(db, seeded):
    s = sub_b(db)
    s.stripe_subscription_id = None
    s.stripe_customer_id = None
    db.commit()
    report = billing_reconcile.reconcile_subscription(db, ORG_B,
                                                      stripe_client=StubStripe())
    assert report["status"] == "no_stripe_subscription"


# ══════════════════════════════════════════════════════════════════════════════
# 16 — observability
# ══════════════════════════════════════════════════════════════════════════════
def test_ledger_records_every_required_observability_field(db, seeded):
    stub = StubStripe(fail_retrieve=True)
    ev = event("invoice.paid", {"subscription": SUB_A}, event_id="evt_obs")
    wh.process_event(db, ev, stripe_client=stub)

    row = ledger(db, "evt_obs")
    assert row.stripe_event_id == "evt_obs"          # event id
    assert row.event_type == "invoice.paid"          # event type
    assert row.attempts == 1                         # attempts
    assert row.status == wh.FAILED                   # processing status
    assert row.last_error                            # error
    assert row.processed_at is None                  # processed timestamp
    assert row.stripe_created_at is not None
    assert row.api_version == "2024-06-20"

    stub.fail_retrieve = False
    wh.process_event(db, ev, stripe_client=stub)
    row = ledger(db, "evt_obs")
    assert row.organization_id == ORG_A               # org id once known
    assert row.processed_at is not None


def test_no_stripe_secret_is_ever_stored_in_the_ledger(db, seeded):
    stub = StubStripe()
    obj = dict(session_obj())
    obj["client_secret"] = "cs_secret_abc"
    obj["setup_intent"] = "seti_secret"
    wh.process_event(db, event("checkout.session.completed", obj,
                               event_id="evt_nosecret"), stripe_client=stub)

    blob = str(ledger(db, "evt_nosecret").payload)
    for leak in ("secret", "sk_test", "sk_live", "whsec_"):
        assert leak not in blob


# ══════════════════════════════════════════════════════════════════════════════
# 17-19 — HTTP endpoint: signature verification and fail-closed behaviour
# ══════════════════════════════════════════════════════════════════════════════
def _signed(payload: bytes, secret: str, *, age_seconds: int = 0) -> str:
    """A genuine Stripe signature header, so real verification runs."""
    import hashlib
    import hmac
    import time
    ts = int(time.time()) - age_seconds
    sig = hmac.new(secret.encode(), b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


@pytest.fixture
def webhook_client(db, monkeypatch):
    """The real router, with a known webhook secret."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.billing import router as billing_router
    from database.database import get_db

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_phase3")
    app = FastAPI()
    app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), "/api/admin/billing/webhook"


def test_endpoint_rejects_an_invalid_signature(webhook_client, seeded):
    client, url = webhook_client
    r = client.post(url, content=b'{"id":"evt_sig","type":"invoice.paid"}',
                    headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400
    assert "signature" in r.json()["detail"].lower()


def test_endpoint_rejects_a_stale_signature(webhook_client, seeded):
    """Correctly signed but outside Stripe's 300s tolerance — a replay."""
    client, url = webhook_client
    body = b'{"id":"evt_stale_sig","object":"event","type":"invoice.paid","data":{"object":{}}}'
    r = client.post(url, content=body,
                    headers={"stripe-signature": _signed(body, "whsec_test_phase3",
                                                         age_seconds=3600)})
    assert r.status_code == 400
    assert "signature" in r.json()["detail"].lower()


def test_endpoint_rejects_a_missing_signature_header(webhook_client, seeded):
    client, url = webhook_client
    r = client.post(url, content=b'{"id":"evt_nosig","type":"invoice.paid"}')
    assert r.status_code == 400


def test_endpoint_fails_closed_when_the_secret_is_not_configured(db, monkeypatch,
                                                                 seeded):
    """A missing secret must refuse every event, and say so plainly.

    Previously the SDK raised AttributeError on a None secret, which the generic
    handler reported as a payload problem — so a misconfiguration was
    indistinguishable from an attack while every genuine event was dropped.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.billing import router as billing_router
    from database.database import get_db

    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    app = FastAPI()
    app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    body = b'{"id":"evt_nosecret_ep","object":"event","type":"invoice.paid","data":{"object":{}}}'
    r = client.post("/api/admin/billing/webhook", content=body,
                    headers={"stripe-signature": _signed(body, "whsec_anything")})

    assert r.status_code == 400
    assert "secret" in r.json()["detail"].lower()
    # Nothing was claimed or applied.
    assert db.query(BillingWebhookEvent).filter(
        BillingWebhookEvent.stripe_event_id == "evt_nosecret_ep").count() == 0


def test_endpoint_returns_200_for_a_duplicate_delivery(webhook_client, db, seeded,
                                                       monkeypatch):
    client, url = webhook_client
    stub = StubStripe(status="past_due")
    # Capture the real function first — patching the name we then call would
    # make the lambda invoke itself.
    real = wh.process_event
    monkeypatch.setattr(
        "app.billing.webhooks.process_event",
        lambda _db, ev, **kw: real(_db, ev, stripe_client=stub))

    body = (b'{"id":"evt_http_dup","object":"event",'
            b'"type":"customer.subscription.updated","created":1700000000,'
            b'"data":{"object":{"id":"' + SUB_A.encode() + b'"}}}')
    headers = {"stripe-signature": _signed(body, "whsec_test_phase3")}

    first = client.post(url, content=body, headers=headers)
    second = client.post(url, content=body, headers=headers)

    assert first.status_code == 200, first.text
    assert first.json()["result"]["status"] == "processed"
    assert second.status_code == 200
    assert second.json()["result"]["status"] == "duplicate"
    assert stub.retrieve_calls == [SUB_A]          # business logic ran once


def test_endpoint_returns_500_so_stripe_retries_a_transient_failure(webhook_client, db,
                                                                   seeded, monkeypatch):
    """A retryable failure must not be acknowledged, or Stripe never re-delivers."""
    client, url = webhook_client
    stub = StubStripe(fail_retrieve=True)
    # Capture the real function first — patching the name we then call would
    # make the lambda invoke itself.
    real = wh.process_event
    monkeypatch.setattr(
        "app.billing.webhooks.process_event",
        lambda _db, ev, **kw: real(_db, ev, stripe_client=stub))

    body = (b'{"id":"evt_http_fail","object":"event","type":"invoice.paid",'
            b'"created":1700000000,'
            b'"data":{"object":{"subscription":"' + SUB_A.encode() + b'"}}}')
    r = client.post(url, content=body,
                    headers={"stripe-signature": _signed(body, "whsec_test_phase3")})

    assert r.status_code == 500
    row = ledger(db, "evt_http_fail")
    assert row.status == wh.FAILED
    assert row.attempts == 1


def test_endpoint_returns_200_for_a_terminal_rejection(webhook_client, db, seeded,
                                                      monkeypatch):
    """An unpaid session will never become paid — asking Stripe to retry is noise."""
    client, url = webhook_client
    stub = StubStripe()
    # Capture the real function first — patching the name we then call would
    # make the lambda invoke itself.
    real = wh.process_event
    monkeypatch.setattr(
        "app.billing.webhooks.process_event",
        lambda _db, ev, **kw: real(_db, ev, stripe_client=stub))

    body = (b'{"id":"evt_http_unpaid","object":"event",'
            b'"type":"checkout.session.completed","created":1700000000,'
            b'"data":{"object":{"payment_status":"unpaid","mode":"subscription",'
            b'"client_reference_id":"' + str(ORG_A).encode() + b'"}}}')
    r = client.post(url, content=body,
                    headers={"stripe-signature": _signed(body, "whsec_test_phase3")})

    assert r.status_code == 200
    assert r.json()["result"]["status"] == "rejected"
    assert ledger(db, "evt_http_unpaid").status == wh.DEAD_LETTER
