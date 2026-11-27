"""Phase 3 - internal tenant, lifecycle policy, and the capability gate.

Three locked business rules are pinned here:

  1. Temveris's own tenant (billing_type='internal') resolves *through* the
     entitlement resolver with Sovereign-equivalent capabilities, holds no
     subscription and no Stripe object, and is still subject to the global
     privacy denials.
  2. Lifecycle state narrows capabilities: trial/active/past-due/cancel-pending
     keep the full plan profile; suspended and cancelled deny premium writes
     while preserving historical reads; closed leaves only essentials.
  3. require_capability answers the WORKSPACE question only. Actor RBAC is a
     separate check and this must not stand in for it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.billing import service as billing_service
from app.commercial import constants, entitlements, ids, manifests
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, EntitlementsCache,
)
from database import models

LIFECYCLE_STATES = ["trial_active", "active", "past_due", "suspended",
                    "cancelled_pending_end", "cancelled", "closed"]


def _customer(db, *, plan="sovereign", state="active"):
    op = models.Organization(name=f"Cust {ids.org_id()[:8]}", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country="US", commercial_state=state,
        operational_org_id=op.id))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
        billing_cycle="monthly", seat_count=10, state=state, provider="stub"))
    db.flush()
    return op, org_id


def _internal(db, name="Temveris"):
    """An internal tenant, created the way bootstrap creates it: identity only."""
    op = models.Organization(name=name, billing_type="internal")
    db.add(op)
    db.flush()
    ensure_internal_identity()
    org = (db.query(CommercialOrganization)
             .filter_by(operational_org_id=op.id).one_or_none())
    if org is None:      # bootstrap uses its own session; mirror it here
        org = CommercialOrganization(
            id=ids.org_id(), name=op.name, country=None,
            commercial_state="active", operational_org_id=op.id)
        db.add(org)
        db.flush()
    return op, org.id


# ══════════════════════════════════════════════════════════════════════════════
# A. Temveris internal tenant
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_resolves_through_the_resolver(db):
    """Not around it. A bypass would skip the global privacy denials."""
    _, org_id = _internal(db)
    profile = entitlements.compute(db, org_id)
    assert profile["plan"] == manifests.INTERNAL_PLAN
    assert profile["manifest_version"] == manifests.CURRENT_VERSIONS[manifests.INTERNAL_PLAN]


def test_internal_tenant_gets_sovereign_equivalent_capabilities(db):
    _, org_id = _internal(db)
    caps = entitlements.compute(db, org_id)["capabilities"]
    assert caps == {**manifests.capabilities_for("sovereign"), "evidence.screenshots.capture": manifests.ALLOW}


def test_internal_tenant_gets_sovereign_equivalent_legacy_features(db):
    """_PLAN_FEATURES has no internal row; without the alias it would silently
    fall through to Verified and drag the capability map down with it."""
    from app.commercial.entitlements import _PLAN_LIMITS
    _, org_id = _internal(db)
    profile = entitlements.compute(db, org_id)

    # The Sovereign-only flags, which Verified would not have granted.
    for flag in ("sso", "scim", "legal_hold", "api_access", "advanced_compliance"):
        assert profile["features"][flag] is True, flag
    assert profile["limits"] == _PLAN_LIMITS["sovereign"]

    # data_export is deliberately NOT asserted: the internal identity has no
    # owner_user_id, so the email-verification layer clears it. That is the
    # existing I-6 rule applying honestly, not a defect in the alias.
    assert profile["restrictions"]["email_unverified"] is True


@pytest.mark.parametrize("key", sorted(manifests.GLOBALLY_DENIED))
def test_internal_tenant_still_denied_the_privacy_capabilities(db, key):
    """The whole reason for routing internal through the resolver."""
    _, org_id = _internal(db)
    assert entitlements.compute(db, org_id)["capabilities"][key] == manifests.DENY


def test_internal_tenant_has_no_subscription_and_needs_none(db):
    _, org_id = _internal(db)
    assert db.query(CommercialSubscription).filter_by(
        organization_id=org_id).one_or_none() is None
    assert entitlements.compute(db, org_id)["plan"] == manifests.INTERNAL_PLAN


def test_internal_tenant_remains_non_billable(db):
    op, _ = _internal(db)
    assert billing_service.is_internal_tenant(db, op.id) is True


def test_internal_identity_creation_is_idempotent(db):
    op, _ = _internal(db)
    ensure_internal_identity()
    ensure_internal_identity()
    assert db.query(CommercialOrganization).filter_by(
        operational_org_id=op.id).count() == 1


def test_a_customer_org_is_never_treated_as_internal(db):
    _, org_id = _customer(db, plan="verified")
    assert entitlements.compute(db, org_id)["plan"] == "verified"


def test_an_unlinked_commercial_org_is_not_promoted_to_internal(db):
    """is_internal_tenant fails CLOSED for a missing org - right for billing,
    catastrophic here. An org with no operational link is not provably internal.
    """
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name="Legacy", country="US", commercial_state="active",
        operational_org_id=None))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code="verified",
        billing_cycle="monthly", seat_count=1, state="active", provider="stub"))
    db.flush()
    assert entitlements.compute(db, org_id)["plan"] == "verified"


# ══════════════════════════════════════════════════════════════════════════════
# B/C. Lifecycle policy
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state", ["trial_active", "active", "past_due",
                                   "cancelled_pending_end"])
def test_full_entitlements_are_retained(db, state):
    """Trial, active, grace and cancel-pending all keep the plan profile."""
    _, org_id = _customer(db, plan="sovereign", state=state)
    caps = entitlements.compute(db, org_id)["capabilities"]
    assert caps == {**manifests.capabilities_for("sovereign"), "evidence.screenshots.capture": manifests.ALLOW}


def test_past_due_no_longer_withdraws_api_access(db):
    """Locked rule: past-due is not a downgrade, and no risk policy exists."""
    _, org_id = _customer(db, plan="governed", state="past_due")
    profile = entitlements.compute(db, org_id)
    assert profile["features"]["api_access"] is True
    assert profile["capabilities"]["integrations.api"] == manifests.ALLOW


@pytest.mark.parametrize("state", ["suspended", "cancelled"])
def test_premium_writes_are_denied(db, state):
    _, org_id = _customer(db, plan="sovereign", state=state)
    caps = entitlements.compute(db, org_id)["capabilities"]
    for key in ("payroll.calculator", "leave.approvals", "team.directory.manage",
                "time.attendance.policy", "workspace.settings.core",
                "offboarding.governed", "integrations.api"):
        assert caps[key] == manifests.DENY, key


@pytest.mark.parametrize("state", ["suspended", "cancelled"])
def test_historical_reads_become_read_only_not_denied(db, state):
    """READ_ONLY is not a softer DENY - these are genuinely viewable records."""
    _, org_id = _customer(db, plan="sovereign", state=state)
    caps = entitlements.compute(db, org_id)["capabilities"]
    for key in ("reports.standard", "reports.advanced", "time.attendance.view",
                "time.playback.basic", "activity.log.view",
                "evidence.screenshots.view", "dashboard.overview.view"):
        assert caps[key] == manifests.READ_ONLY, key


@pytest.mark.parametrize("state", LIFECYCLE_STATES)
def test_essential_routes_survive_every_state(db, state):
    """Spec essential-route invariant: a customer locked out of billing cannot
    pay their way out of suspension.

    "Essential" means LIFECYCLE-INVARIANT, not plan-universal. Lifecycle only
    narrows, so it can never hand a plan a capability the manifest withheld —
    identity.sso is denied on Verified under the shipped packaging and stays
    denied. What must hold is that no lifecycle state *removes* an essential
    capability the plan does grant.
    """
    _, org_id = _customer(db, plan="sovereign", state=state)
    caps = entitlements.compute(db, org_id)["capabilities"]
    baseline = manifests.capabilities_for("sovereign")
    for key in sorted(manifests.ESSENTIAL_CAPABILITIES):
        assert caps[key] == baseline[key], f"{key} changed in {state}"


@pytest.mark.parametrize("state", LIFECYCLE_STATES)
def test_billing_portal_is_reachable_in_every_state_on_every_plan(db, state):
    """The concrete case the invariant exists for: billing is granted on every
    plan, so it must survive every lifecycle state."""
    for plan in ("verified", "governed", "sovereign"):
        _, org_id = _customer(db, plan=plan, state=state)
        caps = entitlements.compute(db, org_id)["capabilities"]
        assert caps["billing.portal"] == manifests.ALLOW, f"{plan}/{state}"


@pytest.mark.parametrize("state", ["suspended", "cancelled", "closed"])
def test_collection_stops(db, state):
    """'Disable collection' / 'stop active collection'."""
    _, org_id = _customer(db, plan="sovereign", state=state)
    caps = entitlements.compute(db, org_id)["capabilities"]
    assert caps["evidence.screenshots.capture"] == manifests.DENY


def test_closed_leaves_only_essentials(db):
    _, org_id = _customer(db, plan="sovereign", state="closed")
    caps = entitlements.compute(db, org_id)["capabilities"]
    for key, decision in caps.items():
        if key in manifests.ESSENTIAL_CAPABILITIES:
            assert decision == manifests.ALLOW, key
        elif key in manifests.LIFECYCLE_UNDECIDED:
            continue
        else:
            assert decision == manifests.DENY, key


def test_lifecycle_can_only_narrow_never_widen(db):
    """A suspended Verified tenant must not reach a Governed capability."""
    _, org_id = _customer(db, plan="verified", state="suspended")
    caps = entitlements.compute(db, org_id)["capabilities"]
    base = manifests.capabilities_for("verified")
    for key, decision in caps.items():
        if base[key] == manifests.DENY:
            assert decision == manifests.DENY, key


def test_states_outside_the_approved_policy_are_left_alone(db):
    """trial_expired and friends were not covered by the locked policy, so they
    keep today's behaviour rather than an invented one."""
    _, org_id = _customer(db, plan="governed", state="manual_review")
    caps = entitlements.compute(db, org_id)["capabilities"]
    assert caps["reports.advanced"] == manifests.ALLOW


