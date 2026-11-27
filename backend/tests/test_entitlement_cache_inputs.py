"""Phase 2.10 - the entitlement cache is now self-validating.

Four defects from the Phase 2.9 audit:

  1. ``email_verified`` was a caller-supplied parameter while the cache was keyed
     on organization_id alone, so one caller could poison the profile for every
     other without anything in the data changing.
  2. An explicit capability override was silently reverted by the derived legacy
     mirror, for exactly the nine capabilities most likely to be negotiated.
  3. A country change altered jurisdiction entitlements without invalidating.
  4. ``manifest_version`` alone no longer identifies a profile, because
     capabilities began honouring per-organization inputs.

Still no enforcement. These tests pin correctness of the cached state only.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.commercial import constants, entitlements, ids, manifests
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, EmailVerificationToken,
    EntitlementsCache,
)
from database import models


def _tenant(db, *, plan="verified", state="active", country="US"):
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name="Acme", country=country, commercial_state=state))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
        billing_cycle="monthly", seat_count=10, state=state, provider="stub",
        trial_started_at=datetime.now(timezone.utc),
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14)))
    db.flush()
    return org_id


def _verify_owner(db, org_id):
    """Give the organization an owner whose email is genuinely verified."""
    user = models.User(name="Owner", email=f"owner+{org_id[:10]}@example.test")
    db.add(user)
    db.flush()
    db.add(EmailVerificationToken(
        user_id=user.id, organization_id=org_id,
        token_hash=f"hash_{org_id[:24]}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        consumed_at=datetime.now(timezone.utc)))
    db.query(CommercialOrganization).filter_by(id=org_id).one().owner_user_id = user.id
    db.flush()
    return user


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 - email verification is authoritative, not caller-supplied
# ══════════════════════════════════════════════════════════════════════════════
def test_one_caller_cannot_poison_the_cache_for_another(db):
    """The Phase 2.9 defect, pinned.

    A background sweep computing with email_verified=False used to cache an
    unverified profile that a verified owner then received for the whole TTL.
    """
    org_id = _tenant(db, plan="governed")
    _verify_owner(db, org_id)

    entitlements.compute(db, org_id, email_verified=False)   # not believed

    assert entitlements.get(db, org_id)["restrictions"]["email_unverified"] is False
    assert entitlements.get(db, org_id)["features"]["data_export"] is True


def test_verified_owner_gets_verified_behaviour(db):
    org_id = _tenant(db, plan="governed")
    _verify_owner(db, org_id)
    assert entitlements.compute(db, org_id)["restrictions"]["email_unverified"] is False


def test_organization_without_a_verified_owner_stays_unverified(db):
    """Whatever the caller claims."""
    org_id = _tenant(db, plan="governed")
    profile = entitlements.compute(db, org_id, email_verified=True)
    assert profile["restrictions"]["email_unverified"] is True
    assert profile["features"]["data_export"] is False


def test_an_owner_with_an_unconsumed_token_is_not_verified(db):
    """Only a *consumed* token counts - matches identity.is_email_verified."""
    org_id = _tenant(db, plan="governed")
    user = models.User(name="Owner", email=f"pending+{org_id[:10]}@example.test")
    db.add(user)
    db.flush()
    db.add(EmailVerificationToken(
        user_id=user.id, organization_id=org_id, token_hash=f"h_{org_id[:24]}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        consumed_at=None))
    db.query(CommercialOrganization).filter_by(id=org_id).one().owner_user_id = user.id
    db.flush()

    assert entitlements.compute(db, org_id)["restrictions"]["email_unverified"] is True


def test_resolution_is_deterministic_regardless_of_caller(db):
    org_id = _tenant(db, plan="governed")
    _verify_owner(db, org_id)
    a = entitlements.compute(db, org_id, email_verified=False)
    entitlements.invalidate(db, org_id)
    b = entitlements.compute(db, org_id, email_verified=True)
    assert a["features"] == b["features"]
    assert a["snapshot_hash"] == b["snapshot_hash"]


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 - an explicit override outranks the derived mirror
# ══════════════════════════════════════════════════════════════════════════════
LEGACY_BACKED = sorted(k for k, f in manifests.FEATURES.items() if f.legacy_key)


def test_the_legacy_backed_set_is_the_nine_from_the_audit():
    assert len(LEGACY_BACKED) == 9


@pytest.mark.parametrize("key", LEGACY_BACKED)
def test_explicit_allow_survives_the_mirror_for_every_legacy_backed_key(key):
    """These nine are exactly the ones the mirror used to silently revert."""
    profile = entitlements._base_profile("verified")
    profile = entitlements._apply_enterprise_overrides(
        profile, {"capabilities": {key: manifests.ALLOW}})
    entitlements._mirror_legacy_restrictions(profile)
    assert profile["capabilities"][key] == manifests.ALLOW


@pytest.mark.parametrize("decision", [
    manifests.ALLOW, manifests.DENY, manifests.READ_ONLY,
    manifests.CONFIGURATION_REQUIRED,
])
def test_every_explicit_decision_is_preserved(decision):
    profile = entitlements._base_profile("verified")
    profile = entitlements._apply_enterprise_overrides(
        profile, {"capabilities": {"identity.sso": decision}})
    entitlements._mirror_legacy_restrictions(profile)
    assert profile["capabilities"]["identity.sso"] == decision


def test_rejected_override_entries_are_not_shielded_from_the_mirror():
    """An invalid decision is ignored AND must not exempt the key from narrowing."""
    profile = entitlements._base_profile("governed")
    profile["features"]["api_access"] = False
    profile = entitlements._apply_enterprise_overrides(
        profile, {"capabilities": {"integrations.api": "wide_open"}})
    entitlements._mirror_legacy_restrictions(profile)
    assert profile["capabilities"]["integrations.api"] == manifests.DENY


def test_unknown_capability_in_an_override_is_ignored():
    profile = entitlements._base_profile("verified")
    profile = entitlements._apply_enterprise_overrides(
        profile, {"capabilities": {"not.a.capability": manifests.ALLOW}})
    assert "not.a.capability" not in profile["capabilities"]


def test_mirror_still_narrows_capabilities_nobody_overrode():
    profile = entitlements._base_profile("governed")
    profile["features"]["api_access"] = False
    entitlements._mirror_legacy_restrictions(profile)
    assert profile["capabilities"]["integrations.api"] == manifests.DENY


def test_without_an_override_phase_2_behaviour_is_unchanged():
    plain = entitlements._base_profile("governed")
    entitlements._mirror_legacy_restrictions(plain)
    assert plain["capabilities"] == manifests.capabilities_for("governed")


def test_internal_override_marker_never_reaches_the_profile(db):
    """`_explicit_capabilities` is bookkeeping; it must not be served or cached."""
    org_id = _tenant(db, plan="verified")
    sub = db.query(CommercialSubscription).filter_by(organization_id=org_id).one()
    sub.enterprise_override = {"capabilities": {"identity.sso": manifests.ALLOW}}
    db.flush()

    profile = entitlements.compute(db, org_id)
    assert "_explicit_capabilities" not in profile
    assert profile["capabilities"]["identity.sso"] == manifests.ALLOW

    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    assert "_explicit_capabilities" not in row.profile_json


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4 - snapshot hash
# ══════════════════════════════════════════════════════════════════════════════
_BASE = dict(plan_code="governed", state="active", country="US",
             email_verified=True, enterprise_override={"a": 1},
             manifest_version="governed_2026_08")


def test_snapshot_is_deterministic_across_dict_ordering():
    a = entitlements._snapshot_hash(**{**_BASE, "enterprise_override": {"a": 1, "b": 2}})
    b = entitlements._snapshot_hash(**{**_BASE, "enterprise_override": {"b": 2, "a": 1}})
    assert a == b


def test_snapshot_is_stable_across_calls():
    assert entitlements._snapshot_hash(**_BASE) == entitlements._snapshot_hash(**_BASE)


@pytest.mark.parametrize("field,value", [
    ("plan_code", "sovereign"),
    ("state", "suspended"),
    ("country", "DE"),
    ("email_verified", False),
    ("enterprise_override", {"a": 2}),
    ("manifest_version", "governed_2026_09"),
])
def test_every_relevant_input_changes_the_snapshot(field, value):
    assert entitlements._snapshot_hash(**{**_BASE, field: value}) != \
        entitlements._snapshot_hash(**_BASE)


def test_an_irrelevant_representation_difference_does_not_churn_the_cache():
    assert entitlements._snapshot_hash(**{**_BASE, "country": "us"}) == \
        entitlements._snapshot_hash(**{**_BASE, "country": "US"})


def test_profile_carries_a_snapshot(db):
    org_id = _tenant(db, plan="governed")
    assert entitlements.compute(db, org_id)["snapshot_hash"]


def test_matching_snapshot_serves_the_cache(db):
    org_id = _tenant(db, plan="governed")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    row.profile_json = {**row.profile_json, "sentinel": "kept"}
    db.flush()
    assert entitlements.get(db, org_id).get("sentinel") == "kept"


def test_missing_snapshot_forces_recompute(db):
    org_id = _tenant(db, plan="governed")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    stripped = {k: v for k, v in row.profile_json.items() if k != "snapshot_hash"}
    row.profile_json = {**stripped, "sentinel": "stale"}
    db.flush()
    assert "sentinel" not in entitlements.get(db, org_id)


def test_wrong_snapshot_forces_recompute(db):
    org_id = _tenant(db, plan="governed")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    row.profile_json = {**row.profile_json, "snapshot_hash": "0" * 64,
                        "sentinel": "stale"}
    db.flush()
    assert "sentinel" not in entitlements.get(db, org_id)


# ── the cache is self-validating: these change inputs WITHOUT invalidating ────
def test_country_change_is_detected_without_any_invalidation(db):
    """Fix 3 drops the row explicitly. This proves the integrity check would
    have caught it regardless - defence in depth for every input."""
    org_id = _tenant(db, plan="governed", country="US")
    entitlements.compute(db, org_id)

    db.query(CommercialOrganization).filter_by(id=org_id).one().country = \
        next(iter(constants.SANCTIONED_COUNTRIES))
    db.flush()

    assert set(entitlements.get(db, org_id)["capabilities"].values()) == {manifests.DENY}


def test_override_change_is_detected_without_any_invalidation(db):
    org_id = _tenant(db, plan="verified")
    assert entitlements.compute(db, org_id)["capabilities"]["governance.ediscovery"] == manifests.DENY

    sub = db.query(CommercialSubscription).filter_by(organization_id=org_id).one()
    sub.enterprise_override = {"capabilities": {"governance.ediscovery": manifests.ALLOW}}
    db.flush()

    assert entitlements.get(db, org_id)["capabilities"]["governance.ediscovery"] == manifests.ALLOW


def test_email_verification_change_is_detected_without_any_invalidation(db):
    org_id = _tenant(db, plan="governed")
    assert entitlements.compute(db, org_id)["restrictions"]["email_unverified"] is True

    _verify_owner(db, org_id)

    assert entitlements.get(db, org_id)["restrictions"]["email_unverified"] is False


def test_plan_change_is_detected_without_any_invalidation(db):
    org_id = _tenant(db, plan="verified")
    assert entitlements.compute(db, org_id)["plan"] == "verified"

    db.query(CommercialSubscription).filter_by(
        organization_id=org_id).one().plan_code = "governed"
    db.flush()

    assert entitlements.get(db, org_id)["plan"] == "governed"


def test_commercial_state_change_is_detected_without_any_invalidation(db):
    org_id = _tenant(db, plan="governed", state="active")
    entitlements.compute(db, org_id)

    db.query(CommercialOrganization).filter_by(
        id=org_id).one().commercial_state = "suspended"
    db.flush()

    assert entitlements.get(db, org_id)["commercial_state"] == "suspended"


def test_unresolvable_tenant_still_has_the_shape(db):
    profile = entitlements.compute(db, "org_nope")
    assert profile["snapshot_hash"] is None
    assert profile["manifest_version"] is None


# ══════════════════════════════════════════════════════════════════════════════
# Still no enforcement
# ══════════════════════════════════════════════════════════════════════════════
def test_the_gate_is_wired_only_to_approved_caps_and_stays_off():
    """Phase 4.2 wired the rollout-gated gate to the approved safe-first admin
    capabilities. Because it is non-blocking while enforcement is OFF (see
    enforcement._gate_capability), defining and wiring the mechanism has NOT
    turned into applying it: no route can 403 in the current configuration, and
    no capability outside the approved set began wiring.
    """
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
    from app.commercial import enforcement
    assert enforcement.enforcement_mode() == enforcement.MODE_OFF
