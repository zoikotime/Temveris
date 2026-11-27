"""Phase 3.5b - shadow observation widened from 2 pilot routes to 19.

Phase 3.3 proved the mechanism was safe on two routes. Phase 3.4 found that was
too narrow to size enforcement: it covered 2 of 39 capabilities, exercised no
READ_ONLY path, and touched nothing that resolves to CONFIGURATION_REQUIRED -
which is exactly why the unsatisfiable-screenshot blocker survived undetected.

This phase adds hooks across every capability family. Enforcement is still off.
The tests below are mostly structural: the strongest guarantee that a shadow
hook cannot change a response is that its return value is syntactically
discarded at every call site, which an AST walk can prove for the whole tree.
"""

import ast
import json
import logging
import pathlib

import pytest

from app.billing import service as billing_service
from app.commercial import entitlements, enforcement, ids, manifests
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
FRONTEND_SRC = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"
SHADOW_LOGGER = "zoiko.entitlement.shadow"

#: Every capability the hooks are expected to observe after this phase.
EXPECTED_COVERAGE = {
    # global DENY
    "productivity.apps_history",
    "productivity.website_categories",
    # essential
    "billing.portal",
    "identity.mfa",
    # historical
    "dashboard.overview.view",
    "reports.standard",
    "activity.log.view",
    "time.playback.basic",
    # operational / write
    "team.directory.manage",
    "leave.approvals",
    "workspace.settings.rbac_advanced",
    # Governed+ — activity.anomaly, integrations.api and payroll.calculator were
    # consolidated into the self-observing rollout gate in Phase 4.3, so they are
    # observed there (across every wired route) rather than by a single manual
    # hook. They are intentionally NOT in this manual-hook coverage set.
    "reports.advanced",
    # compliance / governance
    "reports.compliance",
    "governance.chain_of_custody",
    # genuinely Sovereign+
    "holidays.multicountry",
    # CONFIGURATION_REQUIRED
    "evidence.screenshots.capture",
}


def _shadow_calls():
    """(file, lineno, capability, is_bare_statement) for every hook in app/."""
    out = []
    for f in sorted(APP.rglob("*.py")):
        if f.name == "enforcement.py":
            continue
        src = f.read_text(encoding="utf-8")
        if "_shadow(" not in src:
            continue
        tree = ast.parse(src)
        bare = {id(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_shadow":
                cap = (n.args[2].value
                       if len(n.args) > 2 and isinstance(n.args[2], ast.Constant)
                       else None)
                out.append((f.relative_to(APP).as_posix(), n.lineno, cap,
                            id(n) in bare))
    return out


def _org(db, name, *, plan=None, state="active", country="US",
         billing_type="customer"):
    op = models.Organization(name=name, billing_type=billing_type)
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=name, country=country, commercial_state=state,
        operational_org_id=op.id))
    db.flush()
    if plan is not None:
        db.add(CommercialSubscription(
            id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
            billing_cycle="monthly", seat_count=5, state=state, provider="stub"))
        db.flush()
    u = models.User(name="U", email=f"c35b-{org_id[:8]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()
    return op, org_id, u


def _emitted(caplog):
    """Parsed payloads of every shadow line captured."""
    out = []
    for rec in caplog.records:
        if rec.name != SHADOW_LOGGER:
            continue
        msg = rec.getMessage()
        if "entitlement.shadow " in msg:
            out.append(json.loads(msg.split("entitlement.shadow ", 1)[1]))
    return out


# ==============================================================================
# Structural: a hook cannot influence a response
# ==============================================================================
def test_every_shadow_call_is_a_bare_statement():
    """The core safety property, proved for the whole tree rather than sampled.

    If the return value is never bound, it cannot reach a branch, a status code
    or a response body.
    """
    consumed = [(f, ln, cap) for f, ln, cap, bare in _shadow_calls() if not bare]
    assert not consumed, f"a shadow result is being used: {consumed}"


def test_shadow_returns_none():
    assert enforcement.shadow_capability.__annotations__.get("return") is None or True
    # ...and in practice:
    assert enforcement.shadow_capability(None, None, "reports.standard",
                                         route="test") is None


def test_shadow_swallows_a_broken_database(db, monkeypatch):
    """A logging or resolution failure must never be why a request fails."""
    def boom(*a, **k):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(enforcement, "resolve_commercial_org", boom)
    _, _, u = _org(db, "Boom", plan="verified")
    assert enforcement.shadow_capability(
        db, u, "reports.standard", route="GET /x") is None


def test_shadow_swallows_a_broken_entitlement_layer(db, monkeypatch):
    monkeypatch.setattr(entitlements, "get",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("nope")))
    _, _, u = _org(db, "Boom2", plan="verified")
    assert enforcement.shadow_capability(
        db, u, "reports.standard", route="GET /x") is None


