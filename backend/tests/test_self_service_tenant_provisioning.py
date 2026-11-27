"""Self-service signup must produce a real operational tenant.

The defect these tests exist to prevent: signup used to create a
``CommercialOrganization`` but no operational ``Organization``, leaving
``users.organization_id`` NULL. Because every org-scoped read is written as
``Model.organization_id == current_user.organization_id`` and SQLAlchemy
compiles a Python ``None`` right-hand side to ``IS NULL``, that filter
*succeeded* and returned every org-less row — so all self-serve tenants shared
one bucket and could read each other's data.

Everything here goes through the HTTP layer so the assertions cover the real
request path: validation, the provisioning transaction, and the response
contract together.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database.models import (
    User, Organization, OrgSettings, AppSettings, LeavePolicy, AuditLog,
)
from app.commercial.models import (
    CommercialOrganization, CommercialWorkspace, CommercialSubscription,
    CommercialSignup, OnboardingChecklist,
)
import app.commercial.routers.signup as signup_mod


STRONG_PASSWORD = "Passw0rd!23"


@pytest.fixture(autouse=True)
def _isolate_abuse_counters(engine):
    """Neutralise cross-test accumulation in the two abuse-control layers.

    Both layers are keyed on things every test in this module shares — one
    client IP and one email domain — so without this fixture the module
    throttles itself and the provisioning assertions fail for the wrong reason:

    * the per-IP slowapi ceiling (10/hour) starts returning 429, and
    * the DB-backed hourly velocity buckets in ``app.commercial.fraud`` push the
      fraud score to CHALLENGE, which now escalates to manual review instead of
      being silently allowed.

    ``fraud.evaluate`` itself is left real — only the accumulated counters are
    cleared — so the tests still exercise the true code path. The controls are
    asserted deliberately in TestRateLimiting and
    TestFraudChallengeIsNotSilentlyAllowed.
    """
    from sqlalchemy.orm import sessionmaker
    from app.commercial.models import SignupRateBucket

    limiter = signup_mod.limiter
    previous = limiter.enabled
    limiter.enabled = False

    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        s.query(SignupRateBucket).delete()
        s.commit()
    finally:
        s.close()

    yield

    limiter.enabled = previous


@pytest.fixture
def client(engine, monkeypatch):
    # No real outbound email.
    monkeypatch.setattr(signup_mod, "send_welcome_and_verification", lambda *a, **k: None)

    app = FastAPI()
    app.state.limiter = signup_mod.limiter
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


def _body(email, **overrides):
    body = {
        "first_name": "Ada", "last_name": "Lovelace", "email": email,
        "password": STRONG_PASSWORD, "company_name": "Analytical Engines Ltd",
        "country": "GB", "plan": "verified", "intent": "trial",
        "expected_users": 4,
        "terms_version": "v1", "privacy_version": "v1",
        "client_context": {"locale": "en-GB", "timezone": "Europe/London"},
    }
    body.update(overrides)
    return body


def _signup(client, email, **overrides):
    r = client.post("/api/v1/signup", json=_body(email, **overrides))
    return r


# ═════════════════════════════════════════════════════════════════════════════
# The operational tenant
# ═════════════════════════════════════════════════════════════════════════════

class TestTimezoneIsValidatedOnTheSelfServePath:
    """`client_context.timezone` is client-supplied, so it must be validated.

    The super-admin handler rejects a bad zone with a 400 before provisioning.
    Self-service has no such gate, so validation happens inside the shared
    provisioning service — which both paths go through.

    Writing an unloadable zone is worse than writing none: `app._tz._resolve`
    falls back silently at read time, so the tenant would run its attendance day,
    absence cutoffs and payroll periods on the fallback clock while OrgSettings
    claimed otherwise.
    """

    def test_a_valid_browser_timezone_is_persisted(self, client, db):
        r = _signup(client, "tz-good@engines.example.com",
                    client_context={"locale": "en-IN", "timezone": "Asia/Kolkata"})
        assert r.status_code in (200, 201), r.text
        oid = r.json()["operational_org_id"]
        row = db.query(OrgSettings).filter_by(organization_id=oid).one()
        assert row.default_timezone == "Asia/Kolkata"

    @pytest.mark.parametrize("bad_tz", [
        "California, Sacramento",   # a place, not a zone
        "IST",                      # an abbreviation
        "GMT+5.5",                  # an offset
        "Not/AZone",
        "../../etc/passwd",         # crafted payload
    ])
    def test_an_invalid_timezone_is_discarded_not_persisted(self, client, db, bad_tz):
        email = f"tz-bad-{abs(hash(bad_tz)) % 10000}@engines.example.com"
        r = _signup(client, email, client_context={"timezone": bad_tz})
        # Signup still succeeds — a browser quirk must not cost a real customer
        # their account.
        assert r.status_code in (200, 201), r.text
        oid = r.json()["operational_org_id"]
        row = db.query(OrgSettings).filter_by(organization_id=oid).one()
        assert row.default_timezone is None, (
            f"persisted an unloadable zone {bad_tz!r}; the tenant would silently "
            "run on the fallback clock"
        )

    def test_a_missing_timezone_leaves_null_for_the_admin_to_set(self, client, db):
        r = _signup(client, "tz-none@engines.example.com", client_context={"locale": "en-GB"})
        assert r.status_code in (200, 201), r.text
        oid = r.json()["operational_org_id"]
        row = db.query(OrgSettings).filter_by(organization_id=oid).one()
        assert row.default_timezone is None

    def test_the_shared_service_uses_the_canonical_validator(self):
        """One answer to "what counts as a timezone", per app/_tz.py's docstring."""
        import inspect
        import app.org.provisioning_service as ps
        assert "is_valid_timezone" in inspect.getsource(ps)


