"""Phase 3.21 — enforcement rollout control + shadow readiness.

The rollout gate is a SAFE control surface: OFF by default, layered opt-in
(master env → per-org flag → per-capability scope) and an immediate kill-switch.
Since Phase 4.2 the rollout-gated gate is wired to the approved safe-first admin
capabilities, and Phase 4.3 made it self-observing in SHADOW mode (16 manual
hooks remain after 3 anchor hooks were consolidated into the gate). These tests
prove the rollout control fails to OFF in every degenerate case, cannot be
flipped by a tenant, and that observation/aggregation still work — with
enforcement still OFF.
"""
import os

import pytest

from app.commercial import enforcement, phase5, manifests, ids
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

FLAG = enforcement.ENFORCEMENT_FLAG
ENV = enforcement._MASTER_ENV


@pytest.fixture(autouse=True)
def _clean_rollout_state():
    """Every test starts from the safe default and restores it after."""
    prior_env = os.environ.get(ENV)
    os.environ.pop(ENV, None)
    enforcement.arm_enforcement()
    enforcement.ENFORCE_CAPABILITIES.clear()
    phase5._FEATURE_FLAGS[FLAG]["overrides"].clear()
    phase5._FEATURE_FLAGS[FLAG]["default"] = False
    yield
    os.environ.pop(ENV, None)
    if prior_env is not None:
        os.environ[ENV] = prior_env
    enforcement.arm_enforcement()
    enforcement.ENFORCE_CAPABILITIES.clear()
    phase5._FEATURE_FLAGS[FLAG]["overrides"].clear()
    phase5._FEATURE_FLAGS[FLAG]["default"] = False


def _mode(cap="reports.advanced", org="org_x"):
    return enforcement.enforcement_mode(capability=cap, organization_id=org)


# ══════════════════════════════════════════════════════════════════════════════
# Default / degenerate configs all resolve to OFF
# ══════════════════════════════════════════════════════════════════════════════
def test_default_is_off():
    assert _mode() == enforcement.MODE_OFF
    assert enforcement.should_enforce(capability="reports.advanced",
                                      organization_id="org_x") is False


def test_missing_master_env_is_off():
    os.environ.pop(ENV, None)
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    assert _mode() == enforcement.MODE_OFF          # master gate closed


@pytest.mark.parametrize("bad", ["", "false", "0", "no", "off", "banana", "  "])
def test_invalid_or_false_master_env_is_off(bad):
    os.environ[ENV] = bad
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    assert _mode() == enforcement.MODE_OFF


def test_master_on_but_flag_off_is_off():
    os.environ[ENV] = "true"
    assert _mode() == enforcement.MODE_OFF          # per-org flag still off


def test_flag_on_but_capability_not_enrolled_is_shadow():
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    # capability NOT in ENFORCE_CAPABILITIES
    assert _mode() == enforcement.MODE_SHADOW


def test_all_layers_opt_in_is_enforce():
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    assert _mode() == enforcement.MODE_ENFORCE
    assert enforcement.should_enforce(capability="reports.advanced",
                                      organization_id="org_x") is True


# ══════════════════════════════════════════════════════════════════════════════
# Kill-switch
# ══════════════════════════════════════════════════════════════════════════════
def test_kill_switch_forces_off_even_when_fully_enabled():
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    assert _mode() == enforcement.MODE_ENFORCE
    enforcement.kill_enforcement()
    assert _mode() == enforcement.MODE_OFF          # instant, no restart
    enforcement.arm_enforcement()
    assert _mode() == enforcement.MODE_ENFORCE      # released


def test_arming_alone_does_not_enable_enforcement():
    enforcement.arm_enforcement()
    assert _mode() == enforcement.MODE_OFF          # every other gate still closed


