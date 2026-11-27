"""Activation status read-model + endpoint (Phase 4 §1)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from app.commercial import activation_status, ids
from app.commercial.models import (
    CommercialSignup, CommercialWorkspace, CommercialOrganization,
)
import app.commercial.routers.signup as signup_mod
from app.commercial.routers.activation import router as activation_router


# ── read-model mapping ───────────────────────────────────────────────────────
def _seed(db, prov_state, *, sub_state="trial_active", signup_state="signup_complete", failure=None):
    org_id = ids.org_id()
    ws_id = ids.workspace_id()
    corr = ids.correlation_id()
    db.add(CommercialOrganization(id=org_id, name="Acme", commercial_state=sub_state))
    db.add(CommercialWorkspace(id=ws_id, organization_id=org_id, slug=ws_id, name="Acme WS",
                               provisioning_state=prov_state))
    sg = CommercialSignup(id=ids.signup_id(), email=f"{ws_id}@e.com", state=signup_state,
                          correlation_id=corr, organization_id=org_id, workspace_id=ws_id,
                          failure_code=failure)
    db.add(sg)
    db.commit()
    return sg.id, org_id, ws_id


def test_ready_is_100_percent(db):
    sid, _, _ = _seed(db, "ready")
    s = activation_status.compute(db, signup_id=sid)
    assert s["status"] == "ready"
    assert s["workspace_ready"] is True
    assert s["provisioning"]["percent_complete"] == 100
    assert s["provisioning"]["estimated_seconds_remaining"] is None
    assert s["support_reference"].startswith("ZT-")


def test_mid_progress_reports_step_and_partial_percent(db):
    sid, _, _ = _seed(db, "workspace_created", signup_state="workspace_pending")
    s = activation_status.compute(db, signup_id=sid)
    assert s["status"] == "in_progress"
    assert s["provisioning"]["current_step"] == "Creating workspace"
    assert 0 < s["provisioning"]["percent_complete"] < 100
    assert "Creating user" in s["provisioning"]["completed_steps"]
    assert s["provisioning"]["estimated_seconds_remaining"] > 0


def test_failed_retryable_offers_retry(db):
    sid, _, _ = _seed(db, "failed_retryable", signup_state="signup_failed", failure="PROVISIONING_FAILED")
    s = activation_status.compute(db, signup_id=sid)
    assert s["status"] == "failed"
    assert s["provisioning"]["retry_available"] is True
    assert s["provisioning"]["failure_code"] == "PROVISIONING_FAILED"


def test_failed_terminal_no_retry(db):
    sid, _, _ = _seed(db, "failed_terminal", signup_state="signup_failed")
    s = activation_status.compute(db, signup_id=sid)
    assert s["status"] == "failed"
    assert s["provisioning"]["retry_available"] is False


def test_unknown_id_raises(db):
    with pytest.raises(activation_status.ActivationNotFound):
        activation_status.compute(db, signup_id="sgn_does_not_exist")


# ── endpoint + e2e after a real signup ───────────────────────────────────────
@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(signup_mod, "send_welcome_and_verification", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(signup_mod.router)
    app.include_router(activation_router)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def test_status_endpoint_requires_an_identifier(client):
    r = client.get("/api/v1/activation/status")
    assert r.status_code == 400


def test_signup_then_status_ready_e2e(client):
    body = {
        "first_name": "A", "last_name": "B", "email": "act-status@example.com",
        # Must satisfy the platform password policy, which signup now enforces.
        "password": "Passw0rd!23", "company_name": "Acme", "country": "US",
        "plan": "verified", "intent": "trial", "expected_users": 1,
        "terms_version": "v1", "privacy_version": "v1",
    }
    r = client.post("/api/v1/signup", json=body)
    assert r.status_code in (200, 201), r.text
    signup_id = r.json()["signup_id"]

    s = client.get("/api/v1/activation/status", params={"signup_id": signup_id})
    assert s.status_code == 200, s.text
    data = s.json()
    # provisioning is inline → already ready by the time we poll
    assert data["status"] == "ready"
    assert data["workspace_ready"] is True
    assert data["provisioning"]["percent_complete"] == 100
    assert data["entitlements_ready"] is True
    assert data["workspace_url"] is not None
    assert data["trace_id"].startswith("trc_")
