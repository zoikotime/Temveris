"""End-to-end self-service journey, through HTTP, on one app.

Everything else tests a slice. This walks the whole thing the way a customer
does, with all the real routers mounted together so the seams between them are
exercised too:

    POST /api/v1/signup                     create the tenant, get a session
    GET  /api/v1/onboarding/setup           wizard opens at step 1
    PUT  /org/settings                      step 1 — organization
    POST /api/v1/onboarding/setup/step      advance
    POST /org/teams                         step 2 — teams
    POST /api/v1/onboarding/setup/complete  step 3 — finish
    POST /employees/invite                  invite a teammate
    POST /auth/set-password                 teammate activates
    POST /auth/login-json                   both accounts sign in

Then the properties that matter across the whole journey: the invited employee
lands in the founder's organization, a second tenant cannot see the first, and
an existing (super-admin-provisioned) tenant is never routed into the wizard.

Every request after signup is authenticated with the token signup issued — no
token is fabricated, and no organization id is ever sent by the client.
"""

import os
import sys
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from database import database as _db          # noqa: E402
from database import models                   # noqa: E402
from app.commercial import models as cm       # noqa: E402
import app.commercial.routers.signup as signup_mod   # noqa: E402

STRONG_PASSWORD = "Journey!23aB"


@pytest.fixture
def app_env(monkeypatch):
    """One FastAPI app with every router the journey touches."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    prev_engine, prev_session = _db.engine, _db.SessionLocal
    _db.engine = eng
    _db.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)

    for t in (
        models.Organization, models.User, models.OrgSettings, models.AppSettings,
        models.LeavePolicy, models.Team, models.AuditLog, models.UserSession,
        models.RefreshToken, models.ClockSession, models.Activity,
        models.BreakSession, models.PasswordResetOTP,
        cm.CommercialOrganization, cm.CommercialWorkspace, cm.CommercialSubscription,
        cm.CommercialSignup, cm.CommercialEvent, cm.CommercialConsent,
        cm.EntitlementsCache, cm.FraudSignal, cm.ManualReviewCase,
        cm.EmailVerificationToken, cm.SignupRateBucket, cm.OnboardingChecklist,
        cm.CommercialIdempotencyKey,
    ):
        t.__table__.create(eng, checkfirst=True)

    # No outbound email anywhere in the journey.
    monkeypatch.setattr(signup_mod, "send_welcome_and_verification", lambda *a, **k: None)
    import app.email_service as es
    monkeypatch.setattr(es, "send_invite_email", lambda **k: True)

    # Abuse controls are asserted in their own module; here they would only
    # throttle the journey itself (one IP, one domain).
    signup_mod.limiter.enabled = False

    from app.auth.service import router as auth_router
    from app.employees.router import router as employees_router
    from app.org.router import router as org_router
    from app.commercial.routers.onboarding import router as onboarding_router

    app = FastAPI()
    app.state.limiter = signup_mod.limiter
    for r in (signup_mod.router, auth_router, employees_router, org_router,
              onboarding_router):
        app.include_router(r)

    TestingSessionLocal = _db.SessionLocal

    from database.database import get_db

    def _get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    client = TestClient(app, raise_server_exceptions=True)

    # The user-identity cache would otherwise serve a stale pre-invite view of a
    # user across requests inside one test.
    from app.auth import service as auth_service
    auth_service._USER_CACHE.clear()

    session = TestingSessionLocal()
    try:
        yield client, session
    finally:
        session.close()
        signup_mod.limiter.enabled = True
        auth_service._USER_CACHE.clear()
        _db.engine, _db.SessionLocal = prev_engine, prev_session


def _signup_body(email, company, **over):
    body = {
        "first_name": "Ada", "last_name": "Lovelace", "email": email,
        "password": STRONG_PASSWORD, "company_name": company,
        "country": "IN", "plan": "verified", "intent": "trial",
        "expected_users": 5,
        "terms_version": "v1", "privacy_version": "v1",
        "client_context": {"locale": "en-IN", "timezone": "Asia/Kolkata"},
    }
    body.update(over)
    return body


def _auth(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ═════════════════════════════════════════════════════════════════════════════

class TestFullJourney:

    def test_signup_to_invited_teammate(self, app_env):
        client, db = app_env

        # ── 1. Signup ────────────────────────────────────────────────────
        r = client.post("/api/v1/signup",
                        json=_signup_body("ada@enginehouse.example.com", "Engine House"))
        assert r.status_code in (200, 201), r.text
        signup = r.json()

        token = signup["access_token"]
        assert token, "signup did not issue a session"
        org_id = signup["operational_org_id"]
        assert org_id is not None
        assert signup["setup_required"] is True
        assert signup["role"] == "admin"

        # ── 2. Wizard opens at step 1 ────────────────────────────────────
        r = client.get("/api/v1/onboarding/setup", headers=_auth(token))
        assert r.status_code == 200, r.text
        state = r.json()
        assert state["setup_required"] is True
        assert state["current_step"] == "organization"
        # Signup's company name reached the operational settings row, which is
        # what the product UI reads.
        assert state["organization"]["company_name"] == "Engine House"
        assert state["organization"]["id"] == org_id
        assert state["seats"]["used"] == 1
        assert state["seats"]["limit"] == 25          # verified plan
        assert state["teams"]["count"] == 0
        assert state["teams"]["limit"] is None        # no cap sold today

        # ── 3. Step 1 — organization (existing API) ──────────────────────
        r = client.put("/org/settings", headers=_auth(token), json={
            "company_name": "Engine House Ltd",
            "default_timezone": "Asia/Kolkata",
            "primary_country": "IN",
        })
        assert r.status_code == 200, r.text

        r = client.post("/api/v1/onboarding/setup/step", headers=_auth(token),
                        json={"step": "teams"})
        assert r.status_code == 200, r.text
        assert r.json()["current_step"] == "teams"

        # ── 4. Step 2 — teams (existing API) ─────────────────────────────
        for name in ("Engineering", "Operations"):
            r = client.post("/org/teams", headers=_auth(token), json={"name": name})
            assert r.status_code == 200, r.text

        r = client.post("/api/v1/onboarding/setup/step", headers=_auth(token),
                        json={"step": "complete"})
        assert r.status_code == 200, r.text
        assert r.json()["completed"] is True

        # ── 5. Step 3 — finish ───────────────────────────────────────────
        r = client.post("/api/v1/onboarding/setup/complete", headers=_auth(token))
        assert r.status_code == 200, r.text
        done = r.json()
        assert done["completed"] is True
        assert done["setup_required"] is False
        assert done["teams"]["count"] == 2
        assert done["organization"]["company_name"] == "Engine House Ltd"

        # ── 6. Invite a teammate (existing API) ──────────────────────────
        r = client.post("/employees/invite", headers=_auth(token),
                        json={"email": "grace@enginehouse.example.com",
                              "name": "Grace Hopper"})
        assert r.status_code == 200, r.text
        invitee_id = r.json()["id"]

        invitee = db.query(models.User).filter_by(id=invitee_id).one()
        # The property the whole tenancy fix exists for.
        assert invitee.organization_id == org_id
        assert invitee.role == "employee"
        assert invitee.is_password_set is False
        invite_token = invitee.invite_token
        assert invite_token

        # ── 7. Teammate activates ────────────────────────────────────────
        r = client.get(f"/auth/invite/verify/{invite_token}")
        assert r.status_code == 200 and r.json()["valid"] is True

        r = client.post("/auth/set-password", json={
            "token": invite_token, "password": STRONG_PASSWORD, "name": "Grace Hopper",
        })
        assert r.status_code == 200, r.text

        db.expire_all()
        invitee = db.query(models.User).filter_by(id=invitee_id).one()
        assert invitee.is_password_set is True
        assert invitee.organization_id == org_id, "activation moved the employee's org"

        # ── 8. Both accounts can sign in ─────────────────────────────────
        for email, expected_role in (
            ("ada@enginehouse.example.com", "admin"),
            ("grace@enginehouse.example.com", "employee"),
        ):
            r = client.post("/auth/login-json",
                            json={"email": email, "password": STRONG_PASSWORD})
            assert r.status_code == 200, r.text
            u = r.json()["user"]
            assert u["role"] == expected_role
            assert u["organization_id"] == org_id

        # ── 9. The founder's dashboard view is org-scoped ────────────────
        r = client.get("/employees", headers=_auth(token))
        assert r.status_code == 200, r.text
        emails = {e["email"] for e in r.json()}
        assert emails == {"ada@enginehouse.example.com", "grace@enginehouse.example.com"}

    def test_two_journeys_stay_isolated(self, app_env):
        """Two self-serve tenants provisioned side by side must not see each other."""
        client, db = app_env

        a = client.post("/api/v1/signup",
                        json=_signup_body("a@alpha.example.com", "Alpha Ltd")).json()
        b = client.post("/api/v1/signup",
                        json=_signup_body("b@beta.example.com", "Beta Ltd")).json()

        assert a["operational_org_id"] != b["operational_org_id"]

        client.post("/employees/invite", headers=_auth(a["access_token"]),
                    json={"email": "a-emp@alpha.example.com"})
        client.post("/employees/invite", headers=_auth(b["access_token"]),
                    json={"email": "b-emp@beta.example.com"})
        client.post("/org/teams", headers=_auth(a["access_token"]),
                    json={"name": "Alpha Team"})
        client.post("/org/teams", headers=_auth(b["access_token"]),
                    json={"name": "Beta Team"})

        a_emails = {e["email"] for e in
                    client.get("/employees", headers=_auth(a["access_token"])).json()}
        b_emails = {e["email"] for e in
                    client.get("/employees", headers=_auth(b["access_token"])).json()}
        assert a_emails == {"a@alpha.example.com", "a-emp@alpha.example.com"}
        assert b_emails == {"b@beta.example.com", "b-emp@beta.example.com"}
        assert not (a_emails & b_emails)

        a_teams = {t["name"] for t in
                   client.get("/org/teams", headers=_auth(a["access_token"])).json()}
        b_teams = {t["name"] for t in
                   client.get("/org/teams", headers=_auth(b["access_token"])).json()}
        assert a_teams == {"Alpha Team"}
        assert b_teams == {"Beta Team"}

        a_settings = client.get("/org/settings", headers=_auth(a["access_token"])).json()
        assert a_settings["company_name"] == "Alpha Ltd"

    def test_cross_tenant_invite_of_an_active_account_is_refused(self, app_env):
        """One tenant must not be able to graft another's live account."""
        client, db = app_env

        a = client.post("/api/v1/signup",
                        json=_signup_body("owner-a@one.example.com", "One Ltd")).json()
        b = client.post("/api/v1/signup",
                        json=_signup_body("owner-b@two.example.com", "Two Ltd")).json()

        r = client.post("/employees/invite", headers=_auth(b["access_token"]),
                        json={"email": "owner-a@one.example.com"})
        assert r.status_code == 409, r.text

        db.expire_all()
        victim = db.query(models.User).filter_by(email="owner-a@one.example.com").one()
        assert victim.organization_id == a["operational_org_id"]
        assert victim.role == "admin"