# ══════════════════════════════════════════════════════════════════════════════
# Scope / progressive rollout
# ══════════════════════════════════════════════════════════════════════════════
def test_per_org_scope_isolation():
    os.environ[ENV] = "true"
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    phase5.set_override(FLAG, organization_id="org_a", value=True)
    assert enforcement.enforcement_mode(capability="reports.advanced",
                                        organization_id="org_a") == enforcement.MODE_ENFORCE
    # a different org, not overridden, is unaffected
    assert enforcement.enforcement_mode(capability="reports.advanced",
                                        organization_id="org_b") == enforcement.MODE_OFF


def test_per_capability_scope_isolation():
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    assert enforcement.enforcement_mode(capability="reports.advanced",
                                        organization_id="org_x") == enforcement.MODE_ENFORCE
    # a capability not enrolled stays shadow
    assert enforcement.enforcement_mode(capability="payroll.calculator",
                                        organization_id="org_x") == enforcement.MODE_SHADOW


def test_evaluation_is_deterministic():
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    assert _mode() == _mode() == enforcement.MODE_ENFORCE


def test_restart_resets_to_off():
    """Simulate a restart: in-memory flag state is rebuilt at import defaults."""
    phase5.set_override(FLAG, organization_id="org_x", value=True)
    os.environ[ENV] = "true"
    enforcement.ENFORCE_CAPABILITIES.add("reports.advanced")
    assert _mode() == enforcement.MODE_ENFORCE
    # A restart clears the in-memory override map and the in-process scope set.
    phase5._FEATURE_FLAGS[FLAG]["overrides"].clear()
    enforcement.ENFORCE_CAPABILITIES.clear()
    assert _mode() == enforcement.MODE_OFF


# ══════════════════════════════════════════════════════════════════════════════
# Security: not a tenant setting, not client-selectable
# ══════════════════════════════════════════════════════════════════════════════
def test_flag_is_not_an_ordinary_tenant_setting():
    """The enforcement flag lives in the phase5 rollout registry, not in the
    AppSettings tenant cascade, so no tenant/worker/admin app setting can reach
    it."""
    from app.settings.router import DEFAULTS as SETTINGS
    assert FLAG not in SETTINGS
    assert "entitlement_enforcement" not in SETTINGS


def test_the_override_route_requires_super_admin(db):
    """Only the super-admin commercial route can flip the phase5 override — a
    tenant admin or worker cannot."""
    from app.commercial.routers import admin as admin_router
    from fastapi import HTTPException
    for role in ("employee", "admin", "hr", "owner"):
        u = models.User(id=-3, name="x", email=f"ro-{role}@x.test",
                        organization_id=1, role=role)
        with pytest.raises(HTTPException) as exc:
            admin_router.set_feature_flag_override(
                "entitlement_enforcement", body={"organization_id": "org_x", "value": True},
                db=db, current_user=u)
        assert exc.value.status_code == 403


def test_status_reports_only_configuration_not_tenant_data():
    status = enforcement.enforcement_status()
    assert set(status) >= {"kill_switch", "master_env_enabled", "flag",
                           "enforce_capabilities", "global_mode"}
    blob = str(status).lower()
    for banned in ("password", "token", "secret", "email", "stripe"):
        assert banned not in blob


# ══════════════════════════════════════════════════════════════════════════════
# Shadow system remains intact and enforcement stays OFF
# ══════════════════════════════════════════════════════════════════════════════
def test_manual_shadow_hooks_after_gate_consolidation():
    """Phase 4.3 consolidated the 3 manual hooks on WIRED anchor routes
    (activity.anomaly/risk-users, payroll.calculator/configs, integrations.api
    GET) into the self-observing rollout gate, leaving 16 manual hooks. Those 3
    capabilities are now observed by the gate across every wired route, not by a
    single per-route hook."""
    import ast, pathlib
    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    n = 0
    for f in app.rglob("*.py"):
        if f.name == "enforcement.py":
            continue
        src = f.read_text(encoding="utf-8")
        if "_shadow(" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_shadow":
                n += 1
    assert n == 16


