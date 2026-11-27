"""Phase 3.5a - an impaired tenant keeps the routes it needs to recover.

Phase 3.4 found that ``_empty_profile`` denied *every* capability, including
``billing.portal`` and ``identity.mfa``. That breaks the essential-route
invariant in the worst possible place: an organization in ``manual_review``
could not reach the billing portal, which is the one route that would let it
stop being in ``manual_review``.

The fix is deliberately narrow. Essential capabilities inherit whatever their
plan manifest already decided - they are not force-allowed. So Verified keeps
``identity.sso`` denied here exactly as it does everywhere else, and SSO
packaging is untouched. Everything non-essential still denies, and the two
globally denied privacy capabilities still deny.
"""

import pathlib as _pathlib

import pytest

from app.billing import service as billing_service
from app.commercial import entitlements, ids, manifests
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

PRIVACY = sorted(manifests.GLOBALLY_DENIED)

#: What an impaired Verified tenant is allowed to reach. Written out rather
#: than derived, so a change to the essential set has to be made here too.
EXPECTED_PERMISSIVE = {"billing.portal", "identity.mfa", "identity.sso"}


def _identity(db, name, *, state="manual_review", plan=None, country="US"):
    """A commercial identity, optionally with a subscription."""
    op = models.Organization(name=name, billing_type="customer")
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
    return op, org_id


def _caps(db, org_id):
    return entitlements.compute(db, org_id)["capabilities"]


def _permissive(caps):
    return {k for k, v in caps.items() if v in manifests.PERMISSIVE}


# ==============================================================================
# The fix: manual_review keeps its essential routes
# ==============================================================================
def test_manual_review_allows_the_billing_portal(db):
    """The whole point: this is the route out of manual_review."""
    _, org_id = _identity(db, "Needs billing")
    assert _caps(db, org_id)["billing.portal"] == manifests.ALLOW


def test_manual_review_allows_mfa(db):
    """Account security must not lapse because billing is unresolved."""
    _, org_id = _identity(db, "Needs MFA")
    assert _caps(db, org_id)["identity.mfa"] == manifests.ALLOW


@pytest.mark.parametrize("key", PRIVACY)
def test_manual_review_still_denies_privacy(db, key):
    """The one rule that must never have a hole in it."""
    _, org_id = _identity(db, f"Privacy {key}")
    assert _caps(db, org_id)[key] == manifests.DENY


def test_manual_review_denies_a_historical_capability(db):
    """reports.standard is granted to every plan, and is still denied here."""
    _, org_id = _identity(db, "No standard reports")
    assert _caps(db, org_id)["reports.standard"] == manifests.DENY


def test_manual_review_denies_a_premium_operational_capability(db):
    _, org_id = _identity(db, "No payroll")
    assert _caps(db, org_id)["payroll.calculator"] == manifests.DENY


def test_manual_review_grants_exactly_the_expected_set(db):
    """Pinned exhaustively: nothing else slipped through."""
    _, org_id = _identity(db, "Exact set")
    assert _permissive(_caps(db, org_id)) == EXPECTED_PERMISSIVE


def test_manual_review_grants_nothing_outside_the_essential_set(db):
    _, org_id = _identity(db, "Essential subset")
    assert _permissive(_caps(db, org_id)) <= manifests.ESSENTIAL_CAPABILITIES


# ==============================================================================
# SSO packaging is NOT changed by this
# ==============================================================================
def test_sso_is_granted_to_an_impaired_verified_tenant(db):
    """SSO is now an all-plan security baseline (Product decision, 3.18) AND
    essential, so an impaired Verified tenant keeps it — account access must not
    lapse because billing is unresolved."""
    _, org_id = _identity(db, "Has SSO")
    assert _caps(db, org_id)["identity.sso"] == manifests.ALLOW


def test_essentials_inherit_the_plan_rather_than_being_forced():
    """The helper reads the manifest; it does not hard-code ALLOW."""
    verified = entitlements._essential_only_capabilities("verified")
    sovereign = entitlements._essential_only_capabilities("sovereign")

    # SSO is all-plan now, so both inherit ALLOW from their manifest.
    assert verified["identity.sso"] == manifests.ALLOW
    assert sovereign["identity.sso"] == manifests.ALLOW
    # ...and neither gains anything non-essential.
    for caps in (verified, sovereign):
        assert _permissive(caps) <= manifests.ESSENTIAL_CAPABILITIES


def test_unknown_plan_falls_back_without_raising():
    """manifest_for falls back to Verified; the helper must not explode."""
    caps = entitlements._essential_only_capabilities("no_such_plan")
    assert _permissive(caps) == EXPECTED_PERMISSIVE


# ==============================================================================
# Everything else is unchanged
# ==============================================================================
def test_a_healthy_customer_is_unaffected(db):
    _, org_id = _identity(db, "Healthy", state="active", plan="governed")
    caps = _caps(db, org_id)
    assert caps["reports.advanced"] == manifests.ALLOW
    assert caps["payroll.calculator"] == manifests.ALLOW
    assert caps == {**manifests.capabilities_for("governed"), "evidence.screenshots.capture": manifests.ALLOW}


def test_a_healthy_verified_trial_is_unaffected(db):
    _, org_id = _identity(db, "Trial", state="trial_active", plan="verified")
    assert _caps(db, org_id) == {**manifests.capabilities_for("verified"), "evidence.screenshots.capture": manifests.ALLOW}


