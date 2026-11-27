"""Phase 3.6 - actor entitlement projection + read-only inspector.

Presentation is not authorization. These tests pin the two invariants that make
that safe: a decision of DENY can never project as SHOW, and the actor's role can
only ever narrow a LOCK to a HIDE. Everything is read-only; nothing enforces.
"""
import pathlib

import pytest

from app.billing import service as billing_service
from app.commercial import entitlements, ids, manifests, projection
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
FRONTEND_SRC = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"

PRIVACY = sorted(manifests.GLOBALLY_DENIED)
PRESENTATIONS = manifests.PRESENTATIONS


def _tenant(db, *, plan="verified", state="active", country="US",
            billing_type="customer"):
    op = models.Organization(name=f"Proj {ids.org_id()[:6]}", billing_type=billing_type)
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country=country, commercial_state=state,
        operational_org_id=op.id))
    db.flush()
    if plan is not None:
        db.add(CommercialSubscription(
            id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
            billing_cycle="monthly", seat_count=5, state=state, provider="stub"))
        db.flush()
    return op, org_id


def _project(db, org_id, role):
    profile = entitlements.get(db, org_id)
    return projection.build_actor_projection(profile, role=role)


def _by_key(proj):
    return {r["capability"]: r for r in proj["capabilities"]}


# ══════════════════════════════════════════════════════════════════════════════
# 1-2. Completeness across the 38 keys and every plan
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("plan", ["verified", "governed", "sovereign", "enterprise"])
def test_all_39_keys_present_for_every_plan(db, plan):
    _, org_id = _tenant(db, plan=plan)
    rows = _project(db, org_id, "admin")["capabilities"]
    assert {r["capability"] for r in rows} == set(manifests.FEATURES)
    assert len(rows) == 38


@pytest.mark.parametrize("plan", ["verified", "governed", "sovereign", "enterprise"])
def test_every_presentation_is_from_the_vocabulary(db, plan):
    _, org_id = _tenant(db, plan=plan)
    for r in _project(db, org_id, "admin")["capabilities"]:
        assert r["presentation"] in PRESENTATIONS


# ══════════════════════════════════════════════════════════════════════════════
# 3-6. Role-awareness, and the never-broader invariant
# ══════════════════════════════════════════════════════════════════════════════
def test_worker_and_admin_share_decisions_but_differ_in_presentation(db):
    _, org_id = _tenant(db, plan="verified")
    admin = _by_key(_project(db, org_id, "admin"))
    worker = _by_key(_project(db, org_id, "employee"))

    # Same underlying decisions...
    assert ({k: v["decision"] for k, v in admin.items()}
            == {k: v["decision"] for k, v in worker.items()})
    # ...but the presentation differs on at least the upsellable denies.
    assert any(admin[k]["presentation"] != worker[k]["presentation"] for k in admin)


def test_worker_never_receives_lock(db):
    for plan in ("verified", "governed", "sovereign"):
        _, org_id = _tenant(db, plan=plan)
        for r in _project(db, org_id, "employee")["capabilities"]:
            assert r["presentation"] != manifests.LOCK, (plan, r["capability"])


def test_admin_receives_lock_where_a_capability_is_sold_higher(db):
    _, org_id = _tenant(db, plan="verified")
    admin = _by_key(_project(db, org_id, "admin"))
    # integrations.api is denied on Verified but sold on Governed+ → LOCK.
    assert admin["integrations.api"]["decision"] == manifests.DENY
    assert admin["integrations.api"]["presentation"] == manifests.LOCK


@pytest.mark.parametrize("role", ["admin", "employee", "owner", "hr", "super_admin",
                                  None, "manager"])
def test_presentation_never_broader_than_decision(db, role):
    """The core safety property: only an ALLOW may present as SHOW."""
    for plan in ("verified", "governed", "sovereign", "enterprise"):
        _, org_id = _tenant(db, plan=plan)
        for r in _project(db, org_id, role)["capabilities"]:
            if r["presentation"] == manifests.SHOW:
                assert r["decision"] == manifests.ALLOW, (plan, role, r)
            # A denied capability is never SHOW.
            if r["decision"] == manifests.DENY:
                assert r["presentation"] in (manifests.LOCK, manifests.HIDE), r


