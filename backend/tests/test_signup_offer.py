"""GET /api/v1/billing/quote/preview — the public trial offer.

This is the plan-summary panel on the signup page's Account step. The panel is
the only place a visitor reads a price before they have an account, so what it
serves has to be the same pricing and the same trial clock the paid path uses.
These tests pin that, plus the properties that make an unauthenticated pricing
endpoint safe to expose:

  * it needs no session,
  * it writes nothing (no PriceQuote row per page view),
  * it cannot be steered onto a sales-led tier,
  * and it degrades rather than 500s when the plan catalog is missing.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database import models
from app.billing import trial as billing_trial
from app.commercial import constants
from app.commercial.models import PriceQuote
import app.commercial.routers.checkout as checkout_mod


@pytest.fixture
def client(engine):
    # The catalog table is not part of conftest's schema (nothing else in the
    # suite reads it), so create it here and seed the one plan under test.
    models.BillingPlan.__table__.create(engine, checkfirst=True)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    s = TestingSessionLocal()
    if s.query(models.BillingPlan).filter_by(code="verified").first() is None:
        s.add(models.BillingPlan(code="verified", name="Verified",
                                 monthly_price_per_user=8.0, tier_order=1))
        s.commit()
    s.close()

    app = FastAPI()
    app.state.limiter = checkout_mod.limiter
    app.include_router(checkout_mod.router)

    def _get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=True)


# ── the offer itself ─────────────────────────────────────────────────────────
def test_offer_is_public_and_prices_from_the_canonical_constants(client):
    r = client.get("/api/v1/billing/quote/preview")
    assert r.status_code == 200, r.text
    body = r.json()

    # $8/worker/mo list, 20% off annually → $6.40 effective. Both come from
    # quotes.compute_amounts, the same function checkout charges from.
    assert body["unit_price_monthly_cents"] == 800
    assert body["effective_unit_price_monthly_cents"] == 640
    assert body["annual_discount_pct"] == constants.ANNUAL_DISCOUNT_PCT
    assert body["currency"] == "USD"
    assert body["billing_cycle"] == "annual"


def test_offer_reports_the_authoritative_trial_length(client):
    # app/billing owns the customer-visible trial clock (14 days).
    # app/commercial's TRIAL_LENGTH_DAYS = 30 is deliberately NOT used here —
    # a summary saying 30 days over a subscription that expires at 14 is the
    # exact drift app/billing/trial.py was written to end.
    body = client.get("/api/v1/billing/quote/preview").json()
    assert body["trial_length_days"] == billing_trial.TRIAL_LENGTH_DAYS == 14
    assert body["trial_length_days"] != constants.TRIAL_LENGTH_DAYS


def test_trial_end_is_computed_forward_from_now(client):
    from datetime import datetime, timezone

    body = client.get("/api/v1/billing/quote/preview").json()
    ends = datetime.fromisoformat(body["trial_ends_at"])
    assert ends.tzinfo is not None                      # never a naive local date
    delta_days = (ends - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 13.9 < delta_days <= 14.0


def test_nothing_is_due_today_and_no_card_is_required(client):
    body = client.get("/api/v1/billing/quote/preview").json()
    assert body["amount_due_today_cents"] == 0
    assert body["card_required"] is False
    assert body["card_required"] == constants.CREDIT_CARD_REQUIRED_FOR_TRIAL


def test_plan_label_comes_from_the_billing_catalog(client):
    body = client.get("/api/v1/billing/quote/preview").json()
    assert body["plan_label"] == "Temveris Verified"


def test_legal_versions_match_the_versions_consent_records(client):
    # The page stamps its consent checkbox with whatever it is served here; if
    # these drifted the recorded agreement would name a version nobody saw.
    body = client.get("/api/v1/billing/quote/preview").json()
    assert body["terms_version"] == constants.TERMS_VERSION_CURRENT
    assert body["privacy_version"] == constants.PRIVACY_VERSION_CURRENT


# ── the properties that make it safe to expose ───────────────────────────────
def test_preview_persists_no_quote_row(client, db):
    before = db.query(PriceQuote).count()
    for _ in range(3):
        assert client.get("/api/v1/billing/quote/preview").status_code == 200
    # An anonymous page view must not be able to grow the quote table.
    assert db.query(PriceQuote).count() == before


def test_enterprise_is_not_quotable_from_a_public_request(client):
    # Sales-led: it is absent from SELF_SERVE_PLANS and must never be priced
    # by an unauthenticated caller.
    r = client.get("/api/v1/billing/quote/preview", params={"plan": "enterprise"})
    assert r.status_code == 400


def test_unknown_plan_is_rejected_not_coerced(client):
    assert client.get("/api/v1/billing/quote/preview",
                      params={"plan": "unlimited-everything"}).status_code == 400


def test_monthly_cycle_has_no_annual_discount(client):
    body = client.get("/api/v1/billing/quote/preview",
                      params={"billing_cycle": "monthly"}).json()
    assert body["effective_unit_price_monthly_cents"] == 800
    assert body["unit_price_monthly_cents"] == 800


def test_bad_billing_cycle_is_rejected(client):
    assert client.get("/api/v1/billing/quote/preview",
                      params={"billing_cycle": "fortnightly"}).status_code == 400


def test_missing_plan_catalog_degrades_instead_of_500ing(client, engine, monkeypatch):
    # The signup page is the first thing a visitor loads. An unseeded catalog
    # must cost the display name, not the page.
    from app.billing import service as billing_service

    def _boom(*_a, **_k):
        raise RuntimeError("billing_plans is not there")

    monkeypatch.setattr(billing_service, "get_plan_by_code", _boom)
    r = client.get("/api/v1/billing/quote/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_label"] == "Temveris Verified"     # falls back to title case
    assert body["unit_price_monthly_cents"] == 800        # pricing is unaffected


def test_the_authenticated_quote_endpoint_still_requires_a_session(client):
    # Regression guard on the route ordering: adding /quote/preview must not
    # have made /quote/{quote_id} public.
    r = client.get("/api/v1/billing/quote/qot_deadbeef")
    assert r.status_code in (401, 403), r.text