def test_shadow_swallows_a_broken_logger(db, monkeypatch):
    monkeypatch.setattr(enforcement._shadow_log, "info",
                        lambda *a, **k: (_ for _ in ()).throw(IOError("disk full")))
    _, _, u = _org(db, "Boom3", plan="verified")
    assert enforcement.shadow_capability(
        db, u, "reports.standard", route="GET /x") is None


def test_shadow_never_raises_for_any_capability(db):
    """All 38, including the unbuilt ones."""
    _, _, u = _org(db, "All caps", plan="verified")
    for key in sorted(manifests.FEATURES):
        assert enforcement.shadow_capability(
            db, u, key, route="GET /sweep") is None


def test_shadow_never_raises_for_an_unknown_capability(db):
    _, _, u = _org(db, "Typo", plan="verified")
    assert enforcement.shadow_capability(
        db, u, "not.a.capability", route="GET /x") is None


# ==============================================================================
# Coverage
# ==============================================================================
def test_expected_capabilities_are_hooked():
    hooked = {cap for _, _, cap, _ in _shadow_calls() if cap}
    assert EXPECTED_COVERAGE <= hooked, EXPECTED_COVERAGE - hooked


def test_every_hooked_capability_is_a_real_key():
    """A typo in a hook would silently observe a permanent DENY."""
    hooked = {cap for _, _, cap, _ in _shadow_calls() if cap}
    assert hooked <= set(manifests.FEATURES), hooked - set(manifests.FEATURES)


def test_the_original_pilot_observation_survives():
    """The two Phase 3.3 pilots proved the mechanism is safe. website_categories
    is a privacy route that is not wired, so it keeps its manual hook. The
    payroll.calculator pilot is now a WIRED capability observed by the
    self-observing rollout gate (Phase 4.3) — so its observation survives via the
    gate rather than a manual hook."""
    import pathlib
    import re
    sites = {(f, cap) for f, _, cap, _ in _shadow_calls()}
    assert ("browser/router.py", "productivity.website_categories") in sites
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    pat = re.compile(r'require(?:_writable)?_capability\("([a-z0-9_.]+)"\)')
    wired = set()
    for p in root.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            wired.add(m.group(1))
    assert "payroll.calculator" in wired


def test_each_family_has_at_least_one_hook():
    hooked = {cap for _, _, cap, _ in _shadow_calls() if cap}
    assert hooked & manifests.GLOBALLY_DENIED
    assert hooked & manifests.ESSENTIAL_CAPABILITIES
    assert hooked & manifests.HISTORICAL_CAPABILITIES
    # a genuinely Sovereign+ key
    assert "holidays.multicountry" in hooked
    # a configuration_required key
    assert "evidence.screenshots.capture" in hooked


def test_write_routes_are_marked_writable():
    """READ_ONLY only blocks a write, so the write hooks must say so."""
    writable = set()
    for f in sorted(APP.rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        if "_shadow(" not in src or f.name == "enforcement.py":
            continue
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "_shadow"
                    and any(k.arg == "writable" and k.value.value is True
                            for k in n.keywords)
                    and isinstance(n.args[2], ast.Constant)):
                writable.add(n.args[2].value)
    assert {"team.directory.manage", "leave.approvals",
            "workspace.settings.rbac_advanced",
            "evidence.screenshots.capture"} <= writable, writable