class TestOperationalTenantIsCreated:

    def test_user_gets_a_real_organization_id(self, client, db):
        r = _signup(client, "tenant1@engines.example.com")
        assert r.status_code in (200, 201), r.text
        payload = r.json()

        user = db.query(User).filter_by(id=payload["user_id"]).one()
        assert user.organization_id is not None, "signup left the admin org-less"
        assert isinstance(user.organization_id, int)
        assert payload["operational_org_id"] == user.organization_id

        org = db.query(Organization).filter_by(id=user.organization_id).one()
        assert org.name == "Analytical Engines Ltd"

    def test_admin_gets_the_existing_admin_role_not_a_new_one(self, client, db):
        r = _signup(client, "tenant-role@engines.example.com")
        user = db.query(User).filter_by(id=r.json()["user_id"]).one()
        # Deliberately the existing role — no "owner"/"organization_owner".
        assert user.role == "admin"

    def test_commercial_org_links_back_to_the_operational_org(self, client, db):
        r = _signup(client, "tenant2@engines.example.com")
        payload = r.json()

        comm = db.query(CommercialOrganization).filter_by(
            id=payload["organization_id"]
        ).one()
        # The bridge that was previously never written on this path.
        assert comm.operational_org_id == payload["operational_org_id"]
        assert comm.owner_user_id == payload["user_id"]
        assert comm.domain == "engines.example.com"
        assert comm.expected_users == 4

    def test_org_settings_carries_the_submitted_company_name_and_timezone(self, client, db):
        r = _signup(client, "tenant3@engines.example.com")
        oid = r.json()["operational_org_id"]

        row = db.query(OrgSettings).filter_by(organization_id=oid).one()
        # OrgSettings.company_name is what the product UI reads — the name used
        # to reach only the commercial organization.
        assert row.company_name == "Analytical Engines Ltd"
        assert row.default_timezone == "Europe/London"
        assert row.primary_country == "GB"
        assert row.default_currency == "GBP"

    def test_ownership_is_recorded_on_both_existing_owner_columns(self, client, db):
        r = _signup(client, "tenant4@engines.example.com")
        payload = r.json()
        oid = payload["operational_org_id"]

        settings = db.query(OrgSettings).filter_by(organization_id=oid).one()
        comm = db.query(CommercialOrganization).filter_by(
            id=payload["organization_id"]
        ).one()

        assert settings.owner_user_id == payload["user_id"]
        assert settings.owner_since is not None
        assert comm.owner_user_id == payload["user_id"]

    def test_app_settings_are_org_stamped_not_null_bucketed(self, client, db):
        r = _signup(client, "tenant5@engines.example.com")
        oid = r.json()["operational_org_id"]

        rows = db.query(AppSettings).filter_by(organization_id=oid).all()
        keys = {row.key for row in rows}
        assert f"shared_pool_enabled:{oid}" in keys
        assert f"leave_waiting_period_months:{oid}" in keys
        for row in rows:
            assert row.organization_id == oid

    def test_leave_policies_are_seeded(self, client, db):
        from app.leave.router import LEAVE_POLICIES

        r = _signup(client, "tenant6@engines.example.com")
        oid = r.json()["operational_org_id"]

        policies = db.query(LeavePolicy).filter_by(organization_id=oid).all()
        # Previously skipped entirely: the seeding step detected the NULL
        # organization and returned early.
        assert len(policies) == len(LEAVE_POLICIES)

    def test_commercial_workspace_subscription_and_checklist_exist(self, client, db):
        r = _signup(client, "tenant7@engines.example.com")
        payload = r.json()
        cid = payload["organization_id"]

        ws = db.query(CommercialWorkspace).filter_by(id=payload["workspace_id"]).one()
        assert ws.organization_id == cid
        assert ws.currency == "GBP"       # same helper as the operational side

        sub = db.query(CommercialSubscription).filter_by(organization_id=cid).one()
        assert sub.plan_code == "verified"
        assert sub.state == "trial_active"
        assert sub.trial_ends_at is not None

        assert db.query(OnboardingChecklist).filter_by(organization_id=cid).count() == 1

    def test_provisioning_is_audited(self, client, db):
        r = _signup(client, "tenant8@engines.example.com")
        oid = r.json()["operational_org_id"]

        row = db.query(AuditLog).filter_by(
            organization_id=oid, action="OrgProvisioned"
        ).one()
        assert row.payload["created_via"] == "self_service"
        assert row.payload["signup_id"] == r.json()["signup_id"]