def test_worker_projection_is_a_subset_of_admin_visibility(db):
    """A worker can only ever see less, never more, than an admin."""
    _, org_id = _tenant(db, plan="governed")
    admin = _by_key(_project(db, org_id, "admin"))
    worker = _by_key(_project(db, org_id, "employee"))
    rank = {manifests.HIDE: 0, manifests.LOCK: 1, manifests.CONFIGURATION_REQUIRED: 2,
            manifests.READ_ONLY: 2, manifests.SHOW: 3}
    for k in admin:
        assert rank[worker[k]["presentation"]] <= rank[admin[k]["presentation"]], k


# ══════════════════════════════════════════════════════════════════════════════
# 7-8. READ_ONLY and CONFIGURATION_REQUIRED
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state", ["suspended", "cancelled"])
def test_historical_capabilities_present_read_only_when_impaired(db, state):
    _, org_id = _tenant(db, plan="governed", state=state)
    rows = _by_key(_project(db, org_id, "admin"))
    r = rows["reports.standard"]
    assert r["decision"] == manifests.READ_ONLY
    assert r["presentation"] == manifests.READ_ONLY


@pytest.mark.parametrize("state", ["suspended", "cancelled"])
def test_read_only_is_role_invariant(db, state):
    """READ_ONLY reflects the decision, not the actor - a worker sees it too."""
    _, org_id = _tenant(db, plan="governed", state=state)
    admin = _by_key(_project(db, org_id, "admin"))
    worker = _by_key(_project(db, org_id, "employee"))
    assert admin["reports.standard"]["presentation"] == manifests.READ_ONLY
    assert worker["reports.standard"]["presentation"] == manifests.READ_ONLY


@pytest.mark.parametrize("key", ["evidence.screenshots.masking",
                                 "evidence.screenshots.policy_controls"])
def test_configuration_required_presents_as_setup_not_upsell(db, key):
    # capture is excluded: under DEFAULT settings Layer 6 makes it ALLOW/SHOW.
    # masking (blur off) and policy_controls (no setting) stay CONFIGURATION_REQUIRED.
    _, org_id = _tenant(db, plan="sovereign")
    r = _by_key(_project(db, org_id, "admin"))[key]
    assert r["decision"] == manifests.CONFIGURATION_REQUIRED
    assert r["presentation"] == manifests.CONFIGURATION_REQUIRED
    assert r["presentation"] != manifests.LOCK          # never an upsell
    assert r["reason"] == "requires_configuration"


# ══════════════════════════════════════════════════════════════════════════════
# 9-11. Privacy, internal tenant, manual review
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("key", PRIVACY)
@pytest.mark.parametrize("role", ["admin", "employee"])
def test_privacy_capabilities_always_hide(db, key, role):
    for plan in ("verified", "governed", "sovereign", "enterprise"):
        _, org_id = _tenant(db, plan=plan)
        r = _by_key(_project(db, org_id, role))[key]
        assert r["decision"] == manifests.DENY
        assert r["presentation"] == manifests.HIDE       # never LOCK, even for admin
        assert r["reason"] == "globally_denied"


def test_internal_tenant_projects_sovereign_equivalent(db):
    op = models.Organization(name="Temveris 36", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    identity = (db.query(CommercialOrganization)
                  .filter_by(operational_org_id=op.id).one())
    rows = _by_key(_project(db, identity.id, "admin"))
    sovereign = {**manifests.capabilities_for("sovereign"),
                 "evidence.screenshots.capture": manifests.ALLOW}  # Layer 6 default
    for key, r in rows.items():
        assert r["decision"] == sovereign[key], key
    for key in PRIVACY:
        assert rows[key]["presentation"] == manifests.HIDE


def test_manual_review_essentials_present_but_others_do_not(db):
    op = models.Organization(name="MR proj", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country="US", commercial_state="manual_review",
        operational_org_id=op.id))      # no subscription -> _empty_profile
    db.flush()

    rows = _by_key(_project(db, org_id, "admin"))
    assert rows["billing.portal"]["decision"] == manifests.ALLOW
    assert rows["billing.portal"]["presentation"] == manifests.SHOW
    assert rows["identity.mfa"]["decision"] == manifests.ALLOW
    assert rows["identity.mfa"]["presentation"] == manifests.SHOW
    # identity.sso is now an all-plan essential, so it IS granted here.
    assert rows["identity.sso"]["decision"] == manifests.ALLOW
    assert rows["identity.sso"]["presentation"] == manifests.SHOW
    # A premium operational capability stays unavailable.
    assert rows["payroll.calculator"]["decision"] == manifests.DENY
    assert rows["payroll.calculator"]["presentation"] != manifests.SHOW


