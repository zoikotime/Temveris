"""Commercial Routing Engine tests (§7)."""

from app.commercial import routing, constants
from app.commercial.fraud import FraudDecision, ACTION_BLOCK, ACTION_REVIEW


def _call(**kw):
    base = dict(intent="trial", plan="verified", expected_users=10,
                email="alex@acme.com", country="US")
    base.update(kw)
    return routing.route(**base)


def test_small_team_self_serve():
    d = _call(expected_users=20)
    assert d.route == constants.ROUTE_SELF_SERVE_TRIAL


def test_assisted_band():
    d = _call(expected_users=120)
    assert d.route == constants.ROUTE_ASSISTED


def test_enterprise_band():
    d = _call(expected_users=400)
    assert d.route == constants.ROUTE_ENTERPRISE


def test_enterprise_intent_overrides_size():
    d = _call(intent="enterprise", expected_users=5)
    assert d.route == constants.ROUTE_ENTERPRISE


def test_government_email_goes_to_review():
    d = _call(email="alex@example.gov")
    assert d.route == constants.ROUTE_MANUAL_REVIEW


def test_sanctioned_country_blocked():
    d = _call(country="IR")
    assert d.route == constants.ROUTE_BLOCKED


def test_sso_request_pushes_assisted_below_governed():
    d = _call(plan="verified", sso_or_scim_requested=True)
    assert d.route == constants.ROUTE_ASSISTED


def test_data_residency_forces_enterprise():
    d = _call(custom_data_residency=True)
    assert d.route == constants.ROUTE_ENTERPRISE


def test_fraud_block_short_circuits():
    fr = FraudDecision(action=ACTION_BLOCK, score=100.0, reason="disposable email")
    d = _call(fraud=fr)
    assert d.route == constants.ROUTE_BLOCKED


def test_fraud_review_routes_to_manual():
    fr = FraudDecision(action=ACTION_REVIEW, score=80.0, reason="velocity")
    d = _call(fraud=fr)
    assert d.route == constants.ROUTE_MANUAL_REVIEW


def test_financial_keyword_pushes_assisted():
    d = _call(email="alex@acmebank.com", company_name="Acme Bank")
    assert d.route == constants.ROUTE_ASSISTED
