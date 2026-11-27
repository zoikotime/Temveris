"""Entitlements resolver tests (§13)."""

from datetime import datetime, timezone, timedelta

from app.commercial import entitlements, ids
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription,
)


def _make_org(db, *, plan="verified", state="trial_active", country="US"):
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name="Acme", country=country,
        commercial_state=state,
    ))
    db.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=org_id,
        plan_code=plan, billing_cycle="monthly", seat_count=10,
        state=state, provider="stub",
        trial_started_at=datetime.now(timezone.utc),
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=30),
    ))
    db.flush()
    return org_id


def test_verified_base_features(db):
    org_id = _make_org(db, plan="verified", state="active")
    ent = entitlements.compute(db, org_id, email_verified=True)
    assert ent["plan"] == "verified"
    assert ent["features"]["time_tracking"] is True
    assert ent["features"]["sso"] is True   # SSO is an all-plan baseline (3.18)
    assert ent["limits"]["seat_limit"] == 25


def test_sovereign_includes_sso(db):
    org_id = _make_org(db, plan="sovereign", state="active")
    ent = entitlements.compute(db, org_id, email_verified=True)
    assert ent["features"]["sso"] is True
    assert ent["features"]["scim"] is True
    assert ent["limits"]["seat_limit"] is None


def test_trial_grants_only_selected_plan_features(db):
    """ZT-COM-BILL-001 §K3 — a trial grants exactly the selected plan's
    entitlements. The previous Verified-trial upsell overlay (which switched on
    advanced_compliance + data_export) has been removed: a trial must represent
    what the customer will actually buy.
    """
    org_id = _make_org(db, plan="verified", state="trial_active")
    ent = entitlements.compute(db, org_id, email_verified=True)

    # Still flagged as being in trial — the flag is informational only.
    assert ent["trial_overlay"] is True

    # ...but no feature exceeds the Verified plan.
    assert ent["features"]["advanced_compliance"] is False
    assert ent["features"]["data_export"] is False
    assert ent["features"]["api_access"] is False
    assert ent["features"]["sso"] is True   # SSO is an all-plan baseline (3.18)

    # Verified's own features remain available during the trial.
    assert ent["features"]["time_tracking"] is True
    assert ent["features"]["audit_logs"] is True

    # A trial must not widen limits either.
    assert ent["limits"]["seat_limit"] == 25


def test_trial_features_identical_to_paid_same_plan(db):
    """A trialing tenant and a paying tenant on the same plan must resolve to
    the same feature set — proof the trial adds nothing."""
    trial_org = _make_org(db, plan="governed", state="trial_active")
    paid_org = _make_org(db, plan="governed", state="active")

    trial_ent = entitlements.compute(db, trial_org, email_verified=True)
    paid_ent = entitlements.compute(db, paid_org, email_verified=True)

    assert trial_ent["features"] == paid_ent["features"]
    assert trial_ent["limits"] == paid_ent["limits"]


def test_suspended_blocks_data_export(db):
    org_id = _make_org(db, plan="governed", state="suspended")
    ent = entitlements.compute(db, org_id, email_verified=True)
    assert ent["features"]["data_export"] is False
    assert ent["features"]["api_access"] is False


def test_unverified_email_restriction(db):
    org_id = _make_org(db, plan="governed", state="active")
    ent = entitlements.compute(db, org_id, email_verified=False)
    assert ent["restrictions"]["email_unverified"] is True
    assert ent["features"]["data_export"] is False


def test_invalidate_forces_recompute(db):
    org_id = _make_org(db, plan="verified", state="active")
    entitlements.compute(db, org_id)
    entitlements.invalidate(db, org_id)
    from app.commercial.models import EntitlementsCache
    assert db.query(EntitlementsCache).filter_by(organization_id=org_id).count() == 0
    # Read-through triggers recompute.
    ent = entitlements.get(db, org_id)
    assert ent["plan"] == "verified"


def test_can_predicate(db):
    org_id = _make_org(db, plan="sovereign", state="active")
    assert entitlements.can(db, org_id, "sso") is True
    assert entitlements.can(db, org_id, "scim") is True
    assert entitlements.can(db, org_id, "legal_hold") is True