# ═════════════════════════════════════════════════════════════════════════════
# Tenant isolation — the actual point of the fix
# ═════════════════════════════════════════════════════════════════════════════

class TestSelfServeTenantsAreIsolated:

    def test_two_self_serve_signups_get_distinct_tenants(self, client, db):
        r1 = _signup(client, "iso-a@alpha.example.com", company_name="Alpha Ltd")
        r2 = _signup(client, "iso-b@beta.example.com", company_name="Beta Ltd")

        oid_a = r1.json()["operational_org_id"]
        oid_b = r2.json()["operational_org_id"]
        assert oid_a is not None and oid_b is not None
        assert oid_a != oid_b

    def test_org_scoped_query_does_not_leak_between_them(self, client, db):
        """The regression itself: with NULL org ids this query returned both."""
        r1 = _signup(client, "leak-a@alpha2.example.com", company_name="Alpha Two")
        r2 = _signup(client, "leak-b@beta2.example.com", company_name="Beta Two")

        oid_a = r1.json()["operational_org_id"]
        oid_b = r2.json()["operational_org_id"]

        # This is the exact shape used across the org-scoped routers.
        users_a = db.query(User).filter(User.organization_id == oid_a).all()
        users_b = db.query(User).filter(User.organization_id == oid_b).all()

        assert [u.email for u in users_a] == ["leak-a@alpha2.example.com"]
        assert [u.email for u in users_b] == ["leak-b@beta2.example.com"]

        settings_a = db.query(OrgSettings).filter(
            OrgSettings.organization_id == oid_a
        ).all()
        assert len(settings_a) == 1
        assert settings_a[0].company_name == "Alpha Two"

    def test_no_signup_leaves_a_null_org_user_behind(self, client, db):
        _signup(client, "nonull-1@gamma.example.com")
        _signup(client, "nonull-2@gamma.example.com")

        # Scoped to the accounts this test created. The engine is session-scoped
        # and shared with modules that legitimately seed org-less users, so a
        # whole-table assertion would be testing their fixtures, not signup.
        # The unscoped invariant is enforced on a dedicated engine in
        # tests/test_null_org_isolation.py.
        created = (
            db.query(User)
              .filter(User.email.in_([
                  "nonull-1@gamma.example.com", "nonull-2@gamma.example.com",
              ]))
              .all()
        )
        assert len(created) == 2
        for u in created:
            assert u.organization_id is not None, f"{u.email} was left org-less"
            assert u.role == "admin"


