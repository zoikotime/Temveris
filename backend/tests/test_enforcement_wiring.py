"""Phase 4.2 — the rollout-gated enforcement dependency (require_capability).

require_capability / require_writable_capability are now attached to the approved
safe-first admin routes, but they route through ``enforcement._gate_capability``,
which consults the rollout control (``should_enforce`` / ``enforcement_mode``)
FIRST. That is what makes them safe to wire while enforcement is OFF:

  * OFF / SHADOW  -> non-blocking. A denied tenant is NOT 403'd; the request
                    proceeds exactly as before. This is the current prod state.
  * ENFORCE       -> the decision applies (403 DENY / 409 CONFIGURATION_REQUIRED
                    / READ_ONLY write refusal), via the unchanged
                    ``_assert_capability`` unit.

These tests pin the safety: denied-but-OFF never raises, enforce-mode raises,
per-org isolation holds, and the kill-switch forces OFF instantly.
"""
import os

import pytest
from fastapi import HTTPException

from app.commercial import enforcement, phase5, ids
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

FLAG = enforcement.ENFORCEMENT_FLAG
ENV = enforcement._MASTER_ENV


@pytest.fixture(autouse=True)
def _clean_rollout_state():
    """Every test starts from the safe default (all OFF) and restores it after."""
    prior = os.environ.get(ENV)
    os.environ.pop(ENV, None)
    enforcement.arm_enforcement()
    enforcement.ENFORCE_CAPABILITIES.clear()
    phase5._FEATURE_FLAGS[FLAG]["overrides"].clear()
    phase5._FEATURE_FLAGS[FLAG]["default"] = False
    yield
    os.environ.pop(ENV, None)
    if prior is not None:
        os.environ[ENV] = prior
    enforcement.arm_enforcement()
    enforcement.ENFORCE_CAPABILITIES.clear()
    phase5._FEATURE_FLAGS[FLAG]["overrides"].clear()
    phase5._FEATURE_FLAGS[FLAG]["default"] = False


def _tenant(db, *, plan="verified", state="active"):
    op = models.Organization(name=f"wire {ids.org_id()[:8]}", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name=op.name, country="US",
                                  commercial_state=state, operational_org_id=op.id))
    db.add(CommercialSubscription(id=ids.subscription_id(), organization_id=org_id,
                                  plan_code=plan, billing_cycle="monthly", seat_count=5,
                                  state=state, provider="stub"))
    db.flush()
    u = models.User(name="Admin", email=f"a+{org_id[:8]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()
    return org_id, u


def _fully_enforce(cap, org_id):
    """Opt every rollout layer in for one (capability, org): master env, per-org
    flag, per-capability scope."""
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id=org_id, value=True)
    enforcement.ENFORCE_CAPABILITIES.add(cap)


# ══════════════════════════════════════════════════════════════════════════════
# Non-blocking while enforcement is OFF (the current production state)
# ══════════════════════════════════════════════════════════════════════════════
def test_denied_tenant_is_not_blocked_while_off(db):
    """A Verified tenant is DENIED activity.anomaly / payroll.calculator, but with
    enforcement OFF the gate must return without raising — no 403 in production."""
    _, u = _tenant(db, plan="verified")
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)
    enforcement._gate_capability(db, u, "payroll.calculator", writable=True)
    enforcement._gate_capability(db, u, "integrations.api", writable=True)
    enforcement._gate_capability(db, u, "holidays.multicountry", writable=True)


def test_shadow_mode_is_non_blocking(db):
    """Master + per-org flag on, but the capability is NOT enrolled in
    ENFORCE_CAPABILITIES -> shadow -> the gate still does not block."""
    org_id, u = _tenant(db, plan="verified")
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id=org_id, value=True)
    # capability deliberately NOT added to ENFORCE_CAPABILITIES
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)


# ══════════════════════════════════════════════════════════════════════════════
# 403 only when every rollout layer opts in
# ══════════════════════════════════════════════════════════════════════════════
def test_denied_tenant_is_403_when_fully_enforced(db):
    org_id, u = _tenant(db, plan="verified")           # activity.anomaly -> DENY
    _fully_enforce("activity.anomaly", org_id)
    with pytest.raises(HTTPException) as exc:
        enforcement._gate_capability(db, u, "activity.anomaly", writable=False)
    assert exc.value.status_code == 403


def test_entitled_tenant_passes_when_enforcing(db):
    org_id, u = _tenant(db, plan="governed")           # activity.anomaly -> ALLOW
    _fully_enforce("activity.anomaly", org_id)
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)  # no raise


def test_readonly_write_refused_when_enforcing(db):
    """A suspended tenant keeps READ_ONLY historical access; a write gate refuses
    it (403) while a read gate permits it — only in enforce mode."""
    org_id, u = _tenant(db, plan="sovereign", state="suspended")
    _fully_enforce("reports.standard", org_id)         # historical -> READ_ONLY when suspended
    enforcement._gate_capability(db, u, "reports.standard", writable=False)   # read ok
    with pytest.raises(HTTPException):
        enforcement._gate_capability(db, u, "reports.standard", writable=True)  # write refused


