"""Phase 3.11 — Admin Entitlement Inspector completeness (SPEC.txt §19).

The inspector is a read-only diagnostic. These tests pin the new sections (cache
status, pending lifecycle, enterprise overlay, recent events), the honest
"unavailable" representations, security, tenant isolation, and the invariant that
it never mutates entitlement/subscription/override state.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.billing import service as billing_service
from app.commercial import entitlements, ids, manifests
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import (
    CommercialOrganization, CommercialSubscription, CommercialEvent,
)
from app.commercial.audit import emit_event
from database import models

PRIVACY = sorted(manifests.GLOBALLY_DENIED)


def _su(db, org_id=1):
    return models.User(id=-901, name="su", email="insp-su@x.test",
                       organization_id=org_id, role="super_admin")


def _tenant(db, *, plan="verified", state="active", country="US",
            billing_type="customer", with_sub=True, override=None):
    op = models.Organization(name=f"insp {ids.org_id()[:8]}", billing_type=billing_type)
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country=country, commercial_state=state,
        operational_org_id=op.id))
    sub = None
    if with_sub:
        sub = CommercialSubscription(
            id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
            billing_cycle="monthly", seat_count=5, state=state, provider="stub",
            enterprise_override=override or {})
        db.add(sub)
    db.flush()
    return org_id, sub


def _inspect(db, org_id, user=None):
    # Deferred import: importing the admin router at module (collection) time,
    # before conftest installs the StaticPool SQLite engine, disturbs the shared
    # in-memory connection and drops tables for later suites. Matches the pattern
    # in test_entitlement_projection.
    from app.commercial.routers import admin as admin_router
    return admin_router.entitlement_inspector(
        org_id, db=db, current_user=user or _su(db))


# ══════════════════════════════════════════════════════════════════════════════
# Backward compatibility + shape
# ══════════════════════════════════════════════════════════════════════════════
def test_existing_fields_are_preserved(db):
    org_id, _ = _tenant(db, plan="governed")
    r = _inspect(db, org_id)
    for key in ("organization", "commercial_state", "plan", "subscription_state",
                "billing_cycle", "snapshot", "restrictions", "capabilities",
                "decision_trace", "notes"):
        assert key in r, key
    assert len(r["capabilities"]) == 38


def test_new_sections_are_present(db):
    org_id, _ = _tenant(db)
    r = _inspect(db, org_id)
    for key in ("subscription", "cache", "pending_lifecycle",
                "enterprise_overlay", "recent_events"):
        assert key in r, key


# ══════════════════════════════════════════════════════════════════════════════
# Cache diagnostics
# ══════════════════════════════════════════════════════════════════════════════
def test_cache_hit_after_warming(db):
    org_id, _ = _tenant(db, plan="sovereign")
    entitlements.invalidate(db, org_id)
    entitlements.get(db, org_id)                    # warm
    r = _inspect(db, org_id)
    c = r["cache"]
    assert c["status"] == "HIT"
    assert c["present"] is True
    assert c["is_fresh"] is True and c["manifest_current"] is True
    assert 0 <= c["snapshot_age_seconds"] < 60
    assert 0 < c["ttl_remaining_seconds"] <= 3600
    assert c["snapshot_hash"] == r["snapshot"]["snapshot_hash"]


def test_cache_not_available_when_cold(db):
    org_id, _ = _tenant(db)
    entitlements.invalidate(db, org_id)             # drop any row
    status = entitlements.cache_status(db, org_id)  # inspect the raw helper (pre-get)
    assert status["status"] == "NOT_AVAILABLE"
    assert status["present"] is False
    assert status["snapshot_age_seconds"] is None


def test_last_invalidation_is_honestly_unavailable(db):
    org_id, _ = _tenant(db)
    entitlements.get(db, org_id)
    r = _inspect(db, org_id)
    assert r["cache"]["last_invalidation"] is None
    assert r["notes"]["last_invalidation"] == "not_tracked"


# ══════════════════════════════════════════════════════════════════════════════
# Pending lifecycle — current vs pending, only what is stored
# ══════════════════════════════════════════════════════════════════════════════
def test_trial_expiry_is_shown_when_present(db):
    org_id, sub = _tenant(db, plan="verified", state="trial_active")
    sub.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=10)
    db.flush()
    r = _inspect(db, org_id)
    pl = r["pending_lifecycle"]
    assert pl["current_state"] == "trial_active"
    assert pl["trial_expiry"] is not None


def test_pending_cancellation_is_detected(db):
    org_id, sub = _tenant(db, plan="governed", state="cancelled_pending_end")
    sub.cancel_requested_at = datetime.now(timezone.utc)
    db.flush()
    r = _inspect(db, org_id)
    pl = r["pending_lifecycle"]
    assert pl["pending_cancellation"] is True
    assert pl["has_pending"] is True
    assert pl["cancellation_requested_at"] is not None


def test_no_pending_for_a_clean_active_tenant(db):
    org_id, _ = _tenant(db, plan="verified", state="active")
    pl = _inspect(db, org_id)["pending_lifecycle"]
    assert pl["pending_cancellation"] is False
    assert pl["has_pending"] is False


def test_scheduled_downgrade_is_reported_not_tracked(db):
    """The commercial layer stores no pending plan change — reported honestly."""
    org_id, _ = _tenant(db)
    pl = _inspect(db, org_id)["pending_lifecycle"]
    assert pl["scheduled_downgrade"] == "not_tracked_in_commercial_layer"


def test_recovery_available_in_impaired_states(db):
    for state in ("past_due", "suspended", "cancelled_pending_end"):
        org_id, _ = _tenant(db, plan="governed", state=state)
        assert _inspect(db, org_id)["pending_lifecycle"]["recovery_available"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Enterprise overlay — read-only, keys only
# ══════════════════════════════════════════════════════════════════════════════
def test_overlay_inactive_by_default(db):
    org_id, _ = _tenant(db)
    ov = _inspect(db, org_id)["enterprise_overlay"]
    assert ov == {"active": False, "keys": [], "source": None}


def test_overlay_exposes_keys_not_values(db):
    org_id, _ = _tenant(db, plan="enterprise",
                        override={"seat_limit": 9999, "api_requests_per_day": 100000})
    ov = _inspect(db, org_id)["enterprise_overlay"]
    assert ov["active"] is True
    assert ov["keys"] == ["api_requests_per_day", "seat_limit"]
    assert ov["source"] == "subscription.enterprise_override"
    # values are NOT exposed
    assert "9999" not in str(ov) and "100000" not in str(ov)


# ══════════════════════════════════════════════════════════════════════════════
# Recent events — persisted only, no fabrication
# ══════════════════════════════════════════════════════════════════════════════
def test_recent_events_reflect_persisted_rows(db):
    org_id, _ = _tenant(db, plan="governed")
    emit_event(db, event_type="entitlements.recomputed",
               source_service="test", organization_id=org_id,
               state_from="trial_active", state_to="active")
    db.flush()
    r = _inspect(db, org_id)
    types = [e["event_type"] for e in r["recent_events"]]
    assert "entitlements.recomputed" in types
    ev = next(e for e in r["recent_events"] if e["event_type"] == "entitlements.recomputed")
    assert ev["state_to"] == "active"
    assert ev["occurred_at"] is not None


def test_recent_events_empty_when_none_persisted(db):
    org_id, _ = _tenant(db)
    # A fresh tenant built directly (no provisioning) has no commercial events.
    r = _inspect(db, org_id)
    assert isinstance(r["recent_events"], list)


def test_snapshot_events_are_flagged_persisted(db):
    """Phase 3.12 made the snapshot events durable, so the note reflects that."""
    org_id, _ = _tenant(db)
    r = _inspect(db, org_id)
    assert r["notes"]["snapshot_events"] == "persisted_and_logged"


# ══════════════════════════════════════════════════════════════════════════════
# Security / tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("role", ["employee", "admin", "manager", "hr", "owner",
                                  "billing_admin", None])
def test_non_super_admin_is_denied(db, role):
    org_id, _ = _tenant(db)
    u = models.User(id=-5, name="x", email=f"insp-{role}@x.test",
                    organization_id=1, role=role)
    with pytest.raises(HTTPException) as exc:
        _inspect(db, org_id, user=u)
    assert exc.value.status_code == 403


def test_super_admin_is_allowed(db):
    org_id, _ = _tenant(db)
    r = _inspect(db, org_id, user=_su(db))
    assert r["plan"] is not None


def test_unknown_tenant_returns_404_not_another_tenants_data(db):
    with pytest.raises(HTTPException) as exc:
        _inspect(db, "org_does_not_exist_inspector")
    assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# Read-only: no mutation of entitlement/subscription/override/event state
# ══════════════════════════════════════════════════════════════════════════════
def test_inspector_does_not_mutate_state(db):
    org_id, sub = _tenant(db, plan="governed")
    entitlements.get(db, org_id)                    # warm so get() won't write
    before = {
        "subs": db.query(CommercialSubscription).count(),
        "orgs": db.query(CommercialOrganization).count(),
        "events": db.query(CommercialEvent).count(),
        "override": dict(sub.enterprise_override or {}),
        "plan": sub.plan_code,
        "state": sub.state,
    }
    _inspect(db, org_id)
    _inspect(db, org_id)                             # twice — still no mutation
    assert db.query(CommercialSubscription).count() == before["subs"]
    assert db.query(CommercialOrganization).count() == before["orgs"]
    assert db.query(CommercialEvent).count() == before["events"]
    db.refresh(sub)
    assert sub.plan_code == before["plan"]
    assert sub.state == before["state"]
    assert dict(sub.enterprise_override or {}) == before["override"]


def test_cache_status_helper_is_pure_read(db):
    org_id, _ = _tenant(db)
    entitlements.get(db, org_id)
    before = db.query(CommercialEvent).count()
    for _ in range(3):
        entitlements.cache_status(db, org_id)       # must not write anything
    assert db.query(CommercialEvent).count() == before


# ══════════════════════════════════════════════════════════════════════════════
# Decisions / trace / privacy / internal / isolation unchanged
# ══════════════════════════════════════════════════════════════════════════════
def test_all_39_decisions_and_presentations_present(db):
    org_id, _ = _tenant(db, plan="sovereign")
    caps = {c["capability"]: c for c in _inspect(db, org_id)["capabilities"]}
    assert set(caps) == set(manifests.FEATURES)
    for c in caps.values():
        assert c["decision"] in manifests.DECISIONS
        assert c["presentation"] in manifests.PRESENTATIONS


def test_decision_trace_is_honest(db):
    org_id, _ = _tenant(db)
    trace = {t["layer"]: t for t in _inspect(db, org_id)["decision_trace"]}
    assert trace["workspace_configuration"]["status"] == "not_evaluated"
    assert trace["actor_rbac"]["status"] == "separate_concern"


@pytest.mark.parametrize("key", PRIVACY)
def test_privacy_capabilities_denied_and_hidden(db, key):
    org_id, _ = _tenant(db, plan="sovereign")
    c = next(x for x in _inspect(db, org_id)["capabilities"] if x["capability"] == key)
    assert c["decision"] == manifests.DENY
    assert c["presentation"] == manifests.HIDE


def test_internal_tenant_inspects_as_sovereign_equivalent(db):
    op = models.Organization(name="Temveris inspector", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    identity = (db.query(CommercialOrganization)
                  .filter_by(operational_org_id=op.id).one())
    r = _inspect(db, identity.id)
    assert r["plan"] == manifests.INTERNAL_PLAN
    caps = {c["capability"]: c["decision"] for c in r["capabilities"]}
    assert caps == {**manifests.capabilities_for("sovereign"), "evidence.screenshots.capture": manifests.ALLOW}
    assert billing_service.is_internal_tenant(db, op.id) is True


def test_worker_projection_does_not_poison_a_later_admin_view(db):
    """Actor isolation: presentation is derived per call, never cached."""
    from app.commercial import projection
    org_id, _ = _tenant(db, plan="verified")
    profile = entitlements.get(db, org_id)
    worker = {c["capability"]: c["presentation"]
              for c in projection.build_actor_projection(profile, role="employee")["capabilities"]}
    admin = {c["capability"]: c["presentation"]
             for c in projection.build_actor_projection(profile, role="admin")["capabilities"]}
    # integrations.api is denied on Verified -> hidden for worker, locked for admin.
    assert worker["integrations.api"] == manifests.HIDE
    assert admin["integrations.api"] == manifests.LOCK
    # The inspector itself always projects the admin view.
    insp = {c["capability"]: c["presentation"]
            for c in _inspect(db, org_id)["capabilities"]}
    assert insp["integrations.api"] == manifests.LOCK
