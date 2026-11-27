"""Blockers A and B — the two paths that could charge a real customer wrongly.

BLOCKER A — invoice.paid arriving before checkout.session.completed
Stripe does not guarantee delivery order. The subscription lookup raised
``WebhookRejected``, whose contract is "retrying cannot help", so the event was
dead-lettered on attempt 1 and Stripe was answered 200 — told not to re-deliver. Both
real payments this system has processed hit it: a 100% dead-letter rate on invoice.paid,
silently skipping apply_subscription_state, _sync_dunning and _mirror_invoices.

BLOCKER B — a second Checkout Session while stripe_subscription_id is NULL
Two paid sessions produce two Stripe subscriptions. The webhook keeps the first ("fill
in, never replace") and the second bills the card forever, unreachable from the product
because the hosted portal opens for the *stored* customer. Four such subscriptions
($1,450/month) accumulated in the sandbox exactly this way.

Nothing here contacts Stripe: every client is a double. No Stripe object is created,
modified or cancelled, and no mail is sent.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.billing import router as billing_router
from app.billing import webhooks as billing_webhooks
from app.billing import worker_state
from database.database import get_db
from database.models import (
    BillingPlan, BillingSubscription, BillingWebhookEvent, Organization, User,
)

API = "/api/admin/billing"
ORG = 9801


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def seeded(engine, db):
    from database.models import Base
    Base.metadata.create_all(engine, checkfirst=True)

    def _clean():
        db.query(BillingWebhookEvent).delete()
        db.query(BillingSubscription).delete()
        db.query(BillingPlan).delete()
        db.query(User).filter(User.organization_id == ORG).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id == ORG).delete(synchronize_session=False)
        db.commit()

    _clean()
    db.add(Organization(id=ORG, name="Payer Co", billing_type="customer"))
    gov = BillingPlan(code="governed", name="Governed", tier_order=2,
                      monthly_price_per_user=15.0, annual_price_per_user=144.0,
                      user_limit=250, active_status=True)
    ver = BillingPlan(code="verified", name="Verified", tier_order=1,
                      monthly_price_per_user=8.0, annual_price_per_user=76.80,
                      user_limit=25, active_status=True)
    db.add_all([gov, ver])
    db.flush()
    db.add(BillingSubscription(organization_id=ORG, plan_id=ver.id,
                               billing_cycle="monthly", subscription_status="trial",
                               active_users=1, currency="USD"))
    db.add(User(name="Admin", email="admin@payer.test", organization_id=ORG,
                role="admin", is_active=True,
                billing_state=worker_state.ACTIVE_BILLABLE))
    db.commit()
    yield {"governed": gov, "verified": ver}
    _clean()


def _sub(db):
    return db.query(BillingSubscription).filter_by(organization_id=ORG).first()


# ══════════════════════════════════════════════════════════════════════════════
# BLOCKER A — the ordering race is retryable, not terminal
# ══════════════════════════════════════════════════════════════════════════════
def test_not_ready_is_a_distinct_class_from_rejected():
    """They mean opposite things, so one must not be a subclass of the other."""
    assert not issubclass(billing_webhooks.WebhookNotReady,
                          billing_webhooks.WebhookRejected)
    assert not issubclass(billing_webhooks.WebhookRejected,
                          billing_webhooks.WebhookNotReady)


def test_unknown_subscription_raises_not_ready_not_rejected(db, seeded):
    with pytest.raises(billing_webhooks.WebhookNotReady) as exc:
        billing_webhooks._sub_by_stripe_id(db, "sub_not_linked_yet")
    assert "no_local_subscription_for:sub_not_linked_yet" in str(exc.value)


def test_invoice_paid_before_checkout_is_retryable_and_not_dead_lettered(db, seeded):
    """The exact production failure: invoice.paid first, subscription not linked yet."""
    event = {
        "id": "evt_race_1", "object": "event", "type": "invoice.paid",
        "created": 1_760_000_000,
        "data": {"object": {"id": "in_1", "subscription": "sub_pending"}},
    }
    result = billing_webhooks.process_event(db, event)

    assert result["status"] == "failed", result       # -> router answers HTTP 500
    assert result.get("retryable") is True
    row = db.query(BillingWebhookEvent).filter_by(stripe_event_id="evt_race_1").first()
    assert row.status == billing_webhooks.FAILED      # NOT dead_letter
    assert row.attempts == 1


def test_a_retried_race_event_succeeds_once_the_subscription_is_linked(db, seeded):
    """Attempt 1 fails; by attempt 2 checkout.session.completed has landed."""
    event = {
        "id": "evt_race_2", "object": "event", "type": "invoice.paid",
        "created": 1_760_000_000,
        "data": {"object": {"id": "in_2", "subscription": "sub_linked_later"}},
    }
    assert billing_webhooks.process_event(db, event)["status"] == "failed"

    # checkout.session.completed arrives and links the id.
    sub = _sub(db)
    sub.stripe_subscription_id = "sub_linked_later"
    sub.stripe_customer_id = "cus_x"
    db.commit()

    class FakeStripe:
        class Subscription:
            @staticmethod
            def retrieve(sid, **kw):
                return {"id": sid, "status": "active", "customer": "cus_x",
                        "items": {"data": [{"quantity": 1,
                                            "price": {"id": "price_x",
                                                      "unit_amount": 1500,
                                                      "currency": "usd",
                                                      "recurring": {"interval": "month"}}}]}}
        class Invoice:
            @staticmethod
            def list(**kw):
                return {"data": []}

    result = billing_webhooks.process_event(db, event, stripe_client=FakeStripe)
    assert result["status"] in ("processed", "ignored"), result
    row = db.query(BillingWebhookEvent).filter_by(stripe_event_id="evt_race_2").first()
    assert row.status == billing_webhooks.PROCESSED


def test_the_race_still_dead_letters_after_max_attempts(db, seeded):
    """A subscription that never appears must stop being retried eventually."""
    event = {
        "id": "evt_race_3", "object": "event", "type": "invoice.paid",
        "created": 1_760_000_000,
        "data": {"object": {"id": "in_3", "subscription": "sub_never"}},
    }
    for _ in range(billing_webhooks.MAX_ATTEMPTS + 2):
        billing_webhooks.process_event(db, event)
    row = db.query(BillingWebhookEvent).filter_by(stripe_event_id="evt_race_3").first()
    assert row.status == billing_webhooks.DEAD_LETTER
    assert row.attempts >= billing_webhooks.MAX_ATTEMPTS


@pytest.mark.parametrize("reason,payload", [
    ("unpaid_checkout_session", {"id": "cs_1", "payment_status": "unpaid",
                                 "mode": "subscription"}),
    ("unresolvable_tenant", {"id": "cs_2", "payment_status": "paid",
                             "mode": "subscription", "client_reference_id": "nope"}),
])
def test_genuinely_terminal_rejections_stay_terminal(db, seeded, reason, payload):
    """Only the ordering race changed. An unpaid session or a bad tenant must still be
    dead-lettered and answered 200 — re-delivery cannot fix either."""
    event = {"id": f"evt_term_{reason}", "object": "event",
             "type": "checkout.session.completed", "created": 1_760_000_000,
             "data": {"object": payload}}
    result = billing_webhooks.process_event(db, event)
    assert result["status"] == "rejected", result
    row = db.query(BillingWebhookEvent).filter_by(
        stripe_event_id=f"evt_term_{reason}").first()
    assert row.status == billing_webhooks.DEAD_LETTER


# ══════════════════════════════════════════════════════════════════════════════
# BLOCKER B — checkout duplicate protection
# ══════════════════════════════════════════════════════════════════════════════
def test_same_intent_produces_the_same_idempotency_key():
    k = billing_router._checkout_idempotency_key
    assert k(ORG, "governed", "annual", 3) == k(ORG, "governed", "annual", 3)


@pytest.mark.parametrize("a,b", [
    ((ORG, "governed", "annual", 1), (ORG, "verified", "annual", 1)),   # plan
    ((ORG, "governed", "annual", 1), (ORG, "governed", "monthly", 1)),  # cycle
    ((ORG, "governed", "annual", 1), (ORG, "governed", "annual", 2)),   # seats
    ((ORG, "governed", "annual", 1), (ORG + 1, "governed", "annual", 1)),  # org
])
def test_a_different_intent_produces_a_different_key(a, b):
    k = billing_router._checkout_idempotency_key
    assert k(*a) != k(*b)


def test_the_key_does_not_leak_the_tenant_id_in_the_clear():
    key = billing_router._checkout_idempotency_key(ORG, "governed", "annual", 1)
    assert str(ORG) not in key
    assert "governed" not in key
    assert len(key) == 64


class _FakeSessions:
    """Mimics Stripe's idempotency contract: a repeated key replays the FIRST result.

    Getting this right in the double matters — a double that returns a fresh object per
    call would let the test pass while the real API deduplicated, or vice versa.
    """

    def __init__(self):
        self.created = []          # every create() call, including replays
        self._by_key = {}          # idempotency_key -> the session first returned

    def create(self, **kwargs):
        self.created.append(kwargs)
        key = kwargs.get("idempotency_key")
        if key in self._by_key:
            return self._by_key[key]
        n = len(self._by_key) + 1
        session = type("S", (), {"id": f"cs_{n}", "url": f"https://stripe.test/cs_{n}"})
        if key:
            self._by_key[key] = session
        return session


def _client(db, actor, fake_stripe, monkeypatch):
    monkeypatch.setattr(billing_router.stripe, "checkout", fake_stripe.checkout,
                        raising=False)
    monkeypatch.setattr(billing_router.stripe, "Subscription", fake_stripe.Subscription,
                        raising=False)
    monkeypatch.setenv("STRIPE_PRICE_GOVERNED_ANNUAL", "price_gov_annual")
    monkeypatch.setenv("STRIPE_PRICE_GOVERNED_MONTHLY", "price_gov_monthly")
    app = FastAPI()
    app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    from app.auth.service import get_current_user
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


class _FakeStripe:
    def __init__(self, existing_active=None, list_raises=False):
        sessions = _FakeSessions()
        self.checkout = type("C", (), {"Session": sessions})
        self.sessions = sessions
        outer = self

        class Subscription:
            @staticmethod
            def list(**kw):
                if outer.list_raises:
                    raise RuntimeError("stripe unavailable")
                return {"data": outer.existing_active or []}
        self.Subscription = Subscription
        self.existing_active = existing_active
        self.list_raises = list_raises


def test_two_rapid_upgrade_calls_reuse_one_checkout_session(db, seeded, monkeypatch):
    """A double click must not be able to open two payable sessions."""
    actor = db.query(User).filter_by(organization_id=ORG).first()
    fake = _FakeStripe()
    client = _client(db, actor, fake, monkeypatch)
    body = {"plan_code": "governed", "billing_cycle": "annual"}

    r1 = client.post(f"{API}/upgrade", json=body)
    r2 = client.post(f"{API}/upgrade", json=body)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)

    keys = [c["idempotency_key"] for c in fake.sessions.created]
    assert len(set(keys)) == 1, "both calls must carry the same idempotency key"
    assert r1.json()["url"] == r2.json()["url"]


def test_every_checkout_session_carries_an_idempotency_key(db, seeded, monkeypatch):
    actor = db.query(User).filter_by(organization_id=ORG).first()
    fake = _FakeStripe()
    client = _client(db, actor, fake, monkeypatch)
    client.post(f"{API}/upgrade", json={"plan_code": "governed",
                                        "billing_cycle": "annual"})
    assert fake.sessions.created, "no session was created"
    for call in fake.sessions.created:
        assert call.get("idempotency_key"), call


def test_an_existing_active_stripe_subscription_blocks_a_second_checkout(db, seeded,
                                                                        monkeypatch):
    """The window that produced the orphans: customer id stored, subscription id not.

    Without this the second click opened a second payable session.
    """
    sub = _sub(db)
    sub.stripe_customer_id = "cus_known"
    sub.stripe_subscription_id = None          # webhook has not landed yet
    db.commit()

    actor = db.query(User).filter_by(organization_id=ORG).first()
    fake = _FakeStripe(existing_active=[{"id": "sub_already_paying"}])
    client = _client(db, actor, fake, monkeypatch)

    r = client.post(f"{API}/upgrade", json={"plan_code": "governed",
                                            "billing_cycle": "annual"})
    assert r.status_code == 409, r.text
    assert "already has an active subscription" in r.json()["detail"]
    assert fake.sessions.created == [], "no session may be created in this state"


def test_a_stripe_read_failure_does_not_block_a_legitimate_first_purchase(db, seeded,
                                                                         monkeypatch):
    """Belt, not braces: if the pre-check cannot run, the idempotency key still guards."""
    sub = _sub(db)
    sub.stripe_customer_id = "cus_known"
    db.commit()
    actor = db.query(User).filter_by(organization_id=ORG).first()
    fake = _FakeStripe(list_raises=True)
    client = _client(db, actor, fake, monkeypatch)

    r = client.post(f"{API}/upgrade", json={"plan_code": "governed",
                                            "billing_cycle": "annual"})
    assert r.status_code == 200, r.text
    assert fake.sessions.created[0].get("idempotency_key")


def test_a_first_purchase_with_no_customer_yet_still_works(db, seeded, monkeypatch):
    """No customer id means nothing to pre-check; the key is the whole protection."""
    actor = db.query(User).filter_by(organization_id=ORG).first()
    fake = _FakeStripe()
    client = _client(db, actor, fake, monkeypatch)
    r = client.post(f"{API}/upgrade", json={"plan_code": "governed",
                                            "billing_cycle": "annual"})
    assert r.status_code == 200, r.text
    assert "customer" not in fake.sessions.created[0]


def test_choosing_a_different_plan_still_opens_a_new_session(db, seeded, monkeypatch):
    """Duplicate protection must not become a cage — a real plan change must work."""
    actor = db.query(User).filter_by(organization_id=ORG).first()
    fake = _FakeStripe()
    client = _client(db, actor, fake, monkeypatch)
    monkeypatch.setenv("STRIPE_PRICE_VERIFIED_ANNUAL", "price_ver_annual")

    client.post(f"{API}/upgrade", json={"plan_code": "governed",
                                        "billing_cycle": "annual"})
    client.post(f"{API}/upgrade", json={"plan_code": "governed",
                                        "billing_cycle": "monthly"})
    keys = [c["idempotency_key"] for c in fake.sessions.created]
    assert len(set(keys)) == 2, "a different cycle is a different intent"


# ══════════════════════════════════════════════════════════════════════════════
# BLOCKER I — the renewal date must come from Stripe, not from arithmetic
# ══════════════════════════════════════════════════════════════════════════════
def test_confirm_uses_stripes_current_period_end_not_now_plus_30(db, seeded, monkeypatch):
    """`now + 30` is not a month. February drifted two days, most months by one.

    The billing page showed one renewal date while Stripe charged on another.
    """
    import datetime as _dt
    sub = _sub(db)
    sub.stripe_subscription_id = "sub_live_1"
    sub.stripe_customer_id = "cus_live_1"
    db.commit()

    stripe_period_end = _dt.datetime(2026, 9, 30, 12, 0, tzinfo=_dt.timezone.utc)

    class FakeStripe:
        class checkout:
            class Session:
                @staticmethod
                def retrieve(sid, **kw):
                    return type("S", (), {
                        "payment_status": "paid", "status": "complete",
                        "client_reference_id": str(ORG),
                        "subscription": "sub_live_1", "customer": "cus_live_1",
                        "metadata": type("M", (), {"plan_code": "governed",
                                                   "billing_cycle": "monthly"}),
                    })
        class Subscription:
            @staticmethod
            def retrieve(sid, **kw):
                return {"id": sid, "status": "active",
                        "current_period_end": int(stripe_period_end.timestamp())}

    monkeypatch.setattr(billing_router.stripe, "checkout", FakeStripe.checkout,
                        raising=False)
    monkeypatch.setattr(billing_router.stripe, "Subscription", FakeStripe.Subscription,
                        raising=False)

    actor = db.query(User).filter_by(organization_id=ORG).first()
    app = FastAPI(); app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    from app.auth.service import get_current_user
    app.dependency_overrides[get_current_user] = lambda: actor
    client = TestClient(app)

    r = client.get(f"{API}/upgrade/confirm", params={"session_id": "cs_x"})
    assert r.status_code == 200, r.text

    stored = _sub(db).renewal_date
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=_dt.timezone.utc)
    assert stored == stripe_period_end, (
        "renewal_date must be Stripe's current_period_end, not a local estimate")


def test_confirm_falls_back_to_a_provisional_date_when_stripe_is_unreachable(
        db, seeded, monkeypatch):
    """A rough date beats none — but only as an explicit fallback."""
    import datetime as _dt
    sub = _sub(db)
    sub.stripe_subscription_id = "sub_live_2"
    sub.stripe_customer_id = "cus_live_2"
    db.commit()

    class FakeStripe:
        class checkout:
            class Session:
                @staticmethod
                def retrieve(sid, **kw):
                    return type("S", (), {
                        "payment_status": "paid", "status": "complete",
                        "client_reference_id": str(ORG),
                        "subscription": "sub_live_2", "customer": "cus_live_2",
                        "metadata": type("M", (), {"plan_code": "governed",
                                                   "billing_cycle": "annual"}),
                    })
        class Subscription:
            @staticmethod
            def retrieve(sid, **kw):
                raise RuntimeError("stripe unavailable")

    monkeypatch.setattr(billing_router.stripe, "checkout", FakeStripe.checkout,
                        raising=False)
    monkeypatch.setattr(billing_router.stripe, "Subscription", FakeStripe.Subscription,
                        raising=False)

    actor = db.query(User).filter_by(organization_id=ORG).first()
    app = FastAPI(); app.include_router(billing_router.router)
    app.dependency_overrides[get_db] = lambda: db
    from app.auth.service import get_current_user
    app.dependency_overrides[get_current_user] = lambda: actor
    client = TestClient(app)

    r = client.get(f"{API}/upgrade/confirm", params={"session_id": "cs_y"})
    assert r.status_code == 200, r.text
    stored = _sub(db).renewal_date
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=_dt.timezone.utc)
    # Roughly a year out, and the webhook will correct it.
    delta = (stored - _dt.datetime.now(_dt.timezone.utc)).days
    assert 360 <= delta <= 366, delta
