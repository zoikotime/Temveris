"""End-to-end idempotent signup through the HTTP layer (Build Contracts §7.1).

Proves the decorator + FastAPI DI + DB path together: a repeated
Idempotency-Key replays the original success and creates exactly one
user/signup — instead of the second request 409-ing on duplicate email.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database.models import User
from app.commercial.models import CommercialSignup
import app.commercial.routers.signup as signup_mod


@pytest.fixture
def client(engine, monkeypatch):
    # Don't send real welcome/verification email during the test.
    monkeypatch.setattr(signup_mod, "send_welcome_and_verification", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(signup_mod.router)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=True)


def _body(email):
    # Password must satisfy the platform policy (app.auth.service._PASSWORD_RULES),
    # which signup now enforces — it previously accepted any 8+ character string,
    # giving the platform two different password standards.
    return {
        "first_name": "A", "last_name": "B", "email": email,
        "password": "Passw0rd!23", "company_name": "Acme", "country": "US",
        "plan": "verified", "intent": "trial", "expected_users": 1,
        "terms_version": "v1", "privacy_version": "v1",
    }


def test_repeated_idempotency_key_replays_and_creates_one_user(client, db):
    email = "idem-e2e@example.com"
    headers = {"Idempotency-Key": "sgn-key-e2e-1"}
    body = _body(email)

    r1 = client.post("/api/v1/signup", json=body, headers=headers)
    r2 = client.post("/api/v1/signup", json=body, headers=headers)

    assert r1.status_code in (200, 201), r1.text
    # Second call is replayed, not re-run → identical body, not a 409 dup-email.
    assert r2.json() == r1.json()

    assert db.query(User).filter(User.email == email).count() == 1
    assert db.query(CommercialSignup).filter(CommercialSignup.email == email).count() == 1

    # Exactly one operational tenant, and the replay must not have minted a
    # second one. This is the property the idempotency key exists to protect
    # now that signup provisions the operational side too.
    from database.models import Organization
    user = db.query(User).filter(User.email == email).one()
    assert user.organization_id is not None, "signup left the admin org-less"
    assert db.query(Organization).filter(
        Organization.id == user.organization_id
    ).count() == 1
    assert r1.json()["operational_org_id"] == user.organization_id


def test_same_key_different_payload_rejected_e2e(client):
    headers = {"Idempotency-Key": "sgn-key-e2e-2"}
    client.post("/api/v1/signup", json=_body("first@example.com"), headers=headers)
    r = client.post("/api/v1/signup", json=_body("different@example.com"), headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
