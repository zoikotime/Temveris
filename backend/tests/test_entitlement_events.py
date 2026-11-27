"""Phase 3.10 — entitlement snapshot events (SPEC.txt §18).

Two events, emitted from the resolver's write-through, keyed on the content hash
(never on timestamps or cache metadata):

  entitlements.snapshot.created — a new effective snapshot was persisted
  entitlements.changed          — an existing snapshot's hash differed

Logging-only, and deliberately isolated: emission runs in the resolver hot path,
so it must never fail a request or change a decision. These tests pin the
semantics, the payload safety, and the failure isolation.
"""
import json
import logging

import pytest

from app.billing import service as billing_service
from app.commercial import entitlements, ids, manifests
from app.commercial.bootstrap import ensure_internal_identity
from app.commercial.models import CommercialOrganization, CommercialSubscription
from database import models

EVENTS_LOGGER = "zoiko.entitlement.events"
CREATED = entitlements.EVENT_SNAPSHOT_CREATED
CHANGED = entitlements.EVENT_CHANGED

# The only fields allowed in an event payload. Nothing here is a secret or PII.
ALLOWED_FIELDS = {"event", "organization_id", "plan", "commercial_state",
                  "manifest_version", "snapshot_hash", "previous_hash", "at"}


def _org(db, *, plan="verified", state="active", country="US", billing_type="customer"):
    op = models.Organization(name=f"ev {ids.org_id()[:8]}", billing_type=billing_type)
    db.add(op)
    db.flush()
    org_id = ids.org_id()
    db.add(CommercialOrganization(
        id=org_id, name=op.name, country=country, commercial_state=state,
        operational_org_id=op.id))
    sub = None
    if plan is not None:
        sub = CommercialSubscription(
            id=ids.subscription_id(), organization_id=org_id, plan_code=plan,
            billing_cycle="monthly", seat_count=5, state=state, provider="stub")
        db.add(sub)
    db.flush()
    return org_id, sub


def _events(caplog):
    """Parsed event payloads captured on the entitlement events logger."""
    out = []
    for rec in caplog.records:
        if rec.name != EVENTS_LOGGER:
            continue
        msg = rec.getMessage()
        # "entitlements.snapshot.created {json}"
        _, _, blob = msg.partition(" ")
        try:
            out.append(json.loads(blob))
        except json.JSONDecodeError:
            pass
    return out


def _types(caplog):
    return [e["event"] for e in _events(caplog)]


# ══════════════════════════════════════════════════════════════════════════════
# snapshot.created
# ══════════════════════════════════════════════════════════════════════════════
def test_first_compute_emits_snapshot_created(db, caplog):
    org_id, _ = _org(db)
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        entitlements.compute(db, org_id)
    assert _types(caplog) == [CREATED]


def test_snapshot_created_carries_the_new_hash(db, caplog):
    org_id, _ = _org(db, plan="governed")
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        profile = entitlements.compute(db, org_id)
    ev = _events(caplog)[0]
    assert ev["event"] == CREATED
    assert ev["snapshot_hash"] == profile["snapshot_hash"]
    assert ev["previous_hash"] is None
    assert ev["plan"] == "governed"


def test_post_invalidation_recompute_emits_created_not_changed(db, caplog):
    """A plan change routes through invalidate (row dropped), so the new grant
    surfaces as a freshly created snapshot."""
    org_id, sub = _org(db, plan="verified")
    entitlements.compute(db, org_id)                    # seed
    sub.plan_code = "governed"
    db.flush()
    entitlements.invalidate(db, org_id)
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        entitlements.compute(db, org_id)
    assert _types(caplog) == [CREATED]


# ══════════════════════════════════════════════════════════════════════════════
# entitlements.changed / no false positives
# ══════════════════════════════════════════════════════════════════════════════
def test_identical_recompute_emits_nothing(db, caplog):
    """Same inputs -> same hash -> no event. Not TTL, not computed_at, not a
    re-run of the resolver triggers a false changed."""
    org_id, _ = _org(db)
    entitlements.compute(db, org_id)                    # seed the row
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        entitlements.compute(db, org_id)
        entitlements.compute(db, org_id)
    assert _events(caplog) == []


def test_content_change_without_invalidation_emits_changed(db, caplog):
    """Row still present + different effective content -> created + changed. This
    is the manifest-deploy-on-TTL-refresh shape."""
    org_id, sub = _org(db, plan="verified")
    entitlements.compute(db, org_id)                    # seed
    sub.plan_code = "governed"                          # content differs, no invalidate
    db.flush()
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        entitlements.compute(db, org_id)
    assert _types(caplog) == [CREATED, CHANGED]
    changed = next(e for e in _events(caplog) if e["event"] == CHANGED)
    assert changed["previous_hash"] is not None
    assert changed["snapshot_hash"] != changed["previous_hash"]