# ══════════════════════════════════════════════════════════════════════════════
# Per-org isolation + kill switch
# ══════════════════════════════════════════════════════════════════════════════
def test_per_org_isolation(db):
    org_a, ua = _tenant(db, plan="verified")
    org_b, ub = _tenant(db, plan="verified")
    os.environ[ENV] = "true"
    enforcement.ENFORCE_CAPABILITIES.add("activity.anomaly")
    phase5.set_override(FLAG, organization_id=org_a, value=True)   # only org A enrolled
    with pytest.raises(HTTPException):
        enforcement._gate_capability(db, ua, "activity.anomaly", writable=False)
    # org B is not enrolled -> non-blocking, even with master + scope on.
    enforcement._gate_capability(db, ub, "activity.anomaly", writable=False)


def test_kill_switch_forces_off(db):
    org_id, u = _tenant(db, plan="verified")
    _fully_enforce("activity.anomaly", org_id)
    enforcement.kill_enforcement()
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)   # instant OFF
    enforcement.arm_enforcement()
    with pytest.raises(HTTPException):
        enforcement._gate_capability(db, u, "activity.anomaly", writable=False)


# ══════════════════════════════════════════════════════════════════════════════
# Worker safety: the payroll self-view is never gated
# ══════════════════════════════════════════════════════════════════════════════
def test_worker_my_config_route_has_no_capability_gate():
    import pathlib
    payroll = (pathlib.Path(enforcement.__file__).resolve().parents[1]
               / "payroll" / "router.py").read_text(encoding="utf-8")
    # The decorator block from /my-config up to its handler must carry no gate.
    seg = payroll.split('@router.get("/my-config"')[1].split("def my_payroll_config")[0]
    assert "require_capability" not in seg
    assert "require_writable_capability" not in seg


def test_the_gate_factory_is_still_a_dependency_factory():
    dep = enforcement.require_capability("activity.anomaly")
    assert callable(dep)
    assert enforcement.require_writable_capability("payroll.calculator") is not dep


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4.3 — the self-observing gate: SHADOW mode observes and never blocks
# ══════════════════════════════════════════════════════════════════════════════
def _shadow_mode(org_id):
    """Master + per-org flag on, capability NOT enrolled -> SHADOW."""
    os.environ[ENV] = "true"
    phase5.set_override(FLAG, organization_id=org_id, value=True)
    # deliberately NOT adding the capability to ENFORCE_CAPABILITIES


def test_shadow_mode_observes_denied_and_does_not_block(db, monkeypatch):
    org_id, u = _tenant(db, plan="verified")                # activity.anomaly DENY
    _shadow_mode(org_id)
    calls = []
    monkeypatch.setattr(enforcement, "shadow_capability",
                        lambda *a, **k: calls.append((a, k)))
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)  # must not raise
    assert len(calls) == 1
    assert calls[0][0][2] == "activity.anomaly"             # feature_key positional


def test_shadow_mode_observes_entitled_too(db, monkeypatch):
    org_id, u = _tenant(db, plan="governed")                # activity.anomaly ALLOW
    _shadow_mode(org_id)
    calls = []
    monkeypatch.setattr(enforcement, "shadow_capability", lambda *a, **k: calls.append(1))
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)  # must not raise
    assert len(calls) == 1                                  # observed even when entitled


def test_off_mode_emits_no_shadow_observation(db, monkeypatch):
    """Globally OFF (master env unset) must produce NO shadow rows — the gate
    returns on the fast path before observing."""
    _, u = _tenant(db, plan="verified")
    calls = []
    monkeypatch.setattr(enforcement, "shadow_capability", lambda *a, **k: calls.append(1))
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)
    assert calls == []


def test_shadow_observation_is_fail_open(db, monkeypatch):
    """If shadow telemetry itself raises, the gate must swallow it and let the
    request proceed — observation must never block."""
    org_id, u = _tenant(db, plan="verified")
    _shadow_mode(org_id)

    def _boom(*a, **k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(enforcement, "shadow_capability", _boom)
    enforcement._gate_capability(db, u, "activity.anomaly", writable=False)  # must not raise


def test_no_duplicate_observation_on_consolidated_routes():
    """The 3 wired anchor routes must no longer carry a manual _shadow hook for
    the same capability (that would double-log in shadow mode). The holidays GET
    hook is intentionally kept — its route is not the gated writable one."""
    import pathlib
    root = pathlib.Path(enforcement.__file__).resolve().parents[1]
    intel = (root / "intelligence" / "router.py").read_text(encoding="utf-8")
    payroll = (root / "payroll" / "router.py").read_text(encoding="utf-8")
    workspace = (root / "workspace" / "router.py").read_text(encoding="utf-8")
    assert "_shadow(" not in intel                          # activity.anomaly consolidated
    assert "_shadow(" not in payroll                        # payroll.calculator consolidated
    assert '_shadow(db, current_user, "integrations.api"' not in workspace
    assert '_shadow(db, current_user, "holidays.multicountry"' in workspace  # kept
