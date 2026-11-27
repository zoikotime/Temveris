"""HTTP integration tests for the Kairos chatbot module.

Verifies the in-process FastAPI port that replaced the standalone Node
KAIROS-BE service. Hits every endpoint via TestClient against the shared
in-memory SQLite DB built by conftest (which uses StaticPool so the
multi-threaded TestClient sees a consistent connection).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def kairos_app(engine):
    """Add the kairos tables to the conftest engine, then import main."""
    from app.kairos import models as kmodels

    for t in (
        kmodels.KairosConversation,
        kmodels.KairosChat,
        kmodels.KairosNewPrompt,
        kmodels.KairosEmailRateLimit,
    ):
        t.__table__.create(engine, checkfirst=True)

    import main
    return main.app


@pytest.fixture(scope="module")
def users(kairos_app, engine):
    from sqlalchemy.orm import sessionmaker
    from database import models as legacy
    from app.auth.service import create_access_token

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = SessionLocal()
    employee = legacy.User(
        name="Alice", email="alice-kairos@example.com", password="x",
        role="employee", is_active=True,
    )
    admin = legacy.User(
        name="Admin", email="admin-kairos@example.com", password="x",
        role="admin", is_active=True,
    )
    s.add_all([employee, admin]); s.commit(); s.refresh(employee); s.refresh(admin)
    s.close()

    return {
        "employee":  employee,
        "admin":     admin,
        "emp_token": create_access_token({"user_id": employee.id}),
        "adm_token": create_access_token({"user_id": admin.id}),
    }


@pytest.fixture
def client(kairos_app):
    return TestClient(kairos_app)


@pytest.fixture
def H_emp(users):
    return {"Authorization": f"Bearer {users['emp_token']}"}


@pytest.fixture
def H_adm(users):
    return {"Authorization": f"Bearer {users['adm_token']}"}


# ── Tests ─────────────────────────────────────────────────────────────────

def test_context_unauth(client):
    r = client.get("/api/v1/kairos/chat/context")
    assert r.status_code == 200
    ctx = r.json()["context"]
    assert ctx["assistantName"]
    assert isinstance(ctx["defaultSuggestions"], list)


def test_chat_requires_auth(client):
    r = client.post("/api/v1/kairos/chat", json={"message": "hi"})
    assert r.status_code == 401


def test_chat_creates_session_on_first_message(client, H_emp):
    r = client.post("/api/v1/kairos/chat", headers=H_emp, json={"message": "hi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sessionId"]
    assert body["message"]["intent"]


def test_chat_reuses_session_when_id_provided(client, H_emp):
    r1 = client.post("/api/v1/kairos/chat", headers=H_emp, json={"message": "hello"})
    sid = r1.json()["sessionId"]
    r2 = client.post(
        "/api/v1/kairos/chat", headers=H_emp,
        json={"message": "follow-up", "sessionId": sid},
    )
    assert r2.status_code == 200
    assert r2.json()["sessionId"] == sid


def test_session_list_and_history(client, H_emp):
    sid = client.post(
        "/api/v1/kairos/chat", headers=H_emp, json={"message": "history-test"}
    ).json()["sessionId"]

    sessions = client.get("/api/v1/kairos/chat/sessions", headers=H_emp).json()["sessions"]
    assert any(s["sessionId"] == sid for s in sessions)

    msgs = client.get(f"/api/v1/kairos/chat/history/{sid}", headers=H_emp).json()["messages"]
    assert len(msgs) >= 2


def test_history_blocks_other_users(client, H_emp, H_adm):
    sid = client.post(
        "/api/v1/kairos/chat", headers=H_emp, json={"message": "private"}
    ).json()["sessionId"]
    r = client.get(f"/api/v1/kairos/chat/history/{sid}", headers=H_adm)
    assert r.status_code == 403


def test_new_prompts_admin_only(client, H_emp, H_adm):
    assert client.get("/api/v1/kairos/chat/new-prompts", headers=H_emp).status_code == 403
    assert client.get("/api/v1/kairos/chat/new-prompts", headers=H_adm).status_code == 200


def test_mail_status_returns_quota(client, H_emp):
    r = client.get("/api/v1/kairos/mail/status", headers=H_emp)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["limit"] == 5
    assert body["allowed"] is True


def test_escalate_is_placeholder(client, H_emp):
    r = client.post("/api/v1/kairos/escalate", headers=H_emp)
    assert r.status_code == 501


def test_end_and_delete_session(client, H_emp):
    sid = client.post(
        "/api/v1/kairos/chat", headers=H_emp, json={"message": "throwaway"}
    ).json()["sessionId"]

    assert client.patch(f"/api/v1/kairos/chat/sessions/{sid}/end",
                        headers=H_emp).status_code == 200
    assert client.delete(f"/api/v1/kairos/chat/sessions/{sid}",
                        headers=H_emp).status_code == 200

    sessions = client.get("/api/v1/kairos/chat/sessions", headers=H_emp).json()["sessions"]
    assert all(s["sessionId"] != sid for s in sessions)


def test_chat_rate_limit_per_user(client, engine):
    """20/min/user — a fresh user (no prior calls) makes exactly 20
    successes in a burst; the 21st through 25th return 429."""
    from sqlalchemy.orm import sessionmaker
    from database import models as legacy
    from app.auth.service import create_access_token
    from app.kairos.service import _rate_state, _rate_lock

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = SessionLocal()
    fresh = legacy.User(
        name="RateUser", email="rate-kairos@example.com", password="x",
        role="employee", is_active=True,
    )
    s.add(fresh); s.commit(); s.refresh(fresh)
    s.close()

    with _rate_lock:
        _rate_state.pop("rate-kairos@example.com", None)

    H = {"Authorization": f"Bearer {create_access_token({'user_id': fresh.id})}"}
    codes = [
        client.post("/api/v1/kairos/chat", headers=H, json={"message": "hi"}).status_code
        for _ in range(25)
    ]
    n_200 = sum(1 for c in codes if c == 200)
    n_429 = sum(1 for c in codes if c == 429)
    assert n_200 == 20, f"expected 20 successes, got {n_200} (codes: {codes})"
    assert n_429 == 5,  f"expected 5 throttled, got {n_429} (codes: {codes})"
