"""Phase 2 - canonical feature dictionary and versioned plan manifests.

These tests pin three things: that the dictionary and manifests cannot become
internally inconsistent, that the privacy denial cannot be lost, and that adding
the capability layer did not disturb the existing resolver contract.
"""

from datetime import datetime, timezone, timedelta

import pytest

from app.commercial import constants, entitlements, ids, manifests
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models as _dbmodels


# ── dictionary integrity ────────────────────────────────────────────────────
def test_every_feature_key_is_a_capability_not_a_plan():
    """The whole point of the vocabulary: ask what you may do, not who you are.

    ``offboarding.governed`` is allowed and is not a plan reference: the
    specification uses "governed offboarding" as the name of a workflow variant
    (reconciliation + attestation + audit evidence), which is a capability that
    happens to share a word with a plan. The check below still rejects a key
    whose FIRST segment is a plan name, or an is_/_features style flag.
    """
    plan_words = {"verified", "governed", "sovereign", "enterprise", "premium"}
    allowed_variant_keys = {"offboarding.governed"}

    for key in manifests.FEATURES:
        low = key.lower()
        head = low.split(".", 1)[0]
        assert head not in plan_words, f"{key} is namespaced under a plan name"
        assert not low.startswith("is_"), f"{key} reads as a plan predicate"
        assert not low.endswith("_features"), f"{key} reads as a plan bundle"
        if key in allowed_variant_keys:
            continue
        assert not any(f".{w}" in low for w in plan_words), (
            f"{key} names a plan rather than a capability")


def test_feature_keys_are_namespaced_and_unique():
    for key, feat in manifests.FEATURES.items():
        assert key == feat.key
        assert "." in key, f"{key} is not namespaced as category.capability"
        assert key == key.lower().strip()
    # Dict construction would silently collapse duplicates, so prove the source
    # tuple had none by comparing against the declared key of every entry.
    assert len(manifests.FEATURES) == len({f.key for f in manifests.FEATURES.values()})


def test_legacy_keys_reference_the_real_existing_vocabulary():
    """Traceability to the 9 pre-existing flags, so the two cannot drift apart."""
    for key, feat in manifests.FEATURES.items():
        if feat.legacy_key is not None:
            assert feat.legacy_key in constants.ENTITLEMENT_FEATURES, (
                f"{key} claims legacy_key {feat.legacy_key!r} which does not exist")


def test_every_plan_has_a_current_manifest_version():
    for plan in manifests.PLANS:
        version = manifests.CURRENT_VERSIONS[plan]
        assert version in manifests.MANIFESTS
        assert manifests.MANIFESTS[version].plan_code == plan


def test_manifest_versions_are_dated_and_plan_scoped():
    """Packaging changes ship as a new version, so the version must identify both."""
    for version, m in manifests.MANIFESTS.items():
        assert version.startswith(f"{m.plan_code}_"), version
        assert m.effective_from, version


def test_every_plan_records_a_decision_for_every_capability():
    """Silence is not a decision - an omitted key would deny by accident."""
    for version, m in manifests.MANIFESTS.items():
        assert set(m.grants) == set(manifests.FEATURES), (
            f"{version} does not cover the dictionary exactly")


def test_every_decision_and_provenance_is_from_the_closed_set():
    for version, m in manifests.MANIFESTS.items():
        for key, grant in m.grants.items():
            assert grant.decision in manifests.DECISIONS, f"{version}/{key}"
            assert grant.provenance in manifests.PROVENANCES, f"{version}/{key}"


# ── the validator actually rejects bad configuration ────────────────────────
def test_validator_rejects_a_manifest_that_grants_a_denied_capability(monkeypatch):
    """Proves the privacy guard is structural, not just a convention."""
    version = manifests.CURRENT_VERSIONS["governed"]
    broken = dict(manifests.MANIFESTS)
    m = broken[version]
    grants = dict(m.grants)
    grants["productivity.apps_history"] = manifests.Grant(
        manifests.ALLOW, manifests.POLICY)
    broken[version] = m._replace(grants=grants)

    monkeypatch.setattr(manifests, "MANIFESTS", broken)
    with pytest.raises(manifests.ManifestError, match="globally denied"):
        manifests._validate()


