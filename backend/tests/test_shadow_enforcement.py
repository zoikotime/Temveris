"""Phase 3.3 - shadow entitlement observation.

Records what enforcement WOULD decide and changes nothing. The single property
that matters most here is negative: no decision, and no failure inside the
observer, may alter what a request returns.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.commercial import constants, entitlements, ids, manifests
from app.commercial import enforcement
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

PRIVACY = sorted(manifests.GLOBALLY_DENIED)

#: The capabilities queued for enforcement, per the Phase 3.3 scope.
ENFORCEMENT_CANDIDATES = [
    "productivity.apps_history", "productivity.website_categories",
    "payroll.calculator", "reports.advanced", "activity.anomaly",
    "leave.approvals", "offboarding.governed", "time.attendance.policy",
    "time.playback.advanced", "workspace.settings.rbac_advanced",
    "evidence.screenshots.policy_controls", "evidence.screenshots.masking",
    "integrations.api", "integrations.webhooks",
    "evidence.legal_hold", "reports.compliance", "governance.chain_of_custody",
]


def _tenant(db, *, plan="verified", state="active", identity=True,
            subscription=True, billing_type="customer", name=None):
    op = models.Organization(name=name or f"Org {ids.org_id()[:8]}",
                             billing_type=billing_type)
    db.add(op)
    db.flush()
    org_id = None
    if identity:
        org_id = ids.org_id()
        db.add(CommercialOrganization(
            id=org_id, name=op.name, country="US", commercial_state=state,
            operational_org_id=op.id))
        db.flush()
    if subscription and org_id:
        db.add(CommercialSubscription(
            id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
            billing_cycle="monthly", seat_count=5, state=state, provider="stub"))
        db.flush()
    u = models.User(name="U", email=f"u{op.id}-{ids.org_id()[:6]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()
    return op, org_id, u


def _records(caplog):
    return [r for r in caplog.records if r.name == "zoiko.entitlement.shadow"
            and r.levelno >= logging.INFO]


# ══════════════════════════════════════════════════════════════════════════════
# It observes, and it never interferes
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("plan,capability", [
    ("governed", "reports.advanced"),          # ALLOW
    ("verified", "identity.sso"),              # DENY
    ("sovereign", "evidence.screenshots.capture"),  # CONFIGURATION_REQUIRED
])
def test_every_decision_returns_none_and_continues(db, plan, capability):
    _, _, u = _tenant(db, plan=plan)
    assert enforcement.shadow_capability(
        db, u, capability, route="test") is None


def test_read_only_also_continues(db):
    """A suspended tenant's historical read resolves READ_ONLY; shadow records
    it and still returns None."""
    _, org_id, u = _tenant(db, plan="sovereign", state="suspended")
    assert entitlements.compute(db, org_id)["capabilities"][
        "reports.standard"] == manifests.READ_ONLY
    assert enforcement.shadow_capability(
        db, u, "reports.standard", route="test") is None


def test_unknown_capability_records_deny_but_continues(db, caplog):
    _, _, u = _tenant(db, plan="sovereign")
    with caplog.at_level(logging.INFO, logger="zoiko.entitlement.shadow"):
        assert enforcement.shadow_capability(
            db, u, "not.a.capability", route="test") is None
    rec = _records(caplog)[-1].getMessage()
    assert '"decision": "deny"' in rec
    assert '"reason": "unknown_capability"' in rec


def test_missing_commercial_identity_records_deny_but_continues(db, caplog):
    _, _, u = _tenant(db, identity=False, subscription=False)
    with caplog.at_level(logging.INFO, logger="zoiko.entitlement.shadow"):
        assert enforcement.shadow_capability(
            db, u, "reports.standard", route="test") is None
    rec = _records(caplog)[-1].getMessage()
    assert '"decision": "deny"' in rec
    assert '"reason": "no_commercial_identity"' in rec


def test_the_observer_cannot_raise(db):
    """Shadow mode must never become an availability risk."""
    assert enforcement.shadow_capability(db, None, "reports.advanced",
                                         route="null-user") is None
    assert enforcement.shadow_capability(None, None, "reports.advanced",
                                         route="null-everything") is None


def test_a_broken_resolver_does_not_break_the_caller(db, monkeypatch):
    _, _, u = _tenant(db)

    def _boom(*_a, **_k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(enforcement, "resolve_commercial_org", _boom)
    assert enforcement.shadow_capability(db, u, "reports.advanced",
                                         route="test") is None


# ══════════════════════════════════════════════════════════════════════════════
# What it records
# ══════════════════════════════════════════════════════════════════════════════
def test_the_record_carries_the_rollout_fields(db, caplog):
    _, org_id, u = _tenant(db, plan="governed", state="active")
    with caplog.at_level(logging.INFO, logger="zoiko.entitlement.shadow"):
        enforcement.shadow_capability(db, u, "reports.advanced",
                                      route="GET /reports/advanced")
    msg = _records(caplog)[-1].getMessage()
    for field in ("organization_id", "plan", "lifecycle_state", "capability",
                  "decision", "would_block", "route", "at", "reason"):
        assert f'"{field}"' in msg, field
    assert org_id in msg
    assert "GET /reports/advanced" in msg


def test_the_record_carries_no_credentials_or_personal_data(db, caplog):
    _, _, u = _tenant(db, plan="governed")
    with caplog.at_level(logging.INFO, logger="zoiko.entitlement.shadow"):
        enforcement.shadow_capability(db, u, "reports.advanced", route="r")
    msg = _records(caplog)[-1].getMessage()
    for leak in ("password", "token", "secret", "sk_", "email", u.email):
        assert leak not in msg, leak


def test_would_block_distinguishes_read_from_write(db, caplog):
    """READ_ONLY passes a read and fails a write - the record must say which."""
    _, _, u = _tenant(db, plan="sovereign", state="suspended")
    with caplog.at_level(logging.INFO, logger="zoiko.entitlement.shadow"):
        enforcement.shadow_capability(db, u, "reports.standard", route="r",
                                      writable=False)
        enforcement.shadow_capability(db, u, "reports.standard", route="w",
                                      writable=True)
    read, write = _records(caplog)[-2:]
    assert '"would_block": false' in read.getMessage()
    assert '"would_block": true' in write.getMessage()


# ══════════════════════════════════════════════════════════════════════════════
# Lifecycle decisions surface correctly
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state,expected", [
    ("trial_active", manifests.ALLOW),
    ("active", manifests.ALLOW),
    ("past_due", manifests.ALLOW),               # Phase 3 rule: not a downgrade
    ("cancelled_pending_end", manifests.ALLOW),  # retained until effective_at
    ("suspended", manifests.DENY),               # premium write
    ("cancelled", manifests.DENY),
    ("closed", manifests.DENY),
])
def test_lifecycle_decision_for_a_premium_write(db, state, expected):
    _, org_id, _ = _tenant(db, plan="sovereign", state=state)
    assert entitlements.compute(db, org_id)["capabilities"][
        "payroll.calculator"] == expected


@pytest.mark.parametrize("state", ["suspended", "cancelled"])
def test_lifecycle_decision_for_a_historical_read(db, state):
    _, org_id, _ = _tenant(db, plan="sovereign", state=state)
    assert entitlements.compute(db, org_id)["capabilities"][
        "reports.standard"] == manifests.READ_ONLY


def test_past_due_still_keeps_api_access(db):
    """The Phase 3 business rule, observed through shadow."""
    _, org_id, _ = _tenant(db, plan="governed", state="past_due")
    assert entitlements.compute(db, org_id)["capabilities"][
        "integrations.api"] == manifests.ALLOW


# ══════════════════════════════════════════════════════════════════════════════
# Global DENY holds everywhere shadow can see
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("key", PRIVACY)
@pytest.mark.parametrize("state", ["trial_active", "active", "past_due",
                                   "suspended", "cancelled", "closed"])
def test_privacy_denied_in_shadow_for_every_state(db, key, state):
    _, org_id, _ = _tenant(db, plan="sovereign", state=state)
    assert entitlements.compute(db, org_id)["capabilities"][key] == manifests.DENY


@pytest.mark.parametrize("key", PRIVACY)
def test_privacy_denied_in_shadow_for_the_internal_tenant(db, key, caplog):
    op = models.Organization(name="Vendor shadow", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    u = models.User(name="V", email=f"v{op.id}@x.test", organization_id=op.id)
    db.add(u)
    db.flush()

    with caplog.at_level(logging.INFO, logger="zoiko.entitlement.shadow"):
        enforcement.shadow_capability(db, u, key, route="internal")
    msg = _records(caplog)[-1].getMessage()
    assert '"decision": "deny"' in msg
    assert '"plan": "internal"' in msg


# ══════════════════════════════════════════════════════════════════════════════
# The per-capability switch and the population report
# ══════════════════════════════════════════════════════════════════════════════
def test_shadow_capabilities_is_per_capability(db, monkeypatch):
    """Enabling enforcement is a per-key decision, never a flag day."""
    from fastapi import HTTPException
    _, _, u = _tenant(db, plan="verified")

    with pytest.raises(HTTPException):
        enforcement._assert_capability(db, u, "governance.ediscovery", writable=False)

    monkeypatch.setattr(enforcement, "SHADOW_CAPABILITIES", {"governance.ediscovery"})
    enforcement._assert_capability(db, u, "governance.ediscovery", writable=False)

    # A different capability is unaffected by that one key being shadowed.
    with pytest.raises(HTTPException):
        enforcement._assert_capability(db, u, "evidence.legal_hold", writable=False)


def test_shadow_report_answers_the_rollout_questions(db):
    _tenant(db, plan="verified", state="active")
    _tenant(db, plan="sovereign", state="active")
    _tenant(db, identity=False, subscription=False)

    report = enforcement.shadow_report(db, capabilities=ENFORCEMENT_CANDIDATES)
    assert report["organizations"] >= 2
    assert set(report["capabilities"]) == set(ENFORCEMENT_CANDIDATES)
    # both privacy keys block for every organization evaluated
    for key in PRIVACY:
        assert report["would_block"][key] == report["organizations"]
    assert isinstance(report["by_plan"], dict)
    assert isinstance(report["by_state"], dict)


def test_shadow_report_is_read_only_apart_from_the_existing_cache(db):
    """It must not create organizations, subscriptions or any other row."""
    _tenant(db, plan="verified")
    before_orgs = db.query(CommercialOrganization).count()
    before_subs = db.query(CommercialSubscription).count()

    enforcement.shadow_report(db, capabilities=ENFORCEMENT_CANDIDATES)

    assert db.query(CommercialOrganization).count() == before_orgs
    assert db.query(CommercialSubscription).count() == before_subs


def test_repeated_shadow_calls_do_not_grow_state(db):
    """Requirement 17: the entitlement cache is one row per org, not per call."""
    from app.commercial.models import EntitlementsCache
    _, org_id, u = _tenant(db, plan="governed")
    for _ in range(10):
        enforcement.shadow_capability(db, u, "reports.advanced", route="loop")
    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# Nothing else changed
# ══════════════════════════════════════════════════════════════════════════════
def test_rbac_is_untouched_by_shadow(db):
    _, _, admin = _tenant(db, plan="governed")
    worker = models.User(name="W", email=f"w{admin.organization_id}@x.test",
                         organization_id=admin.organization_id, role="employee")
    db.add(worker)
    db.flush()
    assert (enforcement.capability_decision(db, admin, "reports.advanced")
            == enforcement.capability_decision(db, worker, "reports.advanced"))


def test_routes_stay_non_blocking_while_off():
    """Shadow hooks are allowed, and so is the rollout-gated require_capability
    that Phase 4.2 wired to the approved admin capabilities — because it is
    non-blocking while enforcement is OFF. What is NOT allowed is a route that
    could block in the current configuration. Pinned two ways: only approved
    capabilities are wired, and enforcement_mode() is OFF."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    pat = re.compile(r'require(?:_writable)?_capability\("([a-z0-9_.]+)"\)')
    wired = set()
    for p in root.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            wired.add(m.group(1))
    assert wired == {"activity.anomaly", "integrations.api",
                     "payroll.calculator", "holidays.multicountry"}, wired
    assert enforcement.enforcement_mode() == enforcement.MODE_OFF


def test_pilot_hooks_use_the_non_blocking_helper_only():
    """Every route-level shadow call must be the observer, never the gate."""
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    hooks = []
    for p in root.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "_shadow(" in line and "import" not in line:
                hooks.append((str(p.relative_to(root)), n, line.strip()))
    assert hooks, "expected at least one pilot hook"
    for path, n, line in hooks:
        assert "shadow_capability" not in line or "_shadow(" in line
        # a hook must not be used in a conditional or assignment
        assert not re.match(r"\s*(if|while|return|assert|[\w\.]+\s*=)\s", line), \
            f"{path}:{n} uses the shadow result: {line}"


def test_no_stripe_call_is_introduced():
    import inspect
    src = inspect.getsource(enforcement)
    for token in ("stripe.", "Stripe(", "checkout.Session", "Customer.create"):
        assert token not in src, token


def test_business_rules_untouched():
    from app.billing.trial import TRIAL_LENGTH_DAYS
    from app.commercial.entitlements import _PLAN_LIMITS
    assert TRIAL_LENGTH_DAYS == 14
    assert _PLAN_LIMITS["governed"]["seat_limit"] == 100
    assert manifests.PLANS == ("verified", "governed", "sovereign", "enterprise")
