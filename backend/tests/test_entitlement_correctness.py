"""Phase 2.8 - correctness fixes beneath the entitlement layer.

Four defects, none of which changed what any user can do today, all of which
would have made later enforcement wrong:

  1. the cache TTL check relabelled the timezone instead of converting it
  2. a manifest change did not invalidate cached profiles
  3. in-place plan changes never reached the commercial projection
  4. capabilities bypassed the resolution chain entirely

Nothing here enforces anything. These tests pin the plumbing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.commercial import constants, entitlements, ids, manifests
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, EntitlementsCache,
)
from database import models

IST = timezone(timedelta(hours=5, minutes=30))
PST = timezone(timedelta(hours=-8))


@pytest.fixture(scope="module", autouse=True)
def _billing_tables(engine):
    """conftest creates only the commercial subset; the projection test needs
    the operational billing tables too."""
    models.BillingPlan.__table__.create(engine, checkfirst=True)
    models.BillingSubscription.__table__.create(engine, checkfirst=True)
    yield


def _governed_plan(db):
    """Get-or-create. ``change_stripe_plan`` commits, so a plan row inserted by
    one test survives into the next and a second insert would collide."""
    existing = db.query(models.BillingPlan).filter_by(code="governed").first()
    if existing is not None:
        return existing
    plan = models.BillingPlan(code="governed", name="Governed", tier_order=2,
                              monthly_price_per_user=15.0)
    db.add(plan)
    db.flush()
    return plan


def _tenant(db, *, plan="verified", state="active", country="US"):
    op = models.Organization(name=f"Acme {ids.org_id()[:8]}")
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country=country, commercial_state=state,
        operational_org_id=op.id))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
        billing_cycle="monthly", seat_count=10, state=state, provider="stub",
        trial_started_at=datetime.now(timezone.utc),
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14)))
    db.flush()
    return op.id, org_id


def _verify_owner(db, org_id):
    """Give a commercial organization an owner whose email is verified.

    Entitlements now resolve email verification authoritatively from the
    organization's owner, so a tenant created with ``owner_user_id=None`` is
    correctly *unverified* and loses ``data_export`` under spec I-6. A test that
    means "an ordinary healthy organization" has to say so explicitly instead of
    relying on the old default argument.
    """
    from app.commercial.models import EmailVerificationToken
    user = models.User(name="Owner", email=f"owner+{org_id[:10]}@example.test")
    db.add(user)
    db.flush()
    db.add(EmailVerificationToken(
        user_id=user.id, organization_id=org_id,
        token_hash=f"hash_{org_id[:24]}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        consumed_at=datetime.now(timezone.utc),
    ))
    org = db.query(CommercialOrganization).filter_by(id=org_id).one()
    org.owner_user_id = user.id
    db.flush()
    return user


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 - TTL timezone
# ══════════════════════════════════════════════════════════════════════════════
# Tested as pure functions on purpose. The suite runs on SQLite, which has no
# native timezone type and hands back naive datetimes, so a test that writes a
# cache row and reads it back cannot reproduce the bug and would pass either way.

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("tz", [timezone.utc, IST, PST])
def test_expired_row_is_stale_in_every_timezone(tz):
    """The original defect: an expiry ten minutes past, expressed at +05:30,
    read as five hours in the future."""
    expired = (NOW - timedelta(minutes=10)).astimezone(tz)
    assert entitlements._is_fresh(expired, now=NOW) is False


@pytest.mark.parametrize("tz", [timezone.utc, IST, PST])
def test_unexpired_row_is_fresh_in_every_timezone(tz):
    future = (NOW + timedelta(minutes=10)).astimezone(tz)
    assert entitlements._is_fresh(future, now=NOW) is True


def test_naive_values_are_read_as_utc():
    """SQLite returns naive datetimes; they must not be silently shifted."""
    assert entitlements._is_fresh((NOW - timedelta(minutes=1)).replace(tzinfo=None), now=NOW) is False
    assert entitlements._is_fresh((NOW + timedelta(minutes=1)).replace(tzinfo=None), now=NOW) is True


def test_missing_expiry_is_never_fresh():
    assert entitlements._is_fresh(None, now=NOW) is False


def test_as_utc_converts_rather_than_relabels():
    """The exact bug: .replace() would have returned 17:30 UTC for this value."""
    noon_ist = datetime(2026, 8, 25, 17, 30, tzinfo=IST)   # == 12:00 UTC
    assert entitlements._as_utc(noon_ist) == NOW


def test_ttl_constant_is_unchanged():
    """This phase corrects the comparison, never the configured lifetime."""
    assert constants.ENTITLEMENTS_CACHE_TTL_SECONDS == 3600


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 - manifest version freshness
# ══════════════════════════════════════════════════════════════════════════════
def test_profile_written_with_the_current_manifest_version(db):
    _, org_id = _tenant(db, plan="governed")
    profile = entitlements.compute(db, org_id)
    assert profile["manifest_version"] == manifests.CURRENT_VERSIONS["governed"]


def test_current_cache_is_served_without_recomputing(db):
    _, org_id = _tenant(db, plan="governed")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    row.profile_json = {**row.profile_json, "sentinel": "untouched"}
    db.flush()

    assert entitlements.get(db, org_id).get("sentinel") == "untouched", \
        "a current, unexpired row should be served as-is"


def test_stale_manifest_version_forces_recompute(db):
    _, org_id = _tenant(db, plan="governed")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    row.profile_json = {**row.profile_json,
                        "manifest_version": "governed_1999_01",
                        "sentinel": "stale"}
    db.flush()

    fresh = entitlements.get(db, org_id)
    assert "sentinel" not in fresh
    assert fresh["manifest_version"] == manifests.CURRENT_VERSIONS["governed"]


def test_legacy_profile_shape_forces_recompute(db):
    """The 11 rows that predate the capability layer carry neither key.

    They matter because capability() fails CLOSED on a missing key while can()
    fails open - serving one of these to a capability check would deny
    everything.
    """
    _, org_id = _tenant(db, plan="governed")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    legacy = {k: v for k, v in row.profile_json.items()
              if k not in ("capabilities", "manifest_version")}
    row.profile_json = legacy
    db.flush()
    assert "capabilities" not in row.profile_json

    rebuilt = entitlements.get(db, org_id)
    assert "capabilities" in rebuilt
    assert rebuilt["manifest_version"] == manifests.CURRENT_VERSIONS["governed"]


def test_expired_row_still_recomputes(db):
    """The version check is additional to the TTL, not a replacement for it."""
    _, org_id = _tenant(db, plan="verified")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    row.profile_json = {**row.profile_json, "sentinel": "expired"}
    db.flush()

    assert "sentinel" not in entitlements.get(db, org_id)


def test_unknown_plan_does_not_recompute_forever(db):
    """An unrecognised plan_code falls back to Verified in both the manifest
    lookup and the freshness check, so it settles instead of thrashing."""
    _, org_id = _tenant(db, plan="not_a_real_plan")
    entitlements.compute(db, org_id)
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    assert entitlements._manifest_is_current(row) is True


def test_phase_1_invalidation_still_works(db):
    _, org_id = _tenant(db, plan="governed")
    entitlements.compute(db, org_id)
    entitlements.invalidate(db, org_id)
    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).one_or_none() is None


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3 (billing -> commercial state projection) lives with the billing PR: those
# tests depend on app.commercial.billing_bridge + app.billing, intentionally NOT
# part of this entitlement-only shadow branch. They move with that code so this
# branch imports no billing behavior.

# ══════════════════════════════════════════════════════════════════════════════
# Fix 4 - capabilities through the resolution chain
# ══════════════════════════════════════════════════════════════════════════════
def test_capabilities_start_from_the_plan_manifest():
    profile = entitlements._base_profile("sovereign")
    assert profile["capabilities"] == manifests.capabilities_for("sovereign")


def test_sanctioned_jurisdiction_denies_every_capability():
    country = next(iter(constants.SANCTIONED_COUNTRIES))
    profile = entitlements._apply_jurisdiction(
        entitlements._base_profile("sovereign"), country)

    assert set(profile["capabilities"].values()) == {manifests.DENY}
    assert not any(profile["features"].values()), "existing feature behaviour preserved"


def test_unsanctioned_jurisdiction_changes_nothing():
    before = entitlements._base_profile("governed")
    expected = dict(before["capabilities"])
    after = entitlements._apply_jurisdiction(before, "US")
    assert after["capabilities"] == expected


def test_enterprise_override_can_name_capabilities():
    profile = entitlements._apply_enterprise_overrides(
        entitlements._base_profile("verified"),
        {"capabilities": {"reports.advanced": manifests.ALLOW}})
    assert profile["capabilities"]["reports.advanced"] == manifests.ALLOW


def test_enterprise_override_ignores_unknown_keys_and_decisions():
    """A malformed overlay must not widen access or inject an invalid state."""
    profile = entitlements._apply_enterprise_overrides(
        entitlements._base_profile("verified"),
        {"capabilities": {"not.a.capability": manifests.ALLOW,
                          "reports.standard": "wide_open"}})
    assert "not.a.capability" not in profile["capabilities"]
    assert profile["capabilities"]["reports.standard"] in manifests.DECISIONS


def test_override_still_applies_to_features_and_limits():
    """Established behaviour must be untouched by the capability addition."""
    profile = entitlements._apply_enterprise_overrides(
        entitlements._base_profile("verified"),
        {"features": {"sso": True}, "limits": {"seat_limit": 999}})
    assert profile["features"]["sso"] is True
    assert profile["limits"]["seat_limit"] == 999


def test_mirror_denies_a_capability_whose_legacy_flag_was_switched_off():
    profile = entitlements._base_profile("governed")
    assert profile["capabilities"]["integrations.api"] == manifests.ALLOW
    profile["features"]["api_access"] = False
    entitlements._mirror_legacy_restrictions(profile)
    assert profile["capabilities"]["integrations.api"] == manifests.DENY


def test_mirror_never_promotes_configuration_required():
    profile = entitlements._base_profile("sovereign")
    entitlements._mirror_legacy_restrictions(profile)
    assert profile["capabilities"]["evidence.screenshots.capture"] == \
        manifests.CONFIGURATION_REQUIRED


def test_mirror_leaves_features_and_limits_alone():
    profile = entitlements._base_profile("governed")
    features, limits = dict(profile["features"]), dict(profile["limits"])
    entitlements._mirror_legacy_restrictions(profile)
    assert profile["features"] == features
    assert profile["limits"] == limits


def test_suspension_applies_the_approved_lifecycle_policy(db):
    """Was: a boundary marker for an undecided question.

    This test previously asserted that suspension narrowed ONLY what the legacy
    mirror derived (integrations.api), leaving reports.advanced and
    payroll.calculator at ALLOW, because what suspension should do to the ~30
    capabilities with no legacy counterpart was an open product question.

    That question has since been answered: premium writes are denied, genuine
    historical reads become READ_ONLY, and essentials survive. The assertions are
    updated to the decided behaviour rather than the placeholder.
    """
    _, org_id = _tenant(db, plan="governed", state="suspended")
    caps = entitlements.compute(db, org_id)["capabilities"]

    # Still derived from the legacy flag, as before.
    assert caps["integrations.api"] == manifests.DENY
    # Premium writes: denied.
    assert caps["payroll.calculator"] == manifests.DENY
    # Genuine historical reads: preserved, not denied.
    assert caps["reports.advanced"] == manifests.READ_ONLY
    # Essential routes: untouched.
    assert caps["billing.portal"] == manifests.ALLOW


def test_email_verification_has_no_capability_counterpart(db):
    """data_export names no capability, so this layer correctly does nothing."""
    _, org_id = _tenant(db, plan="governed")
    verified = entitlements.compute(db, org_id, email_verified=True)["capabilities"]
    entitlements.invalidate(db, org_id)
    unverified = entitlements.compute(db, org_id, email_verified=False)["capabilities"]
    assert verified == unverified


# ══════════════════════════════════════════════════════════════════════════════
# The existing contract is untouched
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("plan", manifests.PLANS)
def test_features_and_limits_are_byte_identical_to_the_plan_matrix(db, plan):
    from app.commercial.entitlements import _PLAN_FEATURES, _PLAN_LIMITS

    _, org_id = _tenant(db, plan=plan, state="active")
    _verify_owner(db, org_id)
    profile = entitlements.compute(db, org_id)
    assert profile["features"] == _PLAN_FEATURES[plan]
    assert profile["limits"] == _PLAN_LIMITS[plan]


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