def test_validator_rejects_an_unknown_feature_key(monkeypatch):
    version = manifests.CURRENT_VERSIONS["verified"]
    broken = dict(manifests.MANIFESTS)
    m = broken[version]
    grants = dict(m.grants)
    grants["not.a.real.feature"] = manifests.Grant(manifests.ALLOW, manifests.POLICY)
    broken[version] = m._replace(grants=grants)

    monkeypatch.setattr(manifests, "MANIFESTS", broken)
    with pytest.raises(manifests.ManifestError, match="unknown features"):
        manifests._validate()


def test_validator_rejects_a_missing_decision(monkeypatch):
    version = manifests.CURRENT_VERSIONS["verified"]
    broken = dict(manifests.MANIFESTS)
    m = broken[version]
    grants = dict(m.grants)
    grants.pop("reports.standard")
    broken[version] = m._replace(grants=grants)

    monkeypatch.setattr(manifests, "MANIFESTS", broken)
    with pytest.raises(manifests.ManifestError, match="no decision recorded"):
        manifests._validate()


def test_validator_rejects_plain_allow_on_a_configuration_required_capability(monkeypatch):
    """Entitled is not enabled - a privacy-impacting capability may not be a bare allow."""
    version = manifests.CURRENT_VERSIONS["sovereign"]
    broken = dict(manifests.MANIFESTS)
    m = broken[version]
    grants = dict(m.grants)
    grants["evidence.screenshots.capture"] = manifests.Grant(
        manifests.ALLOW, manifests.POLICY)
    broken[version] = m._replace(grants=grants)

    monkeypatch.setattr(manifests, "MANIFESTS", broken)
    with pytest.raises(manifests.ManifestError, match="configuration_required"):
        manifests._validate()


def test_validator_rejects_a_bogus_legacy_key(monkeypatch):
    broken = dict(manifests.FEATURES)
    key = "reports.standard"
    broken[key] = broken[key]._replace(legacy_key="not_a_real_flag")

    monkeypatch.setattr(manifests, "FEATURES", broken)
    with pytest.raises(manifests.ManifestError, match="legacy_key"):
        manifests._validate()


# ── global privacy denial (spec section 17) ─────────────────────────────────
@pytest.mark.parametrize("plan", manifests.PLANS)
@pytest.mark.parametrize("key", sorted(manifests.GLOBALLY_DENIED))
def test_privacy_features_are_denied_on_every_plan(plan, key):
    """Apps History and Website Categories are denied to every plan, at any price."""
    assert manifests.decision_for(plan, key) == manifests.DENY
    assert manifests.manifest_for(plan).allows(key) is False


def test_the_globally_denied_set_is_exactly_the_two_privacy_features():
    assert manifests.GLOBALLY_DENIED == {
        "productivity.apps_history",
        "productivity.website_categories",
    }


# ── plan resolution ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("plan", manifests.PLANS)
def test_plan_resolves_to_its_own_manifest(plan):
    m = manifests.manifest_for(plan)
    assert m.plan_code == plan
    assert m.version == manifests.CURRENT_VERSIONS[plan]


def test_unknown_plan_falls_back_to_verified_like_the_resolver_does():
    """Matches entitlements._base_profile's existing unknown-plan fallback."""
    assert manifests.manifest_for("nonsense").plan_code == "verified"
    assert manifests.manifest_for(None).plan_code == "verified"


def test_unknown_capability_key_denies():
    """A typo must never widen access."""
    assert manifests.decision_for("sovereign", "reports.doesnotexist") == manifests.DENY


def test_capabilities_for_covers_the_whole_dictionary():
    for plan in manifests.PLANS:
        assert set(manifests.capabilities_for(plan)) == set(manifests.FEATURES)


# ── tiering is intentional, and mirrors what already ships ──────────────────
def test_tiered_capabilities_match_the_existing_plan_features_matrix():
    """The manifest must not invent packaging. Every capability that differs by
    plan has to correspond to a distinction the resolver already makes today.
    """
    from app.commercial.entitlements import _PLAN_FEATURES

    differing = [
        key for key in manifests.FEATURES
        if len({manifests.decision_for(p, key) for p in manifests.PLANS}) > 1
    ]
    for key in differing:
        feat = manifests.FEATURES[key]
        grant = manifests.manifest_for("governed").grants[key]
        if grant.provenance == manifests.SPECIFICATION:
            continue  # sourced from the spec, not from shipped behaviour
        assert feat.legacy_key is not None, (
            f"{key} differs by plan with provenance {grant.provenance!r} but has no "
            f"legacy_key - that would be newly invented packaging")
        for plan in manifests.PLANS:
            expected = _PLAN_FEATURES[plan][feat.legacy_key]
            actual = manifests.manifest_for(plan).allows(key)
            assert actual == expected, (
                f"{key} on {plan}: manifest says {actual}, but the shipped "
                f"_PLAN_FEATURES[{plan}][{feat.legacy_key!r}] says {expected}")