def test_changed_reports_the_hash_transition(db, caplog):
    org_id, sub = _org(db, plan="verified")
    first = entitlements.compute(db, org_id)
    sub.plan_code = "sovereign"
    db.flush()
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        second = entitlements.compute(db, org_id)
    changed = next(e for e in _events(caplog) if e["event"] == CHANGED)
    assert changed["previous_hash"] == first["snapshot_hash"]
    assert changed["snapshot_hash"] == second["snapshot_hash"]


def test_manifest_version_is_reported(db, caplog):
    org_id, _ = _org(db, plan="sovereign")
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        profile = entitlements.compute(db, org_id)
    assert _events(caplog)[0]["manifest_version"] == profile["manifest_version"]


# ══════════════════════════════════════════════════════════════════════════════
# Payload safety (§18.1 / Phase 6)
# ══════════════════════════════════════════════════════════════════════════════
def test_payload_contains_only_approved_fields(db, caplog):
    org_id, _ = _org(db)
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        entitlements.compute(db, org_id)
    for ev in _events(caplog):
        assert set(ev) == ALLOWED_FIELDS, set(ev) ^ ALLOWED_FIELDS


def test_payload_has_no_secrets_or_pii(db, caplog):
    org_id, _ = _org(db)
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        entitlements.compute(db, org_id)
    blob = json.dumps(_events(caplog)).lower()
    for banned in ("password", "token", "secret", "stripe", "email", "card", "cvv"):
        assert banned not in blob, banned


# ══════════════════════════════════════════════════════════════════════════════
# Failure safety (Phase 8) — emission can never alter resolution
# ══════════════════════════════════════════════════════════════════════════════
def test_emission_failure_does_not_break_resolution(db, monkeypatch):
    def boom(*a, **k):
        raise IOError("log sink down")

    monkeypatch.setattr(entitlements._events_log, "info", boom)
    org_id, _ = _org(db, plan="governed")
    profile = entitlements.compute(db, org_id)          # must not raise
    assert profile["plan"] == "governed"
    assert profile["capabilities"] == {**manifests.capabilities_for("governed"), "evidence.screenshots.capture": manifests.ALLOW}


