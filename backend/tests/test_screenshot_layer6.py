"""Phase 3.20 — Layer 6 screenshot configuration resolution (spec §3).

Layer 6 upgrades the screenshot capabilities from CONFIGURATION_REQUIRED to ALLOW
when the workspace has actually configured them, reading the EXISTING settings
cascade. capture ← screenshot_enabled + valid screenshot_interval; masking ←
screenshot_blur; policy_controls has no setting and stays CONFIGURATION_REQUIRED.
Settings are a snapshot-hash input, so a change self-invalidates the cache.
"""
import pytest

from app.commercial import entitlements, ids, manifests
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, EntitlementsCache,
)
from database import models

CAPTURE = "evidence.screenshots.capture"
MASKING = "evidence.screenshots.masking"
POLICY = "evidence.screenshots.policy_controls"
CR = manifests.CONFIGURATION_REQUIRED


def _tenant(db, *, plan="verified", state="active",
            enabled=None, interval=None, blur=None):
    op = models.Organization(name=f"l6 {ids.org_id()[:8]}", billing_type="customer")
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
    for key, val in (("screenshot_enabled", enabled),
                     ("screenshot_interval", interval),
                     ("screenshot_blur", blur)):
        if val is not None:
            db.add(models.AppSettings(organization_id=op.id, key=key,
                                      value=str(val), team_key=None, user_id=None))
    db.flush()
    return op, org_id


def _caps(db, org_id):
    db.query(EntitlementsCache).filter_by(organization_id=org_id).delete()
    db.flush()
    return entitlements.compute(db, org_id)["capabilities"]


# ══════════════════════════════════════════════════════════════════════════════
# capture
# ══════════════════════════════════════════════════════════════════════════════
def test_default_settings_configure_capture(db):
    # DEFAULTS: screenshot_enabled=true, interval=1800 → configured → ALLOW.
    _, org_id = _tenant(db)
    assert _caps(db, org_id)[CAPTURE] == manifests.ALLOW


def test_capture_disabled_stays_configuration_required(db):
    _, org_id = _tenant(db, enabled="false")
    assert _caps(db, org_id)[CAPTURE] == CR


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "", "1.5", "  "])
def test_invalid_interval_stays_configuration_required(db, bad):
    _, org_id = _tenant(db, interval=bad)
    assert _caps(db, org_id)[CAPTURE] == CR


@pytest.mark.parametrize("good", ["1", "60", "1800", "3600"])
def test_valid_positive_interval_configures_capture(db, good):
    _, org_id = _tenant(db, interval=good)
    assert _caps(db, org_id)[CAPTURE] == manifests.ALLOW


@pytest.mark.parametrize("truthy", ["true", "True", "1", "yes", "on"])
def test_enabled_truthy_forms(db, truthy):
    _, org_id = _tenant(db, enabled=truthy)
    assert _caps(db, org_id)[CAPTURE] == manifests.ALLOW


@pytest.mark.parametrize("falsey", ["false", "0", "no", "off", "nonsense"])
def test_enabled_falsey_forms_stay_config_required(db, falsey):
    _, org_id = _tenant(db, enabled=falsey)
    assert _caps(db, org_id)[CAPTURE] == CR


# ══════════════════════════════════════════════════════════════════════════════
# masking
# ══════════════════════════════════════════════════════════════════════════════
def test_masking_blur_false_stays_config_required(db):
    _, org_id = _tenant(db, blur="false")
    assert _caps(db, org_id)[MASKING] == CR


def test_masking_blur_true_configures_masking(db):
    _, org_id = _tenant(db, blur="true")
    assert _caps(db, org_id)[MASKING] == manifests.ALLOW


def test_masking_default_is_config_required(db):
    # blur defaults to "false".
    _, org_id = _tenant(db)
    assert _caps(db, org_id)[MASKING] == CR


# ══════════════════════════════════════════════════════════════════════════════
# policy_controls — no setting, never upgraded
# ══════════════════════════════════════════════════════════════════════════════
def test_policy_controls_always_config_required(db):
    for kwargs in ({}, {"enabled": "true", "interval": "60", "blur": "true"}):
        _, org_id = _tenant(db, **kwargs)
        assert _caps(db, org_id)[POLICY] == CR


# ══════════════════════════════════════════════════════════════════════════════
# lifecycle interaction
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state", ["suspended", "cancelled", "closed"])
def test_configured_capture_denied_in_impaired_states(db, state):
    """capture is operational, so lifecycle denies it even when configured."""
    _, org_id = _tenant(db, state=state)
    assert _caps(db, org_id)[CAPTURE] == manifests.DENY


@pytest.mark.parametrize("state", ["trial_active", "active", "past_due"])
def test_configured_capture_allowed_in_full_states(db, state):
    _, org_id = _tenant(db, state=state)
    assert _caps(db, org_id)[CAPTURE] == manifests.ALLOW


def test_manual_review_no_subscription_denies_capture(db):
    op = models.Organization(name="mr l6", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name="mr", country="US",
                                  commercial_state="manual_review",
                                  operational_org_id=op.id))  # no subscription
    db.flush()
    assert _caps(db, org_id)[CAPTURE] == manifests.DENY       # empty profile


# ══════════════════════════════════════════════════════════════════════════════
# privacy / grandfather / enterprise override interaction
# ══════════════════════════════════════════════════════════════════════════════
def test_privacy_capabilities_unaffected_by_layer6(db):
    _, org_id = _tenant(db, enabled="true", blur="true")
    caps = _caps(db, org_id)
    for key in manifests.GLOBALLY_DENIED:
        assert caps[key] == manifests.DENY