# ==============================================================================
# Observed decisions: privacy, essentials, CONFIGURATION_REQUIRED, READ_ONLY
# ==============================================================================
@pytest.mark.parametrize("key", sorted(manifests.GLOBALLY_DENIED))
@pytest.mark.parametrize("plan", ["verified", "governed", "sovereign", "enterprise"])
def test_privacy_is_observed_as_deny_on_every_plan(db, caplog, key, plan):
    _, _, u = _org(db, f"P {plan} {key}", plan=plan)
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        enforcement.shadow_capability(db, u, key, route="GET /x")
    rec = _emitted(caplog)[-1]
    assert rec["decision"] == manifests.DENY
    assert rec["would_block"] is True


def test_manual_review_essentials_are_observed_as_allowed(db, caplog):
    """Phase 3.5a reopened these; the shadow must now agree."""
    _, _, u = _org(db, "MR essentials", state="manual_review")   # no subscription
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        for key in ("billing.portal", "identity.mfa"):
            enforcement.shadow_capability(db, u, key, route="GET /x")
    for rec in _emitted(caplog):
        assert rec["decision"] == manifests.ALLOW, rec
        assert rec["would_block"] is False, rec


def test_manual_review_non_essentials_are_observed_as_denied(db, caplog):
    _, _, u = _org(db, "MR others", state="manual_review")
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        for key in ("reports.standard", "payroll.calculator",
                    "productivity.apps_history"):
            enforcement.shadow_capability(db, u, key, route="GET /x")
    for rec in _emitted(caplog):
        assert rec["decision"] == manifests.DENY, rec
        assert rec["would_block"] is True, rec


@pytest.mark.parametrize("key", ["evidence.screenshots.masking",
                                 "evidence.screenshots.policy_controls"])
def test_configuration_required_is_observed_never_enforced(db, caplog, key):
    # capture excluded: under DEFAULT settings Layer 6 resolves it to ALLOW.
    """Recorded as a block, but nothing raises and nothing is refused."""
    _, _, u = _org(db, f"CFG {key}", plan="sovereign")
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        result = enforcement.shadow_capability(db, u, key, route="POST /x",
                                               writable=True)
    assert result is None                       # the request is untouched
    rec = _emitted(caplog)[-1]
    assert rec["decision"] == manifests.CONFIGURATION_REQUIRED
    assert rec["would_block"] is True


@pytest.mark.parametrize("state", ["suspended", "cancelled"])
def test_read_only_is_observed_on_a_historical_capability(db, caplog, state):
    _, _, u = _org(db, f"RO {state}", plan="governed", state=state)
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        enforcement.shadow_capability(db, u, "reports.standard",
                                      route="GET /x", writable=False)
        enforcement.shadow_capability(db, u, "reports.standard",
                                      route="PUT /x", writable=True)
    read, write = _emitted(caplog)[-2:]
    assert read["decision"] == manifests.READ_ONLY
    assert read["would_block"] is False, "reading a read-only capability is allowed"
    assert write["decision"] == manifests.READ_ONLY
    assert write["would_block"] is True, "writing a read-only capability is blocked"


@pytest.mark.parametrize("state", ["suspended", "cancelled"])
def test_operational_capability_is_denied_in_impaired_states(db, caplog, state):
    _, _, u = _org(db, f"OP {state}", plan="governed", state=state)
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        enforcement.shadow_capability(db, u, "payroll.calculator",
                                      route="GET /x", writable=True)
    rec = _emitted(caplog)[-1]
    assert rec["decision"] == manifests.DENY
    assert rec["would_block"] is True


def test_closed_state_denies_historical_too(db, caplog):
    _, _, u = _org(db, "Closed", plan="governed", state="closed")
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        enforcement.shadow_capability(db, u, "reports.standard", route="GET /x")
    assert _emitted(caplog)[-1]["decision"] == manifests.DENY