class TestWizardResumability:

    def test_progress_survives_a_reload_and_cannot_rewind(self, app_env):
        client, db = app_env
        s = client.post("/api/v1/signup",
                        json=_signup_body("resume@resume.example.com", "Resume Ltd")).json()
        h = _auth(s["access_token"])

        client.post("/api/v1/onboarding/setup/step", headers=h, json={"step": "teams"})

        # "Reload": a fresh GET resumes where the server says, not where the
        # client last was.
        assert client.get("/api/v1/onboarding/setup", headers=h).json()["current_step"] == "teams"

        # "Back button": a stale client re-posting step 1 must not rewind.
        r = client.post("/api/v1/onboarding/setup/step", headers=h,
                        json={"step": "organization"})
        assert r.json()["current_step"] == "teams"

    def test_completion_is_idempotent_across_duplicate_submits(self, app_env):
        client, db = app_env
        s = client.post("/api/v1/signup",
                        json=_signup_body("dupe@dupe.example.com", "Dupe Ltd")).json()
        h = _auth(s["access_token"])

        first = client.post("/api/v1/onboarding/setup/complete", headers=h).json()
        second = client.post("/api/v1/onboarding/setup/complete", headers=h).json()
        assert first["completed"] is second["completed"] is True
        assert first["completed_at"] == second["completed_at"]

        # And it stays finished on reload — the wizard is never shown again.
        assert client.get("/api/v1/onboarding/setup", headers=h).json()["setup_required"] is False

    def test_unknown_step_is_rejected(self, app_env):
        client, db = app_env
        s = client.post("/api/v1/signup",
                        json=_signup_body("bad@bad.example.com", "Bad Ltd")).json()
        r = client.post("/api/v1/onboarding/setup/step",
                        headers=_auth(s["access_token"]), json={"step": "teleport"})
        assert r.status_code == 400

    def test_employee_cannot_drive_the_wizard(self, app_env):
        client, db = app_env
        s = client.post("/api/v1/signup",
                        json=_signup_body("boss@gate.example.com", "Gate Ltd")).json()
        h = _auth(s["access_token"])
        client.post("/api/v1/onboarding/setup/complete", headers=h)

        r = client.post("/employees/invite", headers=h, json={"email": "worker@gate.example.com"})
        emp = db.query(models.User).filter_by(id=r.json()["id"]).one()
        client.post("/auth/set-password", json={
            "token": emp.invite_token, "password": STRONG_PASSWORD, "name": "Worker",
        })
        login = client.post("/auth/login-json", json={
            "email": "worker@gate.example.com", "password": STRONG_PASSWORD,
        }).json()

        r = client.post("/api/v1/onboarding/setup/step",
                        headers=_auth(login["access_token"]), json={"step": "teams"})
        assert r.status_code == 403