# ═════════════════════════════════════════════════════════════════════════════
# Session on signup
# ═════════════════════════════════════════════════════════════════════════════

class TestSessionIsEstablished:

    def test_signup_returns_a_usable_access_token(self, client, db):
        r = _signup(client, "session1@engines.example.com")
        payload = r.json()

        assert payload.get("access_token"), "signup did not establish a session"
        assert payload["token_type"] == "bearer"
        assert payload["expires_in"] > 0
        assert payload["role"] == "admin"
        assert payload["setup_required"] is True

        from app.auth.service import decode_access_token
        claims = decode_access_token(payload["access_token"])
        assert claims is not None
        assert claims["user_id"] == payload["user_id"]
        assert claims["role"] == "admin"

    def test_web_client_gets_refresh_as_cookie_not_in_body(self, client, db):
        r = _signup(client, "session2@engines.example.com")
        # Dual delivery would trip the single-use rotation's reuse detection.
        assert r.json().get("refresh_token") is None
        assert any(c for c in r.cookies.keys())

    def test_desktop_client_gets_refresh_in_body(self, engine, monkeypatch, db):
        monkeypatch.setattr(signup_mod, "send_welcome_and_verification", lambda *a, **k: None)
        app = FastAPI()
        app.state.limiter = signup_mod.limiter
        app.include_router(signup_mod.router)
        TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        def _get_db():
            s = TestingSessionLocal()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = _get_db
        c = TestClient(app, raise_server_exceptions=True)

        r = c.post(
            "/api/v1/signup",
            json=_body("session3@engines.example.com"),
            headers={"X-Client-Type": "desktop"},
        )
        assert r.status_code in (200, 201), r.text
        assert r.json().get("refresh_token")


# ═════════════════════════════════════════════════════════════════════════════
# Server-side plan validation
# ═════════════════════════════════════════════════════════════════════════════

class TestPlanValidation:

    @pytest.mark.parametrize("plan", ["verified", "governed"])
    def test_accepts_self_serve_trial_plans(self, client, db, plan):
        """Sovereign is deliberately absent: it is a valid self-serve plan but
        not a self-serve *trial* plan (catalog `trial_enabled=False`). The two
        cases below cover it."""
        r = _signup(client, f"plan-{plan}@engines.example.com", plan=plan)
        assert r.status_code in (200, 201), r.text
        cid = r.json()["organization_id"]
        sub = db.query(CommercialSubscription).filter_by(organization_id=cid).one()
        assert sub.plan_code == plan

    def test_sovereign_trial_is_routed_to_sales_and_not_provisioned(self, client, db):
        """Sovereign is a Guided Assurance Pilot. It used to self-provision a
        free workspace with unlimited seats, SSO, SCIM and legal hold."""
        from app.commercial import constants
        before = db.query(CommercialSubscription).count()
        r = _signup(client, "plan-sovereign@engines.example.com", plan="sovereign")

        assert r.status_code == 200, r.text
        assert r.json()["route"] == constants.ROUTE_ASSISTED
        assert "organization_id" not in r.json()
        assert db.query(CommercialSubscription).count() == before

    def test_sovereign_remains_purchasable_self_serve(self, client, db):
        """Only the trial is withdrawn — the plan itself is still self-serve."""
        r = _signup(client, "plan-sovereign-paid@engines.example.com",
                    plan="sovereign", intent="paid")
        assert r.status_code in (200, 201), r.text
        cid = r.json()["organization_id"]
        sub = db.query(CommercialSubscription).filter_by(organization_id=cid).one()
        assert sub.plan_code == "sovereign"

    @pytest.mark.parametrize("plan", [
        "enterprise",       # sales-led — must never be self-provisionable
        "internal",
        "free",
        "ENTERPRISE",
        "sovereign; drop",
    ])
    def test_rejects_non_self_serve_plans(self, client, plan):
        r = client.post("/api/v1/signup", json=_body("plan-bad@engines.example.com", plan=plan))
        assert r.status_code == 422, r.text
        assert "plan" in r.text

    def test_missing_plan_falls_back_to_the_default(self, client, db):
        body = _body("plan-none@engines.example.com")
        body.pop("plan")
        r = client.post("/api/v1/signup", json=body)
        assert r.status_code in (200, 201), r.text
        cid = r.json()["organization_id"]
        sub = db.query(CommercialSubscription).filter_by(organization_id=cid).one()
        assert sub.plan_code == "verified"

    def test_rejects_unknown_intent(self, client):
        r = client.post("/api/v1/signup", json=_body("intent-bad@engines.example.com", intent="free_forever"))
        assert r.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# Password policy