def test_governed_is_a_superset_of_verified():
    """Section 16 inheritance, verified per capability rather than assumed."""
    for key in manifests.FEATURES:
        if manifests.manifest_for("verified").allows(key):
            assert manifests.manifest_for("governed").allows(key), (
                f"{key} is allowed on Verified but not on Governed")


def test_sovereign_is_a_superset_of_governed():
    for key in manifests.FEATURES:
        if manifests.manifest_for("governed").allows(key):
            assert manifests.manifest_for("sovereign").allows(key), (
                f"{key} is allowed on Governed but not on Sovereign")


def test_enterprise_is_at_least_sovereign():
    """Enterprise starts from Sovereign; per-contract variation is carried by
    CommercialSubscription.enterprise_override, not by a static manifest."""
    for key in manifests.FEATURES:
        if manifests.manifest_for("sovereign").allows(key):
            assert manifests.manifest_for("enterprise").allows(key), key


def test_no_plan_is_granted_a_capability_that_does_not_exist():
    """A manifest must not promise something unbuilt.

    integrations.api is the one known exception: the shipped _PLAN_FEATURES
    already advertises api_access on Governed and above while no customer API
    exists. It is listed here so the discrepancy stays visible and cannot grow.
    """
    known_exceptions = {"integrations.api"}
    offenders = set()
    for plan in manifests.PLANS:
        for key, feat in manifests.FEATURES.items():
            if feat.implementation == manifests.ABSENT and manifests.manifest_for(plan).allows(key):
                offenders.add(key)
    assert offenders <= known_exceptions, (
        f"manifest grants unbuilt capabilities: {sorted(offenders - known_exceptions)}")


def test_provisional_keys_are_reported_and_uniform_across_plans():
    """Status-quo grants preserve today's behaviour, so they must not be tiered."""
    provisional = manifests.provisional_keys()
    assert provisional, "expected ungated-today capabilities to be recorded"
    for key in provisional:
        decisions = {manifests.decision_for(p, key) for p in manifests.PLANS}
        assert len(decisions) == 1, (
            f"{key} is provisional (ungated today) but differs by plan {decisions}")


# ── resolver integration: additive, nothing disturbed ───────────────────────
def _tenant(db, *, plan="verified", state="trial_active"):
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name="Acme", country="US", commercial_state=state))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id,
        plan_code=plan, billing_cycle="monthly", seat_count=10,
        state=state, provider="stub",
        trial_started_at=datetime.now(timezone.utc),
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14),
    ))
    db.flush()
    return org_id


def _verify_owner(db, org_id):
    """Give a commercial organization an owner whose email is verified.

    Entitlements now resolve email verification authoritatively from the
    organization's owner, so a tenant created with ``owner_user_id=None`` is
    correctly *unverified* and loses ``data_export`` under spec I-6. A test that
    means "an ordinary healthy organization" has to say so explicitly instead of
    relying on the old default argument.
    """
    from app.commercial.models import EmailVerificationToken
    user = _dbmodels.User(name="Owner", email=f"owner+{org_id[:10]}@example.test")
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


@pytest.mark.parametrize("plan", manifests.PLANS)
def test_resolver_attaches_the_capability_layer(db, plan):
    org_id = _tenant(db, plan=plan, state="active")
    ent = entitlements.compute(db, org_id)

    assert ent["manifest_version"] == manifests.CURRENT_VERSIONS[plan]
    assert ent["capabilities"] == manifests.capabilities_for(plan)


def test_the_nine_legacy_flags_are_untouched_by_the_capability_layer(db):
    """The existing contract must be byte-identical - other code depends on it."""
    from app.commercial.entitlements import _PLAN_FEATURES

    org_id = _tenant(db, plan="governed", state="active")
    _verify_owner(db, org_id)
    ent = entitlements.compute(db, org_id)

    assert ent["features"] == _PLAN_FEATURES["governed"]
    assert set(ent["features"]) == set(constants.ENTITLEMENT_FEATURES)
    assert set(ent["limits"]) == set(constants.ENTITLEMENT_LIMITS)


