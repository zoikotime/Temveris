"""Phase 3.19 — a manifest version bump makes stale caches self-invalidate.

Phase 3.18 changed verified/governed grants in place while keeping the version
string, so warm cache rows could serve the old capability map for a whole TTL.
This phase bumped verified/governed to _2026_09. These tests pin the guarantee:
an old-version cache row is treated as not-current even while fresh by TTL, so
the next read recomputes to the new policy — no explicit invalidation needed.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.commercial import entitlements, ids, manifests
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, EntitlementsCache,
)
from database import models


def _tenant(db, *, plan="verified", state="active"):
    op = models.Organization(name=f"mv {ids.org_id()[:8]}", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country="US", commercial_state=state,
        operational_org_id=op.id))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
        billing_cycle="monthly", seat_count=5, state=state, provider="stub"))
    db.flush()
    return org_id


def _seed_old_version_row(db, org_id, *, plan="verified", old_version="verified_2026_08"):
    """A cache row from the previous packaging: FRESH by TTL, wrong decisions."""
    stale = {
        "tenant_id": org_id, "plan": plan, "commercial_state": "active",
        "manifest_version": old_version, "snapshot_hash": "old-hash-sentinel",
        "capabilities": {k: manifests.DENY for k in manifests.FEATURES},
    }
    row = EntitlementsCache(
        organization_id=org_id,
        computed_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),  # deliberately fresh
        profile_json=stale, plan_code=plan, commercial_state="active")
    db.add(row)
    db.flush()
    return row


# ══════════════════════════════════════════════════════════════════════════════
# The version is the one that changed
# ══════════════════════════════════════════════════════════════════════════════
def test_verified_and_governed_carry_a_new_version():
    assert manifests.CURRENT_VERSIONS["verified"] == "verified_2026_09"
    assert manifests.CURRENT_VERSIONS["governed"] == "governed_2026_09"


def test_unchanged_plans_keep_the_old_version():
    assert manifests.CURRENT_VERSIONS["sovereign"] == "sovereign_2026_08"
    assert manifests.CURRENT_VERSIONS["enterprise"] == "enterprise_2026_08"
    assert manifests.CURRENT_VERSIONS[manifests.INTERNAL_PLAN] == "internal_2026_08"


# ══════════════════════════════════════════════════════════════════════════════
# Old cache rows are stale even while fresh by TTL
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("plan,old", [("verified", "verified_2026_08"),
                                      ("governed", "governed_2026_08")])
def test_old_version_row_is_not_current(db, plan, old):
    org_id = _tenant(db, plan=plan)
    row = _seed_old_version_row(db, org_id, plan=plan, old_version=old)
    assert entitlements._is_fresh(row.expires_at) is True          # fresh by TTL
    assert entitlements._manifest_is_current(row) is False         # but not current


@pytest.mark.parametrize("plan", ["verified", "governed"])
def test_get_recomputes_instead_of_serving_the_stale_row(db, plan):
    org_id = _tenant(db, plan=plan)
    _seed_old_version_row(db, org_id, plan=plan,
                          old_version=f"{plan}_2026_08")
    result = entitlements.get(db, org_id)
    # Recomputed to the new version and the new policy (SSO is now all-plan).
    assert result["manifest_version"] == manifests.CURRENT_VERSIONS[plan]
    assert result["capabilities"]["identity.sso"] == manifests.ALLOW


def test_sovereign_old_row_stays_current(db):
    """Sovereign did not change, so a _2026_08 sovereign row is still current."""
    org_id = _tenant(db, plan="sovereign")
    row = _seed_old_version_row(db, org_id, plan="sovereign",
                                old_version="sovereign_2026_08")
    assert entitlements._manifest_is_current(row) is True


# ══════════════════════════════════════════════════════════════════════════════
# Snapshot hash still deterministic; the version participates in it
# ══════════════════════════════════════════════════════════════════════════════
def test_manifest_version_is_a_snapshot_hash_input():
    base = dict(plan_code="verified", state="active", country="US",
                email_verified=True, enterprise_override=None,
                manifest_version="verified_2026_08")
    assert entitlements._snapshot_hash(**base) != \
        entitlements._snapshot_hash(**{**base, "manifest_version": "verified_2026_09"})


def test_identical_recompute_is_stable_after_the_bump(db):
    org_id = _tenant(db, plan="verified")
    h1 = entitlements.compute(db, org_id)["snapshot_hash"]
    h2 = entitlements.compute(db, org_id)["snapshot_hash"]
    assert h1 == h2                                                # no churn


# ══════════════════════════════════════════════════════════════════════════════
# Decisions unchanged from Phase 3.18 (the version bump moves no grant)
# ══════════════════════════════════════════════════════════════════════════════
def test_decisions_are_exactly_the_phase_3_18_policy():
    v = manifests.capabilities_for("verified")
    g = manifests.capabilities_for("governed")
    s = manifests.capabilities_for("sovereign")
    # SSO all plans
    for plan in manifests.PLANS:
        assert manifests.capabilities_for(plan)["identity.sso"] == manifests.ALLOW
    # seven Governed+
    for key in ("payroll.calculator", "reports.advanced", "activity.anomaly",
                "leave.approvals", "offboarding.governed", "time.attendance.policy",
                "time.playback.advanced"):
        assert v[key] == manifests.DENY and g[key] == manifests.ALLOW, key
    # compliance / custody Sovereign+
    for key in ("reports.compliance", "governance.chain_of_custody"):
        assert g[key] == manifests.DENY and s[key] == manifests.ALLOW, key
    # rbac_advanced all plans; privacy denied; internal == sovereign
    for plan in manifests.PLANS:
        assert manifests.capabilities_for(plan)["workspace.settings.rbac_advanced"] == manifests.ALLOW
        assert all(manifests.capabilities_for(plan)[k] == manifests.DENY
                   for k in manifests.GLOBALLY_DENIED)
    assert manifests.capabilities_for("internal") == s