def test_every_capability_has_exactly_one_nature():
    natures = {k: manifests.capability_nature(k) for k in manifests.FEATURES}
    assert set(natures.values()) <= {"essential", "historical", "operational", "undecided"}
    assert len(natures) == 38


# ══════════════════════════════════════════════════════════════════════════════
# F. Global DENY - every plan, internal, every lifecycle state
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("key", sorted(manifests.GLOBALLY_DENIED))
@pytest.mark.parametrize("state", LIFECYCLE_STATES)
def test_privacy_capabilities_denied_in_every_state(db, key, state):
    _, org_id = _customer(db, plan="sovereign", state=state)
    assert entitlements.compute(db, org_id)["capabilities"][key] == manifests.DENY


@pytest.mark.parametrize("plan", list(manifests.PLANS) + [manifests.INTERNAL_PLAN])
def test_no_manifest_anywhere_grants_a_privacy_capability(plan):
    for key in manifests.GLOBALLY_DENIED:
        assert manifests.decision_for(plan, key) == manifests.DENY


def test_the_validator_rejects_an_internal_manifest_that_grants_one(monkeypatch):
    version = manifests.CURRENT_VERSIONS[manifests.INTERNAL_PLAN]
    broken = dict(manifests.MANIFESTS)
    m = broken[version]
    grants = dict(m.grants)
    grants["productivity.apps_history"] = manifests.Grant(
        manifests.ALLOW, manifests.POLICY)
    broken[version] = m._replace(grants=grants)
    monkeypatch.setattr(manifests, "MANIFESTS", broken)
    with pytest.raises(manifests.ManifestError, match="globally denied"):
        manifests._validate()