class TestExistingTenantCompatibility:

    def test_super_admin_provisioned_tenant_skips_the_wizard_and_works(self, app_env, monkeypatch):
        """A contract tenant must behave exactly as before: no wizard, and no
        seat block despite having no subscription row."""
        client, db = app_env
        from app.org.provisioning_service import provision_tenant, CREATED_VIA_SUPER_ADMIN

        actor = models.User(name="Platform", email="platform@temveris.example",
                            role="super_admin", organization_id=None, is_active=True,
                            is_password_set=True)
        db.add(actor)
        db.flush()
        result = provision_tenant(
            db, org_name="Contract Corp", admin_email="admin@contract.example.com",
            created_via=CREATED_VIA_SUPER_ADMIN, actor_user_id=actor.id,
        )
        db.commit()

        # Activate through the invite flow, as a real admin would.
        r = client.post("/auth/set-password", json={
            "token": result.invite_token, "password": STRONG_PASSWORD, "name": "Contract Admin",
        })
        assert r.status_code == 200, r.text

        login = client.post("/auth/login-json", json={
            "email": "admin@contract.example.com", "password": STRONG_PASSWORD,
        }).json()
        h = _auth(login["access_token"])

        # No wizard — the tenant never called begin().
        state = client.get("/api/v1/onboarding/setup", headers=h).json()
        assert state["setup_required"] is False

        # And inviting still works, even though there is no subscription for a
        # seat limit to be resolved from.
        r = client.post("/employees/invite", headers=h,
                        json={"email": "hire@contract.example.com"})
        assert r.status_code == 200, r.text
        hire = db.query(models.User).filter_by(email="hire@contract.example.com").one()
        assert hire.organization_id == result.organization.id


class TestSeatEnforcementInTheJourney:

    def test_seat_limit_blocks_the_invite_that_would_exceed_it(self, app_env):
        client, db = app_env
        s = client.post("/api/v1/signup",
                        json=_signup_body("cap@cap.example.com", "Cap Ltd")).json()
        h = _auth(s["access_token"])
        org_id = s["operational_org_id"]

        # verified = 25 seats; the founder is seat 1. Fill to the cap directly.
        for i in range(24):
            db.add(models.User(
                name=f"Seat {i}", email=f"seat{i}@cap.example.com", role="employee",
                organization_id=org_id, is_active=True, is_password_set=True,
            ))
        db.commit()

        r = client.post("/employees/invite", headers=h,
                        json={"email": "one-too-many@cap.example.com"})
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "SEAT_OVER_ALLOCATION"
        assert detail["seat_limit"] == 25
        assert detail["seats_used"] == 25

        # Nothing was created for the refused invite.
        assert db.query(models.User).filter_by(
            email="one-too-many@cap.example.com").first() is None
