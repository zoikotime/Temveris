"""Phase 3C - a plan that is not sold as a trial cannot self-provision one.

The catalog has carried ``trial_enabled`` since it was seeded (Verified and
Governed true, Sovereign and Enterprise false) but nothing read it. Combined
with the signup form posting ``expected_users=1`` — which meant the headcount
diversion never fired — ``/start?plan=sovereign`` produced a free 14-day
Sovereign workspace with unlimited seats, SSO, SCIM and legal hold, with no
payment and no sales contact.

These tests cover the routing rule, the catalog lookup behind it, and the real
signup endpoint, because the defect only appeared when all three lined up.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.billing import service as billing_service
from app.commercial import constants, routing


def _route(plan, intent="trial", *, trial_enabled=None, expected_users=1):
    return routing.route(
        intent=intent, plan=plan, expected_users=expected_users,
        email="founder@example.com", country="US",
        trial_enabled=trial_enabled,
    ).route


# ── the routing rule ─────────────────────────────────────────────────────────
def test_verified_trial_is_still_self_serve():
    assert _route("verified", trial_enabled=True) == constants.ROUTE_SELF_SERVE_TRIAL


def test_governed_trial_is_still_self_serve():
    assert _route("governed", trial_enabled=True) == constants.ROUTE_SELF_SERVE_TRIAL


def test_sovereign_trial_goes_to_the_assisted_lane():
    """The Guided Assurance Pilot lane, which already existed."""
    assert _route("sovereign", trial_enabled=False) == constants.ROUTE_ASSISTED


def test_sovereign_paid_signup_is_unchanged():
    """Only the *trial* is withdrawn; buying Sovereign self-serve still works."""
    assert _route("sovereign", intent="paid", trial_enabled=False) == \
        constants.ROUTE_SELF_SERVE_PAID


def test_enterprise_routing_is_unchanged():
    assert _route("enterprise", trial_enabled=False) == constants.ROUTE_ENTERPRISE
    assert _route("enterprise", trial_enabled=True) == constants.ROUTE_ENTERPRISE


def test_an_unknown_eligibility_changes_nothing():
    """`None` means the caller could not say - lead capture never provisions,
    so it must not be diverted by a rule it cannot answer."""
    assert _route("sovereign", trial_enabled=None) == constants.ROUTE_SELF_SERVE_TRIAL


def test_the_rule_only_applies_to_trial_intent():
    for intent in ("paid",):
        assert _route("sovereign", intent=intent, trial_enabled=False) != \
            constants.ROUTE_ASSISTED


def test_higher_priority_lanes_still_win():
    """The new rule must not shadow sanctions, enterprise or headcount."""
    assert _route("sovereign", trial_enabled=False, expected_users=500) == \
        constants.ROUTE_ENTERPRISE
    assert _route("enterprise", trial_enabled=False) == constants.ROUTE_ENTERPRISE


def test_routing_stays_a_pure_function():
    """No database access crept into the routing module."""
    import inspect
    src = inspect.getsource(routing)
    for token in ("Session", "db.query", "get_plan_by_code", "SEED_PLANS"):
        assert token not in src, f"routing.py should not reference {token!r}"


# ── the catalog lookup ───────────────────────────────────────────────────────
@pytest.mark.parametrize("code,expected", [
    ("verified", True), ("governed", True),
    ("sovereign", False), ("enterprise", False),
])
def test_catalog_reports_trial_eligibility(db, code, expected):
    assert billing_service.trial_enabled_for(db, code) is expected


def test_unknown_and_missing_plans_are_never_trial_eligible(db):
    assert billing_service.trial_enabled_for(db, "not_a_plan") is False
    assert billing_service.trial_enabled_for(db, None) is False
    assert billing_service.trial_enabled_for(db, "") is False


def test_lookup_survives_a_schema_without_the_catalog_table(db):
    """The commercial test schema does not build billing_plans. The helper must
    fall back to the seed rather than raising - and must not poison the
    caller's transaction, which is why it inspects instead of catching.
    """
    assert billing_service.trial_enabled_for(db, "sovereign") is False
    assert billing_service.trial_enabled_for(db, "verified") is True
    # the session is still usable afterwards
    from app.commercial.models import CommercialOrganization
    db.query(CommercialOrganization).first()


def test_every_seed_plan_declares_trial_eligibility():
    """A new plan added without the flag must not silently become trial-eligible."""
    for seed in billing_service.SEED_PLANS:
        assert "trial_enabled" in seed, f"{seed['code']} has no trial_enabled"


def test_the_seed_still_says_sovereign_is_not_a_trial_plan():
    """Pins the business rule this phase enforces."""
    by_code = {p["code"]: p for p in billing_service.SEED_PLANS}
    assert by_code["verified"]["trial_enabled"] is True
    assert by_code["governed"]["trial_enabled"] is True
    assert by_code["sovereign"]["trial_enabled"] is False
    assert by_code["enterprise"]["trial_enabled"] is False


# ── the signup endpoint actually supplies it ─────────────────────────────────
def test_signup_passes_catalog_eligibility_into_routing():
    """The defect was that nothing read the flag. Pin the wiring, not just the
    rule - a correct rule nobody calls is what produced this bug."""
    import inspect
    from app.commercial.routers import signup as signup_mod
    src = inspect.getsource(signup_mod)
    assert "trial_enabled=billing_service.trial_enabled_for(db, payload.plan)" in src, \
        "signup must supply real catalog eligibility to routing.route()"


def test_sovereign_remains_a_valid_plan_to_ask_for():
    """The fix withdraws the trial, not the plan: Sovereign must stay
    selectable so the paid and assisted lanes keep working."""
    assert constants.PLAN_SOVEREIGN in constants.SELF_SERVE_PLANS


def test_trial_length_is_still_fourteen_days():
    """Untouched by this phase. The specification says 30; the confirmed
    business rule is 14 and remains live."""
    from app.billing.trial import TRIAL_LENGTH_DAYS
    assert TRIAL_LENGTH_DAYS == 14


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end coverage
# ══════════════════════════════════════════════════════════════════════════════
# The signup-endpoint tests live in tests/test_self_service_tenant_provisioning.py
# (TestPlanValidation), which already owns the TestClient + rate-limiter harness
# for this route:
#
#   test_accepts_self_serve_trial_plans[verified|governed]
#   test_sovereign_trial_is_routed_to_sales_and_not_provisioned
#   test_sovereign_remains_purchasable_self_serve
#
# Kept there rather than duplicated here so there is one signup harness.
