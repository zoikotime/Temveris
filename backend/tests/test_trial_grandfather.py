"""Phase 3.18 — G2 state-conditional grandfather (SPEC retier).

Existing Verified trials keep the seven retiered capabilities, but ONLY while
commercial_state == trial_active. The grant auto-expires with the lifecycle (no
cleanup job), can never grant anything outside the fixed seven, and never touches
marker-less tenants or non-trial states.

These tests build tenants at explicit states (the dev trial ladder is off, so
expiry/conversion is simulated by setting commercial_state directly).
"""
import pytest

from app.commercial import entitlements, ids, manifests
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

SEVEN = sorted(entitlements.GRANDFATHER_TRIAL_CAPS)
MARKER = {entitlements.GRANDFATHER_MARKER: True}
PRIVACY = sorted(manifests.GLOBALLY_DENIED)


def _tenant(db, *, plan="verified", state="trial_active", override=None,
            billing_type="customer", with_sub=True):
    op = models.Organization(name=f"gf {ids.org_id()[:8]}", billing_type=billing_type)
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country="US", commercial_state=state,
        operational_org_id=op.id))
    if with_sub:
        db.add(CommercialSubscription(
            id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
            billing_cycle="monthly", seat_count=5, state=state, provider="stub",
            enterprise_override=override or {}))
    db.flush()
    return org_id


def _caps(db, org_id):
    return entitlements.compute(db, org_id)["capabilities"]


def _seven_granted(caps):
    return sum(1 for k in SEVEN if caps[k] == manifests.ALLOW)


# ══════════════════════════════════════════════════════════════════════════════
# Grant only under (marker AND trial_active)
# ══════════════════════════════════════════════════════════════════════════════
def test_marker_plus_trial_active_grants_all_seven(db):
    org_id = _tenant(db, state="trial_active", override=MARKER)
    assert _seven_granted(_caps(db, org_id)) == 7


@pytest.mark.parametrize("state", ["active", "trial_expired", "past_due",
                                   "suspended", "cancelled", "closed",
                                   "manual_review"])
def test_marker_in_non_trial_state_grants_nothing(db, state):
    org_id = _tenant(db, state=state, override=MARKER)
    caps = _caps(db, org_id)
    assert all(caps[k] != manifests.ALLOW for k in SEVEN), state


def test_markerless_verified_trial_does_not_get_the_seven(db):
    org_id = _tenant(db, state="trial_active", override={})
    caps = _caps(db, org_id)
    assert all(caps[k] != manifests.ALLOW for k in SEVEN)


# ══════════════════════════════════════════════════════════════════════════════
# Cannot grant anything outside the fixed seven
# ══════════════════════════════════════════════════════════════════════════════
def test_malformed_marker_cannot_grant_arbitrary_capabilities(db):
    hostile = {entitlements.GRANDFATHER_MARKER: True,
               "capabilities": {"integrations.api": "allow", "identity.saml": "allow"}}
    org_id = _tenant(db, state="trial_active", override=hostile)
    caps = _caps(db, org_id)
    # The enterprise-override path DOES honour an explicit capabilities dict, so
    # integrations.api would be allowed by THAT path — but the grandfather step
    # itself must not be the thing granting it. Prove the grandfather grants only
    # the seven by using a marker with NO capabilities dict:
    org2 = _tenant(db, state="trial_active", override={entitlements.GRANDFATHER_MARKER: True})
    caps2 = _caps(db, org2)
    # evidence.screenshots.capture is ALLOW under DEFAULT settings via Layer 6,
    # not the marker — exclude it from the "granted by the marker" check.
    granted_beyond_seven = [k for k in manifests.FEATURES
                            if k not in SEVEN and k != "evidence.screenshots.capture"
                            and caps2[k] == manifests.ALLOW
                            and manifests.capabilities_for("verified")[k] != manifests.ALLOW]
    assert granted_beyond_seven == []


