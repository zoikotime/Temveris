"""Ops-center search / correlation resolver (audit §9)."""
from app.commercial import search, ids
from app.commercial.audit import emit_event
from app.commercial.models import (
    CommercialOrganization, CommercialWorkspace, CommercialSignup,
)


def _fixture(db):
    corr = ids.correlation_id()
    org_id = ids.org_id()
    ws_id = ids.workspace_id()
    db.add(CommercialOrganization(id=org_id, name="Globex", commercial_state="trial_active"))
    db.add(CommercialWorkspace(id=ws_id, organization_id=org_id, slug=ws_id, name="Globex WS",
                               provisioning_state="ready"))
    sg = CommercialSignup(id=ids.signup_id(), email="ops-search@example.com",
                          state="signup_complete", correlation_id=corr,
                          organization_id=org_id, workspace_id=ws_id)
    db.add(sg)
    db.flush()
    # two events on the same correlation → a timeline
    emit_event(db, event_type="signup.started", source_service="activation-orchestration",
               payload={}, correlation_id=corr, organization_id=org_id)
    emit_event(db, event_type="signup.completed", source_service="activation-orchestration",
               payload={}, correlation_id=corr, organization_id=org_id, workspace_id=ws_id)
    db.commit()
    return corr, org_id, ws_id, sg.id


def test_resolve_by_correlation_id_returns_ordered_timeline(db):
    corr, org_id, _, _ = _fixture(db)
    r = search.resolve(db, corr)
    assert r["kind"] == "correlation_id"
    assert r["correlation_id"] == corr
    types = [e["event_type"] for e in r["timeline"]]
    assert "signup.started" in types and "signup.completed" in types
    assert types.index("signup.started") < types.index("signup.completed")  # ordered asc


def test_resolve_by_support_reference(db):
    corr, _, _, _ = _fixture(db)
    support_ref = "ZT-" + corr.split("_", 1)[-1][:8].upper()
    r = search.resolve(db, support_ref)
    assert r["kind"] == "support_reference"
    assert r["correlation_id"] == corr
    assert len(r["timeline"]) >= 2


def test_resolve_by_email(db):
    _fixture(db)
    r = search.resolve(db, "ops-search@example.com")
    assert r["kind"] == "email"
    assert r["entity"]["type"] == "signup"
    assert len(r["timeline"]) >= 2


def test_resolve_by_org_id(db):
    _corr, org_id, _, _ = _fixture(db)
    r = search.resolve(db, org_id)
    assert r["kind"] == "organization"
    assert r["entity"]["name"] == "Globex"
    assert len(r["timeline"]) >= 2


def test_resolve_by_workspace_id(db):
    _c, org_id, ws_id, _ = _fixture(db)
    r = search.resolve(db, ws_id)
    assert r["kind"] == "workspace"
    assert r["entity"]["provisioning_state"] == "ready"
    assert len(r["timeline"]) >= 2  # falls back to org timeline


def test_trace_id_reports_not_persisted(db):
    r = search.resolve(db, "trc_abc123")
    assert r["kind"] == "trace_id"
    assert r["timeline"] == []
    assert "correlation" in (r["note"] or "").lower()


def test_unknown_returns_note_not_error(db):
    r = search.resolve(db, "org_does_not_exist")
    assert r["kind"] == "organization"
    assert r["entity"] is None
    assert r["note"]