# ══════════════════════════════════════════════════════════════════════════════
# 12-16. Read-only, no side effects, cache, fail-closed
# ══════════════════════════════════════════════════════════════════════════════
def test_projection_writes_nothing(db):
    from app.commercial.models import EntitlementsCache
    _, org_id = _tenant(db, plan="governed")
    entitlements.get(db, org_id)                 # warm the cache once
    before = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    before_hash = (before.profile_json or {}).get("snapshot_hash")
    before_exp = before.expires_at

    # Projection reads a profile and derives presentation; it must not recompute
    # or rewrite the cache row.
    profile = entitlements.get(db, org_id)
    for role in ("admin", "employee", "owner", None):
        projection.build_actor_projection(profile, role=role)

    after = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    assert (after.profile_json or {}).get("snapshot_hash") == before_hash
    assert after.expires_at == before_exp


def test_actor_presentation_never_leaks_between_users(db):
    """Same org, two actors: decisions identical, presentation role-correct.

    The projection is derived per call from role and never persisted, so a
    worker call cannot poison an admin call or vice versa.
    """
    _, org_id = _tenant(db, plan="verified")
    worker_first = _by_key(projection.build_actor_projection(
        entitlements.get(db, org_id), role="employee"))
    admin_after = _by_key(projection.build_actor_projection(
        entitlements.get(db, org_id), role="admin"))

    assert worker_first["integrations.api"]["presentation"] == manifests.HIDE
    assert admin_after["integrations.api"]["presentation"] == manifests.LOCK
    # decisions are identical regardless of who asked
    assert (worker_first["integrations.api"]["decision"]
            == admin_after["integrations.api"]["decision"] == manifests.DENY)


def test_unresolvable_org_fails_closed(db):
    """An unresolvable tenant is entitled to nothing beyond its essentials, so
    nothing else may present as SHOW. For an admin the upsellable denies show as
    LOCK (a read-only upgrade prompt, not access); privacy keys always hide."""
    empty = entitlements._empty_profile("nope", plan="verified", state="manual_review")
    rows = _by_key(projection.build_actor_projection(empty, role="admin"))

    # Only the essentials the empty profile grants are SHOW.
    shown = {k for k, r in rows.items() if r["presentation"] == manifests.SHOW}
    assert shown == {"billing.portal", "identity.mfa", "identity.sso"}
    # No denied capability presents as SHOW.
    for r in rows.values():
        if r["decision"] == manifests.DENY:
            assert r["presentation"] != manifests.SHOW
    # Privacy always hides, even for an admin.
    for key in PRIVACY:
        assert rows[key]["presentation"] == manifests.HIDE


def test_unresolvable_org_hides_everything_for_a_worker(db):
    """A worker of an unresolvable tenant sees no upsell at all."""
    empty = entitlements._empty_profile("nope", plan="verified", state="manual_review")
    rows = _by_key(projection.build_actor_projection(empty, role="employee"))
    shown = {k for k, r in rows.items() if r["presentation"] == manifests.SHOW}
    assert shown == {"billing.portal", "identity.mfa", "identity.sso"}
    assert not any(r["presentation"] == manifests.LOCK for r in rows.values())


def test_missing_capability_in_profile_is_treated_as_deny(db):
    """A malformed profile must never invent access."""
    partial = {"tenant_id": "t", "plan": "verified", "commercial_state": "active",
               "capabilities": {"reports.standard": manifests.ALLOW}}   # only one key
    rows = _by_key(projection.build_actor_projection(partial, role="admin"))
    assert len(rows) == 38
    assert rows["reports.standard"]["presentation"] == manifests.SHOW
    assert rows["payroll.calculator"]["decision"] == manifests.DENY


# ══════════════════════════════════════════════════════════════════════════════
# Decision trace honesty (§19)
# ══════════════════════════════════════════════════════════════════════════════
def test_decision_trace_is_honest_about_unbuilt_layers():
    trace = {t["layer"]: t for t in projection.decision_trace()}
    assert trace["workspace_configuration"]["status"] == "not_evaluated"
    assert trace["actor_rbac"]["status"] == "separate_concern"
    # the layers that ARE evaluated
    for layer in ("global", "plan", "enterprise_overlay", "jurisdiction", "lifecycle"):
        assert trace[layer]["status"] == "evaluated"


# ══════════════════════════════════════════════════════════════════════════════
# 17-20. Scope guards — nothing enforced, nothing changed
# ══════════════════════════════════════════════════════════════════════════════
def test_require_capability_wired_only_to_approved_caps_and_off():
    """Phase 4.2 wired the rollout-gated require_capability to the approved
    safe-first admin capabilities; it stays non-blocking while enforcement is
    OFF. Pins that no other capability began wiring and enforcement is still
    globally OFF."""
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
    from app.commercial import enforcement
    assert enforcement.enforcement_mode() == enforcement.MODE_OFF