# ══════════════════════════════════════════════════════════════════════════════
# D. Cache behaviour for the internal tenant
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_caches_and_serves(db):
    """The snapshot must be derived identically in compute() and get(), or the
    subscription-less tenant would recompute on every single read."""
    _, org_id = _internal(db)
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    row.profile_json = {**row.profile_json, "sentinel": "kept"}
    db.flush()
    assert entitlements.get(db, org_id).get("sentinel") == "kept"


def test_internal_tenant_cache_invalidates_on_state_change(db):
    _, org_id = _internal(db)
    entitlements.compute(db, org_id)
    org = db.query(CommercialOrganization).filter_by(id=org_id).one()
    org.commercial_state = "suspended"
    db.flush()
    caps = entitlements.get(db, org_id)["capabilities"]
    assert caps["payroll.calculator"] == manifests.DENY


# ══════════════════════════════════════════════════════════════════════════════
# E. Entitlement is not authorization  (MANDATORY distinction)
# ══════════════════════════════════════════════════════════════════════════════
def test_capability_decision_ignores_the_actor_role(db):
    """require_capability answers the WORKSPACE question. It must return the
    same answer for an admin and an employee - RBAC is a separate check, and if
    this ever varied by role the two concerns would have been conflated."""
    from app.commercial import enforcement

    op, org_id = _customer(db, plan="governed", state="active")
    admin = models.User(name="A", email=f"a+{org_id[:8]}@x.test",
                        organization_id=op.id, role="admin")
    worker = models.User(name="W", email=f"w+{org_id[:8]}@x.test",
                         organization_id=op.id, role="employee")
    db.add_all([admin, worker])
    db.flush()

    a = enforcement.capability_decision(db, admin, "reports.advanced")
    w = enforcement.capability_decision(db, worker, "reports.advanced")
    assert a == w == manifests.ALLOW


def test_a_non_entitled_workspace_is_denied_whatever_the_role(db):
    from app.commercial import enforcement

    op, org_id = _customer(db, plan="verified", state="active")
    admin = models.User(name="A", email=f"a2+{org_id[:8]}@x.test",
                        organization_id=op.id, role="admin")
    db.add(admin)
    db.flush()
    # governance.ediscovery is unbuilt, so denied on every plan.
    assert enforcement.capability_decision(db, admin, "governance.ediscovery") == manifests.DENY