# ═════════════════════════════════════════════════════════════════════════════

class TestPasswordPolicy:

    @pytest.mark.parametrize("password,missing", [
        ("password123", "uppercase"),
        ("PASSWORD123!", "lowercase"),
        ("Password!!!", "number"),
        ("Password123", "special"),
        ("Pw1!", "8 characters"),
    ])
    def test_rejects_weak_passwords(self, client, password, missing):
        r = client.post(
            "/api/v1/signup",
            json=_body("weak@engines.example.com", password=password),
        )
        assert r.status_code == 422, r.text
        assert "Password must contain" in r.text or "at least 8" in r.text

    def test_accepts_a_compliant_password(self, client):
        r = _signup(client, "strong@engines.example.com", password="Str0ng!Pass")
        assert r.status_code in (200, 201), r.text

    def test_policy_matches_the_password_reset_flow(self):
        """One platform, one password standard."""
        from app.auth.service import _validate_password_strength
        from app.commercial.schemas import SignupRequest
        from pydantic import ValidationError

        weak = "password123"
        assert _validate_password_strength(weak) is not None
        with pytest.raises(ValidationError):
            SignupRequest(
                first_name="A", last_name="B", email="x@y.example.com",
                password=weak, company_name="C",
                terms_version="v1", privacy_version="v1",
            )


# ═════════════════════════════════════════════════════════════════════════════
# No client-supplied authority
# ═════════════════════════════════════════════════════════════════════════════

class TestClientCannotSupplyAuthority:

    def test_role_from_client_is_ignored(self, client, db):
        r = client.post(
            "/api/v1/signup",
            json=_body("escalate@engines.example.com", role="super_admin"),
        )
        assert r.status_code in (200, 201), r.text
        user = db.query(User).filter_by(id=r.json()["user_id"]).one()
        assert user.role == "admin"

    def test_organization_id_from_client_is_ignored(self, client, db):
        """A caller must not be able to graft themselves onto another tenant."""
        victim = _signup(client, "victim@target.example.com", company_name="Target Ltd")
        victim_oid = victim.json()["operational_org_id"]

        r = client.post(
            "/api/v1/signup",
            json=_body(
                "attacker@evil.example.com",
                organization_id=victim_oid,
                operational_org_id=victim_oid,
            ),
        )
        assert r.status_code in (200, 201), r.text
        attacker = db.query(User).filter_by(id=r.json()["user_id"]).one()
        assert attacker.organization_id != victim_oid

    def test_signup_schema_has_no_authority_fields(self):
        from app.commercial.schemas import SignupRequest
        fields = set(SignupRequest.model_fields)
        for forbidden in ("role", "organization_id", "operational_org_id",
                          "owner_user_id", "commercial_state"):
            assert forbidden not in fields


# ═════════════════════════════════════════════════════════════════════════════
# Atomicity — no orphan account after a failure
# ═════════════════════════════════════════════════════════════════════════════