def test_emission_failure_preserves_privacy_deny(db, monkeypatch):
    monkeypatch.setattr(entitlements._events_log, "info",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    org_id, _ = _org(db, plan="sovereign")
    profile = entitlements.compute(db, org_id)
    for key in manifests.GLOBALLY_DENIED:
        assert profile["capabilities"][key] == manifests.DENY


def test_helper_never_raises_on_bad_input(db):
    # None hashes, None manifest — must be silently tolerated.
    entitlements._emit_snapshot_events(
        db, organization_id="x", plan="verified", state="active",
        manifest_version=None, old_hash=None, new_hash=None)


def test_helper_never_raises_even_with_a_broken_session():
    """A None session must not raise out of the resolver (savepoint guarded)."""
    entitlements._emit_snapshot_events(
        None, organization_id="x", plan="verified", state="active",
        manifest_version=None, old_hash=None, new_hash="h")


# ══════════════════════════════════════════════════════════════════════════════
# Unchanged behaviour
# ══════════════════════════════════════════════════════════════════════════════
def test_internal_tenant_emits_and_stays_billing_inert(db, caplog):
    op = models.Organization(name="Temveris events", billing_type="internal")
    db.add(op)
    db.commit()
    ensure_internal_identity()
    identity = (db.query(CommercialOrganization)
                  .filter_by(operational_org_id=op.id).one())
    entitlements.invalidate(db, identity.id)
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        profile = entitlements.compute(db, identity.id)
    assert _types(caplog) == [CREATED]
    assert profile["plan"] == manifests.INTERNAL_PLAN
    assert billing_service.is_internal_tenant(db, op.id) is True
    with pytest.raises(Exception):
        billing_service.require_billable_tenant(db, op.id)


def test_events_are_durably_persisted(db, caplog):
    """Phase 3.12: the snapshot events are now recorded on commercial_events, in
    addition to the structured log. (Superseded the 3.10 logging-only pinning.)"""
    from app.commercial.models import CommercialEvent
    org_id, _ = _org(db)
    before = db.query(CommercialEvent).filter(
        CommercialEvent.event_type.in_([CREATED, CHANGED])).count()
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        entitlements.compute(db, org_id)
    rows = db.query(CommercialEvent).filter(
        CommercialEvent.event_type.in_([CREATED, CHANGED])).all()
    assert len(rows) == before + 1
    assert rows[-1].event_type == CREATED
    # persisted AND logged — the log payload still fires
    assert CREATED in _types(caplog)


# ══════════════════════════════════════════════════════════════════════════════
# Durable persistence (Phase 3.12)
# ══════════════════════════════════════════════════════════════════════════════
def _persisted(db, org_id):
    from app.commercial.models import CommercialEvent
    return (db.query(CommercialEvent)
              .filter(CommercialEvent.organization_id == org_id,
                      CommercialEvent.event_type.in_([CREATED, CHANGED]))
              .order_by(CommercialEvent.id).all())


def test_snapshot_created_persisted_once_on_first_compute(db):
    org_id, _ = _org(db, plan="governed")
    entitlements.compute(db, org_id)
    rows = _persisted(db, org_id)
    assert [r.event_type for r in rows] == [CREATED]
    assert rows[0].organization_id == org_id
    assert rows[0].source_service == "entitlement_resolver"
    assert rows[0].state_to == "active"


def test_identical_recompute_persists_nothing_new(db):
    org_id, _ = _org(db)
    entitlements.compute(db, org_id)
    entitlements.compute(db, org_id)
    entitlements.compute(db, org_id)
    assert [r.event_type for r in _persisted(db, org_id)] == [CREATED]


def test_content_change_persists_created_and_changed(db):
    org_id, sub = _org(db, plan="verified")
    entitlements.compute(db, org_id)                    # seed
    sub.plan_code = "governed"                          # differ, no invalidate
    db.flush()
    entitlements.compute(db, org_id)
    kinds = [r.event_type for r in _persisted(db, org_id)]
    assert kinds == [CREATED, CREATED, CHANGED]


def test_post_invalidation_persists_created(db):
    org_id, sub = _org(db, plan="verified")
    entitlements.compute(db, org_id)
    sub.plan_code = "sovereign"
    db.flush()
    entitlements.invalidate(db, org_id)
    entitlements.compute(db, org_id)
    assert [r.event_type for r in _persisted(db, org_id)] == [CREATED, CREATED]


def test_persisted_payload_has_only_approved_fields(db):
    org_id, _ = _org(db, plan="sovereign")
    entitlements.compute(db, org_id)
    row = _persisted(db, org_id)[0]
    assert set(row.payload) == ALLOWED_FIELDS
    blob = json.dumps(row.payload).lower()
    for banned in ("password", "token", "secret", "stripe", "email", "card"):
        assert banned not in blob


def test_events_are_isolated_by_tenant(db):
    a, _ = _org(db, plan="verified")
    b, _ = _org(db, plan="governed")
    entitlements.compute(db, a)
    entitlements.compute(db, b)
    a_rows = _persisted(db, a)
    b_rows = _persisted(db, b)
    assert all(r.organization_id == a for r in a_rows)
    assert all(r.organization_id == b for r in b_rows)
    assert a not in [r.organization_id for r in b_rows]


def test_persistence_failure_does_not_break_compute(db, monkeypatch):
    import app.commercial.audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("event sink down")

    monkeypatch.setattr(audit_mod, "emit_event", boom)
    org_id, _ = _org(db, plan="governed")
    profile = entitlements.compute(db, org_id)          # must not raise
    assert profile["plan"] == "governed"
    assert profile["capabilities"] == {**manifests.capabilities_for("governed"), "evidence.screenshots.capture": manifests.ALLOW}


def test_persistence_failure_preserves_cache_row(db, monkeypatch):
    from app.commercial.models import EntitlementsCache
    import app.commercial.audit as audit_mod
    monkeypatch.setattr(audit_mod, "emit_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    org_id, _ = _org(db, plan="sovereign")
    entitlements.compute(db, org_id)
    # The savepoint rolled back the event insert, not the cache write.
    row = db.query(EntitlementsCache).filter_by(organization_id=org_id).one()
    assert (row.profile_json or {}).get("snapshot_hash") is not None


def test_persistence_failure_preserves_privacy_deny(db, monkeypatch):
    import app.commercial.audit as audit_mod
    monkeypatch.setattr(audit_mod, "emit_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    org_id, _ = _org(db, plan="sovereign")
    profile = entitlements.compute(db, org_id)
    for key in manifests.GLOBALLY_DENIED:
        assert profile["capabilities"][key] == manifests.DENY


def test_snapshot_events_are_registered_event_types():
    from app.commercial import constants
    assert CREATED in constants.VALID_EVENT_TYPES
    assert CHANGED in constants.VALID_EVENT_TYPES


def test_inspector_shows_persisted_snapshot_events(db):
    """The Admin Inspector's recent-events panel now surfaces them durably."""
    from app.commercial.routers import admin as admin_router
    org_id, _ = _org(db, plan="governed")
    entitlements.compute(db, org_id)                    # persists a created event
    su = models.User(id=-880, name="su", email="ev-su@x.test",
                     organization_id=1, role="super_admin")
    r = admin_router.entitlement_inspector(org_id, db=db, current_user=su)
    types = [e["event_type"] for e in r["recent_events"]]
    assert CREATED in types


def test_unresolvable_tenant_emits_no_event(db, caplog):
    """An unknown org returns the empty profile early, before the write-through,
    so nothing is persisted and no snapshot event fires — correct, because no
    snapshot was actually created for a tenant that does not exist."""
    with caplog.at_level(logging.INFO, logger=EVENTS_LOGGER):
        profile = entitlements.compute(db, "org_does_not_exist_events")
    assert _events(caplog) == []
    # ...and the caller still gets a usable fail-closed profile.
    assert profile["commercial_state"] == "manual_review"