def test_gate_wired_only_to_approved_caps_and_still_off():
    """Phase 4.2 wired require_capability to the approved safe-first admin
    capabilities. The rollout control is unchanged and still OFF, and the gate is
    rollout-controlled, so the wired routes cannot 403 in the current config. This
    pins that (a) only approved capabilities are wired, and (b) enforcement is
    still globally OFF by default."""
    import pathlib
    import re
    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    pat = re.compile(r'require(?:_writable)?_capability\("([a-z0-9_.]+)"\)')
    wired = set()
    for p in app.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            wired.add(m.group(1))
    approved = {"activity.anomaly", "integrations.api",
                "payroll.calculator", "holidays.multicountry"}
    assert wired == approved, f"unexpected wired capabilities: {wired}"
    # Enforcement remains OFF by default: nothing is enrolled and the master env
    # is unset in this test environment.
    assert enforcement.ENFORCE_CAPABILITIES == set()
    assert enforcement.enforcement_mode() == enforcement.MODE_OFF


def test_shadow_capability_still_never_blocks(db):
    org = models.Organization(name="ShadowOff", billing_type="customer")
    db.add(org)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name="s", country="US",
                                  commercial_state="active", operational_org_id=org.id))
    db.add(CommercialSubscription(id=ids.subscription_id(), organization_id=org_id,
                                  plan_code="verified", billing_cycle="monthly",
                                  seat_count=5, state="active", provider="stub"))
    db.flush()
    u = models.User(name="U", email="shadowoff@x.test", organization_id=org.id, role="admin")
    db.add(u)
    db.flush()
    # A denied capability, shadow-observed, still returns None (never raises).
    assert enforcement.shadow_capability(
        db, u, "reports.compliance", route="GET /x") is None


# ══════════════════════════════════════════════════════════════════════════════
# Aggregation / blast-radius (population sweep, traffic-free)
# ══════════════════════════════════════════════════════════════════════════════
def _tenant(db, *, plan, state="active"):
    op = models.Organization(name=f"agg {ids.org_id()[:6]}", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name=op.name, country="US",
                                  commercial_state=state, operational_org_id=op.id))
    db.add(CommercialSubscription(id=ids.subscription_id(), organization_id=org_id,
                                  plan_code=plan, billing_cycle="monthly",
                                  seat_count=5, state=state, provider="stub"))
    db.flush()
    return org_id


def test_shadow_report_aggregates_blast_radius(db):
    _tenant(db, plan="verified")
    _tenant(db, plan="verified")
    _tenant(db, plan="governed")
    _tenant(db, plan="sovereign")
    rep = enforcement.shadow_report(
        db, capabilities=["reports.compliance", "reports.advanced", "identity.sso"])
    assert rep["organizations"] >= 4
    # compliance is Sovereign+, so verified+governed would be blocked.
    assert rep["would_block"]["reports.compliance"] >= 3
    # SSO is all-plan now — blocked nowhere.
    assert rep["would_block"].get("identity.sso", 0) == 0
    assert "by_plan" in rep and "by_state" in rep
    assert isinstance(rep["denied_organizations"], list)


def test_shadow_report_counts_allow_and_deny(db):
    _tenant(db, plan="verified")
    rep = enforcement.shadow_report(db, capabilities=["reports.advanced"])
    # reports.advanced is Governed+ ; a plain verified tenant (no grandfather) is blocked.
    assert rep["would_block"].get("reports.advanced", 0) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# Golden invariants (no decision change)
# ══════════════════════════════════════════════════════════════════════════════
def test_no_manifest_or_decision_change():
    assert manifests.capabilities_for("verified")["identity.sso"] == manifests.ALLOW
    assert manifests.capabilities_for("verified")["payroll.calculator"] == manifests.DENY
    assert all(manifests.capabilities_for(p)["workspace.settings.rbac_advanced"] == manifests.ALLOW
               for p in manifests.PLANS)
    for plan in manifests.PLANS:
        assert all(manifests.capabilities_for(plan)[k] == manifests.DENY
                   for k in manifests.GLOBALLY_DENIED)