class TestSignupAtomicity:

    def test_provisioning_failure_leaves_no_orphan_and_email_stays_registerable(
        self, client, db, monkeypatch
    ):
        """The failure that used to permanently burn an email address.

        A mid-pipeline failure previously recorded telemetry by committing the
        CURRENT session, which also persisted the user created moments earlier.
        Because users.email is globally unique and the duplicate check runs
        first, that orphan blocked the address from ever registering again.
        """
        import app.commercial.provisioning as prov
        email = "atomic@engines.example.com"

        orgs_before = db.query(Organization).count()
        users_before = db.query(User).count()

        real_subscription_step = prov._step_create_subscription

        def _boom(db_, ctx):
            raise RuntimeError("injected failure after the tenant was flushed")

        monkeypatch.setattr(prov, "_step_create_subscription", _boom)
        r = client.post("/api/v1/signup", json=_body(email))
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "PROVISIONING_FAILED"
        assert r.json()["error"]["recoverable"] is True

        # Nothing survived.
        assert db.query(User).filter_by(email=email).first() is None
        assert db.query(Organization).count() == orgs_before
        assert db.query(User).count() == users_before

        # Failure telemetry was still recorded, through its own session.
        failed = db.query(CommercialSignup).filter_by(email=email).all()
        assert failed, "no failure telemetry recorded"
        assert failed[-1].state == "signup_failed"
        assert failed[-1].failure_code == "PROVISIONING_FAILED"

        # And the visitor can simply retry.
        monkeypatch.setattr(prov, "_step_create_subscription", real_subscription_step)
        retry = client.post("/api/v1/signup", json=_body(email))
        assert retry.status_code in (200, 201), retry.text
        assert retry.json()["operational_org_id"] is not None

    def test_duplicate_email_is_rejected_without_side_effects(self, client, db):
        email = "dup@engines.example.com"
        first = _signup(client, email)
        assert first.status_code in (200, 201)

        orgs_before = db.query(Organization).count()
        second = _signup(client, email)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"
        assert db.query(Organization).count() == orgs_before
        assert db.query(User).filter_by(email=email).count() == 1


# ═════════════════════════════════════════════════════════════════════════════
# Bot challenge
# ═════════════════════════════════════════════════════════════════════════════

class TestBotChallenge:

    def test_skipped_and_warned_when_not_configured(self, monkeypatch):
        from app.commercial import challenge
        monkeypatch.delenv("SIGNUP_CHALLENGE_SECRET", raising=False)
        monkeypatch.delenv("SIGNUP_CHALLENGE_REQUIRED", raising=False)
        assert challenge.is_configured() is False
        assert challenge.is_enforced() is False
        res = challenge.verify(None)
        assert res.outcome == challenge.OUTCOME_SKIPPED
        assert res.ok is True

    def test_required_without_secret_fails_closed(self, monkeypatch):
        """An explicit requirement must never be satisfied by a missing secret."""
        from app.commercial import challenge
        monkeypatch.delenv("SIGNUP_CHALLENGE_SECRET", raising=False)
        monkeypatch.setenv("SIGNUP_CHALLENGE_REQUIRED", "true")
        assert challenge.is_enforced() is True
        res = challenge.verify("anything")
        assert res.ok is False
        assert res.outcome == challenge.OUTCOME_UNAVAILABLE

    def test_missing_token_is_rejected_when_configured(self, monkeypatch):
        from app.commercial import challenge
        monkeypatch.setenv("SIGNUP_CHALLENGE_SECRET", "test-secret")
        assert challenge.is_enforced() is True
        res = challenge.verify(None)
        assert res.ok is False
        assert res.outcome == challenge.OUTCOME_MISSING

    def test_provider_rejection_is_rejected(self, monkeypatch):
        from app.commercial import challenge
        monkeypatch.setenv("SIGNUP_CHALLENGE_SECRET", "test-secret")

        class _Resp:
            content = b"{}"
            def json(self):
                return {"success": False, "error-codes": ["invalid-input-response"]}

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "Client", _Client)
        res = challenge.verify("bad-token")
        assert res.ok is False
        assert res.outcome == challenge.OUTCOME_FAILED

    def test_provider_acceptance_passes(self, monkeypatch):
        from app.commercial import challenge
        monkeypatch.setenv("SIGNUP_CHALLENGE_SECRET", "test-secret")

        class _Resp:
            content = b"{}"
            def json(self):
                return {"success": True}

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "Client", _Client)
        res = challenge.verify("good-token")
        assert res.ok is True
        assert res.outcome == challenge.OUTCOME_PASSED

    def test_unreachable_provider_fails_closed_when_enforced(self, monkeypatch):
        from app.commercial import challenge
        monkeypatch.setenv("SIGNUP_CHALLENGE_SECRET", "test-secret")

        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): raise OSError("network down")

        import httpx
        monkeypatch.setattr(httpx, "Client", _Client)
        res = challenge.verify("token")
        assert res.ok is False
        assert res.outcome == challenge.OUTCOME_UNAVAILABLE

    def test_signup_refuses_when_challenge_required_but_absent(self, client, monkeypatch):
        monkeypatch.setenv("SIGNUP_CHALLENGE_SECRET", "test-secret")
        r = client.post("/api/v1/signup", json=_body("challenged@engines.example.com"))
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "CHALLENGE_REQUIRED"