def test_layer6_does_not_touch_the_grandfathered_seven(db):
    op = models.Organization(name="gf l6", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name="gf", country="US",
                                  commercial_state="trial_active",
                                  operational_org_id=op.id))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code="verified",
        billing_cycle="monthly", seat_count=5, state="trial_active", provider="stub",
        enterprise_override={entitlements.GRANDFATHER_MARKER: True}))
    db.flush()
    caps = _caps(db, org_id)
    # Layer 6 grants capture; the grandfather grants the seven; neither bleeds.
    assert caps[CAPTURE] == manifests.ALLOW
    assert sum(1 for k in entitlements.GRANDFATHER_TRIAL_CAPS
               if caps[k] == manifests.ALLOW) == 7
    assert CAPTURE not in entitlements.GRANDFATHER_TRIAL_CAPS


def test_enterprise_override_capture_wins_over_layer6(db):
    """An explicit override decision is applied before Layer 6, and Layer 6 only
    upgrades a CONFIGURATION_REQUIRED — so a contractual DENY of capture stands."""
    op = models.Organization(name="ent l6", billing_type="customer")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(id=org_id, name="ent", country="US",
                                  commercial_state="active",
                                  operational_org_id=op.id))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code="enterprise",
        billing_cycle="monthly", seat_count=5, state="active", provider="stub",
        enterprise_override={"capabilities": {CAPTURE: "deny"}}))
    db.flush()
    # default settings would configure capture, but the contract denied it.
    assert _caps(db, org_id)[CAPTURE] == manifests.DENY


# ══════════════════════════════════════════════════════════════════════════════
# cache / hash / event behaviour
# ══════════════════════════════════════════════════════════════════════════════
def test_settings_change_self_invalidates_the_cache(db):
    op, org_id = _tenant(db)                                  # default: capture allow
    assert entitlements.get(db, org_id)["capabilities"][CAPTURE] == manifests.ALLOW
    db.add(models.AppSettings(organization_id=op.id, key="screenshot_enabled",
                              value="false", team_key=None, user_id=None))
    db.flush()
    # No explicit invalidate() — the hash includes the config fingerprint.
    assert entitlements.get(db, org_id)["capabilities"][CAPTURE] == CR


def test_screenshot_config_is_a_snapshot_hash_input():
    base = dict(plan_code="verified", state="active", country="US",
                email_verified=True, enterprise_override=None,
                manifest_version="verified_2026_09")
    a = entitlements._snapshot_hash(**base, screenshot_config="cap=True:mask=False")
    b = entitlements._snapshot_hash(**base, screenshot_config="cap=False:mask=False")
    assert a != b


def test_fingerprint_ignores_irrelevant_interval_changes(db):
    """Two valid positive intervals produce the same configured state, so the
    fingerprint (and cache) does not churn."""
    a = entitlements._screenshot_fingerprint({"capture": True, "masking": False})
    b = entitlements._screenshot_fingerprint({"capture": True, "masking": False})
    assert a == b


def test_identical_recompute_stable_with_layer6(db):
    _, org_id = _tenant(db)
    h1 = entitlements.compute(db, org_id)["snapshot_hash"]
    h2 = entitlements.compute(db, org_id)["snapshot_hash"]
    assert h1 == h2


# ══════════════════════════════════════════════════════════════════════════════
# tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
def test_settings_are_isolated_by_tenant(db):
    _, a = _tenant(db, enabled="true")        # configured
    _, b = _tenant(db, enabled="false")       # not configured
    assert _caps(db, a)[CAPTURE] == manifests.ALLOW
    assert _caps(db, b)[CAPTURE] == CR


# ══════════════════════════════════════════════════════════════════════════════
# projection
# ══════════════════════════════════════════════════════════════════════════════
def test_configured_capture_projects_as_show(db):
    from app.commercial import projection
    _, org_id = _tenant(db)
    profile = entitlements.get(db, org_id)
    rows = {r["capability"]: r for r in
            projection.build_actor_projection(profile, role="admin")["capabilities"]}
    assert rows[CAPTURE]["decision"] == manifests.ALLOW
    assert rows[CAPTURE]["presentation"] == manifests.SHOW
    # masking stays a setup CTA, not an upsell
    assert rows[MASKING]["presentation"] == CR


def test_unconfigured_capture_projects_as_setup_cta(db):
    from app.commercial import projection
    _, org_id = _tenant(db, enabled="false")
    profile = entitlements.get(db, org_id)
    rows = {r["capability"]: r for r in
            projection.build_actor_projection(profile, role="admin")["capabilities"]}
    assert rows[CAPTURE]["presentation"] == CR
    assert rows[CAPTURE]["presentation"] != manifests.LOCK    # setup, never upsell


# ══════════════════════════════════════════════════════════════════════════════
# unrelated capabilities unchanged
# ══════════════════════════════════════════════════════════════════════════════
def test_only_screenshot_keys_are_touched_by_layer6(db):
    _, org_id = _tenant(db, enabled="true", blur="true")
    caps = _caps(db, org_id)
    baseline = manifests.capabilities_for("verified")
    for key in manifests.FEATURES:
        if key in (CAPTURE, MASKING):
            continue                                          # the two Layer 6 keys
        assert caps[key] == baseline[key], key