def test_internal_tenant_is_unaffected(db):
    """Temveris has an identity and no subscription, but resolves through
    the internal branch - not through _empty_profile."""
    op = models.Organization(name="Temveris 35a", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    identity = (db.query(CommercialOrganization)
                  .filter_by(operational_org_id=op.id).one())

    profile = entitlements.compute(db, identity.id)
    assert profile["plan"] == manifests.INTERNAL_PLAN
    assert profile["capabilities"] == {**manifests.capabilities_for("sovereign"), "evidence.screenshots.capture": manifests.ALLOW}
    assert all(profile["capabilities"][k] == manifests.DENY for k in PRIVACY)


@pytest.mark.parametrize("state", ["suspended", "cancelled", "closed"])
def test_impaired_lifecycle_states_are_unaffected(db, state):
    """These have a subscription, so they never reach _empty_profile. They keep
    the lifecycle narrowing decided in Phase 3E."""
    _, org_id = _identity(db, f"L {state}", state=state, plan="governed")
    caps = _caps(db, org_id)

    assert caps["billing.portal"] == manifests.ALLOW
    assert all(caps[k] == manifests.DENY for k in PRIVACY)
    # A historical capability is read-only (suspended/cancelled) or denied
    # (closed) - either way, unchanged by this phase.
    assert caps["reports.standard"] in {manifests.READ_ONLY, manifests.DENY}


@pytest.mark.parametrize("state", ["suspended", "cancelled", "closed"])
def test_impaired_states_match_the_pure_lifecycle_function(db, state):
    """Proves the change did not leak into the lifecycle path."""
    _, org_id = _identity(db, f"P {state}", state=state, plan="governed")
    expected = entitlements._apply_lifecycle(
        entitlements._base_profile("governed"), state)["capabilities"]
    assert _caps(db, org_id) == expected


def test_sanctioned_jurisdiction_still_denies_everything(db):
    """A legal embargo outranks the essential set. Separate code path, and it
    must stay a total deny."""
    from app.commercial import constants
    country = next(iter(constants.SANCTIONED_COUNTRIES))
    _, org_id = _identity(db, "Embargo", state="active", plan="sovereign",
                          country=country)
    assert set(_caps(db, org_id).values()) == {manifests.DENY}


# ==============================================================================
# Scope guards - nothing outside entitlements.py moved
# ==============================================================================
def test_gate_wired_only_to_approved_caps_and_stays_off():
    """Phase 4.2: the rollout-gated gate is wired to the approved safe-first admin
    capabilities only, and enforcement is still OFF — so no route can block."""
    import re as _re
    root = _pathlib.Path(__file__).resolve().parents[1] / "app"
    pat = _re.compile(r'require(?:_writable)?_capability\("([a-z0-9_.]+)"\)')
    wired = set()
    for p in root.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            wired.add(m.group(1))
    assert wired == {"activity.anomaly", "integrations.api",
                     "payroll.calculator", "holidays.multicountry"}, wired
    from app.commercial import enforcement as _enf
    assert _enf.enforcement_mode() == _enf.MODE_OFF


def test_shadow_hooks_remain_observation_only():
    """Phase 3.5b widened the pilots from 2 routes to 19. The count is no longer
    the invariant - that every hook stays a discarded bare statement is."""
    import ast as _ast

    root = _pathlib.Path(__file__).resolve().parents[1] / "app"
    total = consumed = 0
    for p in root.rglob("*.py"):
        if p.name == "enforcement.py":
            continue
        src = p.read_text(encoding="utf-8")
        if "_shadow(" not in src:
            continue
        tree = _ast.parse(src)
        bare = {id(n.value) for n in _ast.walk(tree)
                if isinstance(n, _ast.Expr) and isinstance(n.value, _ast.Call)}
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Call) and getattr(n.func, "id", None) == "_shadow":
                total += 1
                consumed += id(n) not in bare

    assert total >= 2, "the original pilot hooks disappeared"
    assert consumed == 0, "a shadow result is being used to make a decision"


def test_trial_length_untouched():
    from app.billing.trial import TRIAL_LENGTH_DAYS
    assert TRIAL_LENGTH_DAYS == 14


def test_plan_matrices_and_seats_untouched():
    from app.commercial.entitlements import _PLAN_FEATURES, _PLAN_LIMITS
    assert _PLAN_LIMITS["verified"]["seat_limit"] == 25
    assert _PLAN_LIMITS["governed"]["seat_limit"] == 100
    assert _PLAN_FEATURES["sovereign"]["sso"] is True
    assert manifests.PLANS == ("verified", "governed", "sovereign", "enterprise")


def test_internal_tenant_stays_billing_inert(db):
    op = models.Organization(name="Inert 35a", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    assert billing_service.is_internal_tenant(db, op.id) is True
    with pytest.raises(Exception):
        billing_service.require_billable_tenant(db, op.id)


def test_the_fix_touches_no_schema_or_stripe_surface():
    """The fix is pure resolution logic; it reads existing manifests only."""
    src = (_pathlib.Path(__file__).resolve().parents[1]
           / "app" / "commercial" / "entitlements.py").read_text(encoding="utf-8")
    helper = src.split("def _essential_only_capabilities")[1].split("\ndef ")[0]
    for forbidden in ("Column(", "add_column", "create_table", "op.", "stripe"):
        assert forbidden not in helper, forbidden