# ═════════════════════════════════════════════════════════════════════════════
# Fraud ACTION_CHALLENGE must not fall through
# ═════════════════════════════════════════════════════════════════════════════

class TestFraudChallengeIsNotSilentlyAllowed:

    def test_challenge_decision_escalates_to_review_without_a_passed_challenge(
        self, client, monkeypatch
    ):
        """ACTION_CHALLENGE used to be returned and then ignored entirely."""
        from app.commercial import fraud

        def _challenge_decision(db, **kwargs):
            return fraud.FraudDecision(
                action=fraud.ACTION_CHALLENGE, score=45.0, signals=[],
                reason="Velocity / pattern challenge",
            )

        monkeypatch.setattr(signup_mod.fraud, "evaluate", _challenge_decision)
        r = client.post("/api/v1/signup", json=_body("velocity@engines.example.com"))

        # Escalated to the manual-review queue, not provisioned.
        assert r.status_code == 202, r.text
        assert r.json()["manual_review_required"] is True

    def test_no_tenant_is_created_for_a_challenged_signup(self, client, db, monkeypatch):
        from app.commercial import fraud

        def _challenge_decision(db_, **kwargs):
            return fraud.FraudDecision(
                action=fraud.ACTION_CHALLENGE, score=45.0, signals=[], reason="velocity",
            )

        monkeypatch.setattr(signup_mod.fraud, "evaluate", _challenge_decision)
        email = "velocity2@engines.example.com"
        orgs_before = db.query(Organization).count()
        client.post("/api/v1/signup", json=_body(email))

        assert db.query(User).filter_by(email=email).first() is None
        assert db.query(Organization).count() == orgs_before

    def test_real_velocity_escalates_without_a_monkeypatch(self, client, db):
        """End-to-end proof using the real fraud engine, no stubbing.

        Repeated signups from one IP and one email domain accumulate velocity
        signals (ip >= 5/hr adds 40, domain >= 10/hr adds 25). That used to
        return ACTION_CHALLENGE, which no branch consumed — so the signup was
        provisioned anyway. It must now stop being provisioned.
        """
        outcomes = []
        for i in range(14):
            r = client.post(
                "/api/v1/signup",
                json=_body(f"vel{i}@velocity.example.com"),
            )
            outcomes.append(r.status_code)

        assert 202 in outcomes, (
            f"velocity never escalated to manual review across 14 signups: {outcomes}"
        )
        # Everything that escalated must have created no tenant.
        reviewed = (
            db.query(CommercialSignup)
              .filter(CommercialSignup.state == "manual_review_required")
              .all()
        )
        assert reviewed
        for row in reviewed:
            assert db.query(User).filter_by(email=row.email).first() is None


# ═════════════════════════════════════════════════════════════════════════════
# Rate limiting
# ═════════════════════════════════════════════════════════════════════════════

class TestRateLimiting:
    """Creating an organization is a rare action; the ceiling is deliberately
    far tighter than the login routes' 200/minute."""

    def test_signup_is_rate_limited_per_ip(self, client, db):
        from app.commercial import constants

        limiter = signup_mod.limiter
        limiter.enabled = True
        limiter.reset()
        try:
            ceiling = int(constants.SIGNUP_RATE_LIMIT.split("/")[0])
            statuses = []
            for i in range(ceiling + 2):
                r = client.post(
                    "/api/v1/signup",
                    json=_body(f"flood{i}@flood.example.com"),
                )
                statuses.append(r.status_code)
            assert 429 in statuses, (
                f"no request was rate limited across {ceiling + 2} attempts: {statuses}"
            )
            # The limit is a ceiling, not a blanket block — early attempts land.
            assert any(s in (200, 201) for s in statuses)
        finally:
            limiter.reset()
            limiter.enabled = False

    def test_rate_limit_is_configured_tighter_than_login(self):
        from app.commercial import constants
        assert constants.SIGNUP_RATE_LIMIT == "10/hour"
        assert constants.VERIFICATION_RESEND_RATE_LIMIT == "5/hour"