def test_frontend_capability_gating_remains_zero():
    if not FRONTEND_SRC.exists():
        pytest.skip("frontend not present")
    hits = [
        f"{p.relative_to(FRONTEND_SRC)}:{n}"
        for p in FRONTEND_SRC.rglob("*.jsx")
        for n, line in enumerate(p.read_text(encoding="utf-8",
                                             errors="ignore").splitlines(), 1)
        if "capabilities[" in line or "require_capability" in line
    ]
    assert not hits, hits


def test_shadow_hooks_remain_observation_only():
    import ast
    total = consumed = 0
    for p in APP.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        src = p.read_text(encoding="utf-8")
        if "_shadow(" not in src:
            continue
        tree = ast.parse(src)
        bare = {id(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_shadow":
                total += 1
                consumed += id(n) not in bare
    assert total == 16, total          # Phase 4.3 consolidated 3 anchor hooks into the gate
    assert consumed == 0


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


def test_trial_pricing_seats_untouched():
    from app.billing.trial import TRIAL_LENGTH_DAYS
    from app.commercial.entitlements import _PLAN_LIMITS
    assert TRIAL_LENGTH_DAYS == 14
    assert _PLAN_LIMITS["verified"]["seat_limit"] == 25


def test_internal_tenant_still_billing_inert(db):
    op = models.Organization(name="Inert 36", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    assert billing_service.is_internal_tenant(db, op.id) is True
    with pytest.raises(Exception):
        billing_service.require_billable_tenant(db, op.id)


# ══════════════════════════════════════════════════════════════════════════════
# HTTP handlers end-to-end (called directly, so the import/wiring is exercised)
# ══════════════════════════════════════════════════════════════════════════════
def test_projection_endpoint_returns_39_rows(db):
    from app.commercial.routers import entitlements as ent_router
    op, org_id = _tenant(db, plan="governed")
    user = models.User(name="A", email=f"proj-ep-{org_id[:6]}@x.test",
                       organization_id=op.id, role="admin")
    db.add(user)
    db.flush()
    resp = ent_router.get_actor_projection(db=db, current_user=user)
    assert len(resp["capabilities"]) == 38
    assert resp["actor_role"] == "admin"
    assert resp["actor_is_worker"] is False


def test_inspector_endpoint_is_super_admin_only(db):
    from fastapi import HTTPException
    from app.commercial.routers import admin as admin_router
    op, org_id = _tenant(db, plan="governed")
    tenant_admin = models.User(name="TA", email=f"insp-{org_id[:6]}@x.test",
                               organization_id=op.id, role="admin")
    db.add(tenant_admin)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        admin_router.entitlement_inspector(org_id, db=db, current_user=tenant_admin)
    assert exc.value.status_code == 403


def test_inspector_endpoint_returns_trace_and_caps(db):
    from app.commercial.routers import admin as admin_router
    op, org_id = _tenant(db, plan="sovereign")
    su = models.User(name="SU", email=f"su-{org_id[:6]}@x.test",
                     organization_id=op.id, role="super_admin")
    db.add(su)
    db.flush()
    resp = admin_router.entitlement_inspector(org_id, db=db, current_user=su)
    assert resp["plan"] == "sovereign"
    assert len(resp["capabilities"]) == 38
    layers = {t["layer"]: t["status"] for t in resp["decision_trace"]}
    assert layers["workspace_configuration"] == "not_evaluated"
    assert resp["notes"]["read_only"] is True


# ══════════════════════════════════════════════════════════════════════════════
# is_upsellable / presentation_for units
# ══════════════════════════════════════════════════════════════════════════════
def test_is_upsellable_hides_the_never_granted_keys():
    never = {"evidence.screenshots.jurisdiction", "governance.ediscovery",
             "integrations.webhooks", "leave.jurisdiction",
             *manifests.GLOBALLY_DENIED}
    for key in never:
        assert manifests.is_upsellable(key) is False, key
    # a genuinely tiered key IS upsellable
    assert manifests.is_upsellable("integrations.api") is True


def test_presentation_for_unknown_key_is_hide_not_show():
    assert manifests.presentation_for(manifests.DENY, "not.a.key", is_worker=False) == manifests.HIDE
    assert manifests.presentation_for(manifests.ALLOW, "not.a.key", is_worker=False) == manifests.SHOW