def test_capability_and_permits_accessors(db):
    org_id = _tenant(db, plan="sovereign", state="active")

    assert entitlements.capability(db, org_id, "identity.sso") == manifests.ALLOW
    assert entitlements.permits(db, org_id, "identity.sso") is True

    # Denied to every plan, whatever the tier.
    assert entitlements.capability(db, org_id, "productivity.apps_history") == manifests.DENY
    assert entitlements.permits(db, org_id, "productivity.apps_history") is False

    # Entitled is not enabled: available, but not switched on.
    assert entitlements.capability(
        db, org_id, "evidence.screenshots.capture") == manifests.CONFIGURATION_REQUIRED
    assert entitlements.permits(db, org_id, "evidence.screenshots.capture") is False

    # Unknown key denies rather than defaulting open.
    assert entitlements.capability(db, org_id, "made.up.key") == manifests.DENY


def test_capability_layer_survives_the_cache_round_trip(db):
    """The profile is cached as JSON, so the new keys have to serialise."""
    org_id = _tenant(db, plan="governed", state="active")
    entitlements.compute(db, org_id)

    cached = entitlements.get(db, org_id)
    assert cached["manifest_version"] == manifests.CURRENT_VERSIONS["governed"]
    assert cached["capabilities"]["reports.compliance"] == manifests.DENY  # Sovereign+ now


def test_unresolvable_tenant_gets_the_shape_but_no_grants(db):
    """Shape-compatible so callers need no special case; grants nothing beyond
    the fallback plan's own essential routes (Phase 3.5a)."""
    profile = entitlements.compute(db, "org_does_not_exist")

    assert profile["manifest_version"] is None
    assert set(profile["capabilities"]) == set(manifests.FEATURES)

    caps = profile["capabilities"]
    permissive = {k for k, v in caps.items() if v in manifests.PERMISSIVE}
    # SSO is now an all-plan essential, so it joins the fallback essentials.
    assert permissive == {"billing.portal", "identity.mfa", "identity.sso"}
    assert permissive <= manifests.ESSENTIAL_CAPABILITIES, (
        "an unresolvable tenant must never gain a non-essential capability")


# ── trial behaviour (14 days, unchanged) ────────────────────────────────────
def test_trial_grants_exactly_the_selected_plans_capabilities(db):
    """A 14-day trial previews the plan being bought - no more, no less."""
    trial_org = _tenant(db, plan="verified", state="trial_active")
    paid_org = _tenant(db, plan="verified", state="active")

    trial = entitlements.compute(db, trial_org)
    paid = entitlements.compute(db, paid_org)

    assert trial["trial_overlay"] is True
    assert paid["trial_overlay"] is False
    assert trial["capabilities"] == paid["capabilities"]
    assert trial["manifest_version"] == paid["manifest_version"]


def test_phase_2_introduces_no_thirty_day_trial_behaviour():
    """The live rule is 14 days. This module must not encode any trial length."""
    import inspect
    src = inspect.getsource(manifests)
    assert "30" not in src.replace("2026", ""), (
        "the manifest module must not encode a trial length")
    from app.billing.trial import TRIAL_LENGTH_DAYS
    assert TRIAL_LENGTH_DAYS == 14


def test_manifests_carry_no_billing_or_pricing_data():
    """Section 19 - the manifest answers capability questions, never billing ones.

    Checked against the actual data rather than the source text, so that prose in
    a docstring cannot fail it and, more importantly, so that a real pricing field
    smuggled into the structures cannot pass it.
    """
    billing_words = ("stripe", "price", "amount", "currency", "usd", "invoice",
                     "payment", "cost", "discount")

    surface = []
    for key, feat in manifests.FEATURES.items():
        surface += [key, feat.category, feat.legacy_key or "", feat.display_name]
    for version, m in manifests.MANIFESTS.items():
        surface += [version, m.plan_code, m.effective_from]
        surface += list(m.grants)
        surface += [g.decision for g in m.grants.values()]
        surface += [g.provenance for g in m.grants.values()]

    for value in surface:
        low = str(value).lower()
        for token in billing_words:
            assert token not in low, (
                f"manifest data references billing concept {token!r} in {value!r}")

    # And no module-level attribute holds anything numeric that could be a price.
    for name in dir(manifests):
        if name.startswith("__"):
            continue
        value = getattr(manifests, name)
        assert not isinstance(value, (int, float)) or isinstance(value, bool), (
            f"manifests.{name} is a bare number; pricing must live in billing")