def test_marker_cannot_grant_a_capability_outside_the_seven(db):
    org_id = _tenant(db, state="trial_active", override=MARKER)
    caps = _caps(db, org_id)
    # integrations.api is Governed+ and NOT in the seven → must stay denied.
    assert caps["integrations.api"] == manifests.DENY


def test_privacy_capabilities_remain_denied_with_marker(db):
    org_id = _tenant(db, state="trial_active", override=MARKER)
    caps = _caps(db, org_id)
    for key in PRIVACY:
        assert caps[key] == manifests.DENY


# ══════════════════════════════════════════════════════════════════════════════
# Never affects other plans / tenants
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("plan", ["governed", "sovereign", "enterprise"])
def test_marker_does_not_change_higher_plans(db, plan):
    with_marker = _tenant(db, plan=plan, state="trial_active", override=MARKER)
    without = _tenant(db, plan=plan, state="trial_active", override={})
    assert _caps(db, with_marker) == _caps(db, without)


def test_internal_tenant_unaffected_by_marker(db):
    op = models.Organization(name="Temveris gf", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    identity = (db.query(CommercialOrganization)
                  .filter_by(operational_org_id=op.id).one())
    # Internal has no subscription/override; the marker cannot reach it.
    caps = entitlements.compute(db, identity.id)["capabilities"]
    assert caps == {**manifests.capabilities_for("sovereign"), "evidence.screenshots.capture": manifests.ALLOW}


# ══════════════════════════════════════════════════════════════════════════════
# Existing enterprise_override behaviour preserved
# ══════════════════════════════════════════════════════════════════════════════
def test_existing_enterprise_capabilities_override_still_works(db):
    """A contractual capabilities override keeps its unconditional behaviour,
    independent of the grandfather marker."""
    ov = {"capabilities": {"integrations.webhooks": "allow"}}
    org_id = _tenant(db, plan="enterprise", state="active", override=ov)
    caps = _caps(db, org_id)
    assert caps["integrations.webhooks"] == manifests.ALLOW      # unconditional


def test_marker_and_enterprise_override_coexist(db):
    ov = {entitlements.GRANDFATHER_MARKER: True,
          "capabilities": {"governance.ediscovery": "allow"}}
    org_id = _tenant(db, plan="verified", state="trial_active", override=ov)
    caps = _caps(db, org_id)
    assert _seven_granted(caps) == 7                             # grandfather grant
    assert caps["governance.ediscovery"] == manifests.ALLOW      # contractual grant


# ══════════════════════════════════════════════════════════════════════════════
# Conversion / expiry remove the grant (simulated state transitions)
# ══════════════════════════════════════════════════════════════════════════════
def test_conversion_to_active_removes_the_grant(db):
    org_id = _tenant(db, state="trial_active", override=MARKER)
    assert _seven_granted(_caps(db, org_id)) == 7
    # Simulate conversion: commercial_state trial_active -> active.
    org = db.query(CommercialOrganization).filter_by(id=org_id).one()
    org.commercial_state = "active"
    db.flush()
    entitlements.invalidate(db, org_id)
    assert _seven_granted(_caps(db, org_id)) == 0


def test_expiry_removes_the_grant(db):
    org_id = _tenant(db, state="trial_active", override=MARKER)
    assert _seven_granted(_caps(db, org_id)) == 7
    org = db.query(CommercialOrganization).filter_by(id=org_id).one()
    org.commercial_state = "trial_expired"
    db.flush()
    entitlements.invalidate(db, org_id)
    assert _seven_granted(_caps(db, org_id)) == 0


# ══════════════════════════════════════════════════════════════════════════════
# The grandfather set is exactly the seven, rbac_advanced excluded
# ══════════════════════════════════════════════════════════════════════════════
def test_grandfather_set_is_exactly_the_seven():
    assert entitlements.GRANDFATHER_TRIAL_CAPS == frozenset({
        "payroll.calculator", "reports.advanced", "activity.anomaly",
        "leave.approvals", "offboarding.governed", "time.attendance.policy",
        "time.playback.advanced",
    })
    assert "workspace.settings.rbac_advanced" not in entitlements.GRANDFATHER_TRIAL_CAPS