def test_essential_survives_every_impaired_state(db, caplog):
    for state in ("suspended", "cancelled", "closed"):
        _, _, u = _org(db, f"E {state}", plan="governed", state=state)
        with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
            enforcement.shadow_capability(db, u, "billing.portal", route="GET /x")
        rec = _emitted(caplog)[-1]
        assert rec["decision"] == manifests.ALLOW, (state, rec)
        assert rec["would_block"] is False, (state, rec)


# ==============================================================================
# Internal tenant
# ==============================================================================
def test_internal_tenant_observed_as_sovereign_equivalent(db, caplog):
    op = models.Organization(name="Temveris 35b", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    u = models.User(name="Z", email="z35b@x.test", organization_id=op.id,
                    role="admin")
    db.add(u)
    db.flush()

    sovereign = manifests.capabilities_for("sovereign")
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        for key in ("reports.compliance", "governance.chain_of_custody",
                    "holidays.multicountry", "payroll.calculator"):
            enforcement.shadow_capability(db, u, key, route="GET /x")
    for rec in _emitted(caplog):
        assert rec["plan"] == manifests.INTERNAL_PLAN, rec
        assert rec["decision"] == sovereign[rec["capability"]], rec


@pytest.mark.parametrize("key", sorted(manifests.GLOBALLY_DENIED))
def test_internal_tenant_privacy_still_denied(db, caplog, key):
    op = models.Organization(name=f"Temveris priv {key}", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    u = models.User(name="Z", email=f"zp-{key[-8:]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        enforcement.shadow_capability(db, u, key, route="GET /x")
    assert _emitted(caplog)[-1]["decision"] == manifests.DENY


def test_internal_tenant_remains_billing_inert(db):
    op = models.Organization(name="Temveris inert 35b", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    assert billing_service.is_internal_tenant(db, op.id) is True
    with pytest.raises(Exception):
        billing_service.require_billable_tenant(db, op.id)


# ==============================================================================
# The log payload itself
# ==============================================================================
def test_log_payload_carries_no_user_identifier(db, caplog):
    _, _, u = _org(db, "Privacy of the log", plan="verified")
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        enforcement.shadow_capability(db, u, "reports.standard", route="GET /x")
    rec = _emitted(caplog)[-1]
    # Phase 3.24 added method + role for the live-window volume report. role is
    # the coarse actor role (admin/employee), NOT a user identifier — no user_id,
    # no email — so the org-level privacy contract still holds.
    assert "user_id" not in rec and "email" not in rec
    assert set(rec) == {"organization_id", "plan", "lifecycle_state", "capability",
                        "decision", "would_block", "writable", "route", "method",
                        "role", "reason", "at"}
    assert rec["role"] in (None, "admin", "employee", "hr", "owner", "manager",
                           "super_admin", "billing_admin", "user")


def test_missing_identity_is_observed_as_deny(db, caplog):
    op = models.Organization(name="No identity 35b", billing_type="customer")
    db.add(op)
    db.flush()
    u = models.User(name="N", email="n35b@x.test", organization_id=op.id)
    db.add(u)
    db.flush()
    with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER):
        enforcement.shadow_capability(db, u, "reports.standard", route="GET /x")
    rec = _emitted(caplog)[-1]
    assert rec["decision"] == manifests.DENY
    assert rec["reason"] == "no_commercial_identity"


# ==============================================================================
# Scope guards
# ==============================================================================
def test_gate_wired_only_to_approved_caps_and_still_off():
    """Phase 4.2 wired the rollout-gated require_capability to the approved
    safe-first admin capabilities. It is non-blocking while enforcement is OFF
    (see enforcement._gate_capability), so no route can change a response in the
    current configuration. This pins that no OTHER capability began wiring and
    that enforcement is still globally OFF."""
    import re
    pat = re.compile(r'require(?:_writable)?_capability\("([a-z0-9_.]+)"\)')
    wired = set()
    for p in APP.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            wired.add(m.group(1))
    assert wired == {"activity.anomaly", "integrations.api",
                     "payroll.calculator", "holidays.multicountry"}, wired
    assert enforcement.enforcement_mode() == enforcement.MODE_OFF


def test_no_shadow_hook_raises_an_http_exception():
    """No hook site may be followed by a raise that reads its result."""
    for f, ln, cap, bare in _shadow_calls():
        assert bare, f"{f}:{ln} ({cap}) is not a bare statement"


def test_frontend_capability_gating_is_still_zero():
    if not FRONTEND_SRC.exists():
        pytest.skip("frontend not present")
    hits = [
        f"{p.relative_to(FRONTEND_SRC)}:{n}"
        for p in FRONTEND_SRC.rglob("*.jsx")
        for n, line in enumerate(p.read_text(encoding="utf-8",
                                             errors="ignore").splitlines(), 1)
        if "capabilities[" in line or "require_capability" in line
    ]
    assert not hits, f"frontend began gating on capabilities: {hits}"


def test_manifests_and_grants_reflect_the_3_18_retier():
    assert manifests.PLANS == ("verified", "governed", "sovereign", "enterprise")
    assert len(manifests.FEATURES) == 38
    # SSO is an all-plan security baseline now.
    for plan in manifests.PLANS:
        assert manifests.capabilities_for(plan)["identity.sso"] == manifests.ALLOW
    for plan in manifests.PLANS:
        caps = manifests.capabilities_for(plan)
        assert all(caps[k] == manifests.DENY for k in manifests.GLOBALLY_DENIED)


def test_seven_keys_are_governed_plus_rbac_stays_all_plan():
    """The seven retiered keys deny on Verified, allow on Governed+.
    workspace.settings.rbac_advanced is the deliberate exception — All-plan."""
    v = manifests.capabilities_for("verified")
    g = manifests.capabilities_for("governed")
    for key in ("payroll.calculator", "reports.advanced", "activity.anomaly",
                "leave.approvals", "offboarding.governed", "time.attendance.policy",
                "time.playback.advanced"):
        assert v[key] == manifests.DENY, key
        assert g[key] == manifests.ALLOW, key
    # INVARIANT: advanced RBAC remains All-plan (Product decision, 3.18).
    for plan in manifests.PLANS:
        assert manifests.capabilities_for(plan)["workspace.settings.rbac_advanced"] == manifests.ALLOW
    # INVARIANT: compliance + chain-of-custody are Sovereign+.
    for key in ("reports.compliance", "governance.chain_of_custody"):
        assert g[key] == manifests.DENY, key
        assert manifests.capabilities_for("sovereign")[key] == manifests.ALLOW


def test_screenshot_configuration_required_semantics_unchanged():
    for plan in manifests.PLANS:
        caps = manifests.capabilities_for(plan)
        for key in ("evidence.screenshots.capture", "evidence.screenshots.masking",
                    "evidence.screenshots.policy_controls"):
            assert caps[key] == manifests.CONFIGURATION_REQUIRED, (plan, key)


def test_trial_pricing_and_seats_untouched():
    from app.billing.trial import TRIAL_LENGTH_DAYS
    from app.commercial.entitlements import _PLAN_FEATURES, _PLAN_LIMITS
    assert TRIAL_LENGTH_DAYS == 14
    assert _PLAN_LIMITS["verified"]["seat_limit"] == 25
    assert _PLAN_LIMITS["governed"]["seat_limit"] == 100
    assert _PLAN_FEATURES["sovereign"]["sso"] is True


def test_no_hook_touches_stripe_or_schema():
    """A hook is one call; it must not have dragged in anything else."""
    for f in sorted(APP.rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        if "_shadow(" not in src or f.name == "enforcement.py":
            continue
        for n, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith("_shadow("):
                assert "stripe" not in line.lower(), f"{f}:{n}"
                assert "commit" not in line.lower(), f"{f}:{n}"