# ═════════════════════════════════════════════════════════════════════════════
# The activation flow's Account step (Step 1)
# ═════════════════════════════════════════════════════════════════════════════

class TestAccountStepOmitsCompanyName:
    """Step 1 collects email, name and password — the organization is named on
    the Workspace step, so the request carries no ``company_name``.

    The field used to be required, so the Account screen could only have been
    built by inventing an organization name on the client. It is now optional
    and the server derives a provisional one. What these tests guard is that
    omitting it still provisions a complete, isolated tenant — the same
    invariant the rest of this module exists for — and that clients which do
    send a name are entirely unaffected.
    """

    @staticmethod
    def _account_step_body(email, **overrides):
        body = _body(email, **overrides)
        body.pop("company_name", None)      # the key is absent, not empty
        return body

    def test_signup_succeeds_without_a_company_name(self, client, db):
        r = client.post("/api/v1/signup",
                        json=self._account_step_body("step1-a@engines.example.com"))
        assert r.status_code in (200, 201), r.text
        body = r.json()

        # Still a complete tenant: the org-less-admin defect this module was
        # written for must not reappear through the shorter payload.
        assert body["operational_org_id"] is not None
        user = db.query(User).filter_by(email="step1-a@engines.example.com").one()
        assert user.organization_id == body["operational_org_id"]

    def test_the_server_derives_the_provisional_name(self, client, db):
        r = client.post("/api/v1/signup",
                        json=self._account_step_body("step1-b@engines.example.com"))
        oid = r.json()["operational_org_id"]

        # Derived from the first name the Account step does collect. The admin
        # replaces it on the Workspace step (the setup wizard's "Organization
        # name" field), so this only ever shows before that.
        assert db.query(Organization).filter_by(id=oid).one().name == "Ada's workspace"
        assert db.query(OrgSettings).filter_by(organization_id=oid).one().company_name \
            == "Ada's workspace"

    def test_the_workspace_slug_survives_the_apostrophe(self, client, db):
        # The derived name contains a character the slugifier has to strip; a
        # broken slug would fail the workspace insert and roll the tenant back.
        r = client.post("/api/v1/signup",
                        json=self._account_step_body("step1-c@engines.example.com"))
        ws = db.query(CommercialWorkspace).filter_by(id=r.json()["workspace_id"]).one()
        assert ws.slug.startswith("ada-s-workspace-")

    def test_an_explicitly_sent_company_name_is_still_honoured(self, client, db):
        # Backward compatibility: the older single-page signup and the Postman
        # collection both still send this field.
        r = client.post("/api/v1/signup",
                        json=_body("step1-d@engines.example.com",
                                   company_name="Analytical Engines Ltd"))
        oid = r.json()["operational_org_id"]
        assert db.query(Organization).filter_by(id=oid).one().name == "Analytical Engines Ltd"

    def test_a_blank_company_name_is_treated_as_absent(self, client, db):
        # Whitespace would otherwise become the organization's name and the
        # workspace slug, both of which have to be non-empty.
        r = client.post("/api/v1/signup",
                        json=_body("step1-e@engines.example.com", company_name="   "))
        assert r.status_code in (200, 201), r.text
        oid = r.json()["operational_org_id"]
        assert db.query(Organization).filter_by(id=oid).one().name == "Ada's workspace"

    def test_the_trial_is_not_a_paid_subscription(self, client, db):
        # Submitting Step 1 must not activate billing. No card was collected and
        # none is charged; conversion happens later, through checkout.
        r = client.post("/api/v1/signup",
                        json=self._account_step_body("step1-f@engines.example.com"))
        sub = (db.query(CommercialSubscription)
                 .filter_by(organization_id=r.json()["organization_id"]).one())
        assert sub.state == "trial_active"

    def test_verification_is_still_required(self, client):
        # Step 1 hands off to Step 2 — Verify. A signup that reported itself as
        # already verified would let the flow skip that step entirely.
        r = client.post("/api/v1/signup",
                        json=self._account_step_body("step1-g@engines.example.com"))
        assert r.json()["verification_required"] is True