def test_unknown_capability_key_denies(db):
    from app.commercial import enforcement

    op, org_id = _customer(db, plan="sovereign")
    u = models.User(name="U", email=f"u+{org_id[:8]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()
    assert enforcement.capability_decision(db, u, "not.a.capability") == manifests.DENY


def test_unresolvable_tenant_now_fails_closed(db):
    """Was: "None means no commercial identity, deliberately distinct from DENY".

    That distinction was load-bearing for the old fail-open, and it meant a
    tenant with no identity bypassed every capability check — including the two
    globally denied privacy capabilities. Phase 3.1 audited it, Phase 3.2 closed
    it: the organizations that predated commercial provisioning were backfilled,
    and an unresolvable caller is now denied rather than skipped.
    """
    from app.commercial import enforcement

    op = models.Organization(name="No commercial row", billing_type="customer")
    db.add(op)
    db.flush()
    u = models.User(name="U", email=f"nc+{op.id}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()

    assert enforcement.capability_decision(db, u, "reports.standard") == manifests.DENY
    # The reason it matters:
    assert enforcement.capability_decision(
        db, u, "productivity.apps_history") == manifests.DENY


# ══════════════════════════════════════════════════════════════════════════════
# The gate itself
# ══════════════════════════════════════════════════════════════════════════════
def test_require_capability_is_a_dependency_factory():
    from app.commercial import enforcement
    dep = enforcement.require_capability("reports.advanced")
    assert callable(dep)
    assert enforcement.require_capability("x") is not dep


def test_allow_passes_and_deny_raises_403(db):
    from fastapi import HTTPException
    from app.commercial import enforcement

    op, org_id = _customer(db, plan="verified", state="active")
    u = models.User(name="U", email=f"g+{org_id[:8]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()

    enforcement._assert_capability(db, u, "reports.standard", writable=False)

    with pytest.raises(HTTPException) as exc:
        enforcement._assert_capability(db, u, "governance.ediscovery", writable=False)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == constants.ERR_ENTITLEMENT_FAILED


def test_configuration_required_raises_409_not_403(db):
    """A setup step, not an upgrade - the UI must not show a pricing page."""
    from fastapi import HTTPException
    from app.commercial import enforcement

    op, org_id = _customer(db, plan="sovereign", state="active")
    u = models.User(name="U", email=f"c+{org_id[:8]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        # policy_controls has no setting, so it stays CONFIGURATION_REQUIRED
        # regardless of Layer 6 (capture is now ALLOW under default settings).
        enforcement._assert_capability(db, u, "evidence.screenshots.policy_controls",
                                       writable=False)
    assert exc.value.status_code == 409


def test_read_only_passes_a_read_gate_but_fails_a_write_gate(db):
    """The distinction that stops historical access being mistaken for a licence
    to write."""
    from fastapi import HTTPException
    from app.commercial import enforcement

    op, org_id = _customer(db, plan="sovereign", state="suspended")
    u = models.User(name="U", email=f"r+{org_id[:8]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()

    enforcement._assert_capability(db, u, "reports.standard", writable=False)
    with pytest.raises(HTTPException):
        enforcement._assert_capability(db, u, "reports.standard", writable=True)


def test_shadow_mode_logs_instead_of_denying(db, monkeypatch):
    from app.commercial import enforcement

    op, org_id = _customer(db, plan="verified", state="active")
    u = models.User(name="U", email=f"s+{org_id[:8]}@x.test",
                    organization_id=op.id, role="admin")
    db.add(u)
    db.flush()

    monkeypatch.setattr(enforcement, "SHADOW_CAPABILITIES", {"identity.sso"})
    enforcement._assert_capability(db, u, "identity.sso", writable=False)


# Phase 4.2 wired the enforcement gate to these approved safe-first admin
# capabilities. The gate is rollout-controlled (see enforcement._gate_capability),
# so the wired routes remain NON-BLOCKING while enforcement is OFF — nothing 403s
# until the rollout control (ENTITLEMENT_ENFORCEMENT + per-org flag +
# ENFORCE_CAPABILITIES) deliberately opts in. This test pins that no OTHER
# capability has begun wiring.
_APPROVED_WIRED_CAPS = {
    "activity.anomaly", "integrations.api", "payroll.calculator",
    "holidays.multicountry",
}


def test_gate_is_wired_only_to_the_approved_safe_first_caps():
    """Phase 4.2: the enforcement gate is attached to the approved admin routes
    only. Any capability that appears in a route dependency must be one of the
    approved safe-first capabilities — nothing else may begin enforcing."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    pat = re.compile(r'require(?:_writable)?_capability\("([a-z0-9_.]+)"\)')
    wired = set()
    for p in root.rglob("*.py"):
        if "enforcement.py" in str(p):
            continue
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            wired.add(m.group(1))
    assert wired == _APPROVED_WIRED_CAPS, f"unexpected wired capabilities: {wired}"
