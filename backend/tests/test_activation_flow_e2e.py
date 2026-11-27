"""The whole activation flow, in one request sequence.

Account → Verify → Workspace → Ready, through the real HTTP layer, against the
real pipeline. The per-step suites already cover each screen's behaviour; what
this file exists for is the joins between them, which is where a four-step flow
actually breaks:

  * Step 1 has to leave behind exactly the state Step 2 expects to find.
  * Step 2 has to leave behind exactly the state Step 3 reads.
  * Nothing along the way may create a second organization, workspace,
    subscription or entitlement — including when steps are repeated, which is
    what a refresh or a double-click looks like from here.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database.models import User, Organization, OrgSettings
from app.auth.service import get_current_user
from app.billing import trial as billing_trial
from app.commercial import constants, identity
from app.commercial.models import (
    CommercialOrganization, CommercialWorkspace, CommercialSubscription,
    CommercialSignup, EntitlementsCache, EmailVerificationToken,
)
import app.commercial.routers.signup as signup_mod
import app.commercial.routers.verification as verification_mod
import app.commercial.routers.onboarding as onboarding_mod
import app.commercial.routers.activation as activation_mod


STRONG_PASSWORD = "Passw0rd!23"


@pytest.fixture(autouse=True)
def _no_abuse_controls(engine):
    """Neutralise the per-IP ceilings so the sequence is not throttled.

    Every request here shares one client IP, which is exactly what the signup
    and verify limits are keyed on. They are asserted deliberately in their own
    suites; here they would only make the flow test fail for the wrong reason.
    """
    from app.commercial.models import SignupRateBucket

    limiters = [signup_mod.limiter, verification_mod.limiter]
    previous = [lim.enabled for lim in limiters]
    for lim in limiters:
        lim.enabled = False

    s = sessionmaker(bind=engine)()
    try:
        s.query(SignupRateBucket).delete()
        s.commit()
    finally:
        s.close()

    yield

    for lim, was in zip(limiters, previous):
        lim.enabled = was


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def flow(Session, monkeypatch):
    """One app carrying every router the flow touches, and a mutable session.

    `session_user` is swapped once signup has produced a user, which is what
    makes the authenticated steps behave like a browser that just received a
    token from Step 1.
    """
    monkeypatch.setattr(signup_mod, "send_welcome_and_verification", lambda *a, **k: None)
    monkeypatch.setattr(verification_mod, "send_verification_resend", lambda *a, **k: True)

    state = {"user": None}

    app = FastAPI()
    app.state.limiter = signup_mod.limiter
    for mod in (signup_mod, verification_mod, onboarding_mod, activation_mod):
        app.include_router(mod.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _current_user():
        if state["user"] is None:
            from fastapi import HTTPException
            raise HTTPException(401, "Not authenticated")
        return state["user"]

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _current_user

    return {"client": TestClient(app, raise_server_exceptions=True), "state": state}


def _sign_in_as(flow, Session, email):
    s = Session()
    user = s.query(User).filter(User.email == email).one()
    s.refresh(user)
    s.expunge(user)
    s.close()
    flow["state"]["user"] = user
    return user


def _raw_code(Session, user_id):
    """The plaintext code is never stored, so mint a known one to verify with.

    This is the same helper path signup uses; issuing a fresh credential is
    what a resend does, so the flow stays realistic.
    """
    s = Session()
    try:
        _, code, _ = identity.issue_verification_credentials(
            s, user_id=user_id, organization_id=None,
        )
        s.commit()
        return code
    finally:
        s.close()


def _counts(Session, comm_org_id=None):
    s = Session()
    try:
        return {
            "orgs": s.query(Organization).count(),
            "comm_orgs": s.query(CommercialOrganization).count(),
            "workspaces": s.query(CommercialWorkspace).count(),
            "subscriptions": s.query(CommercialSubscription).count(),
            "entitlements": s.query(EntitlementsCache).count(),
            "users": s.query(User).count(),
        }
    finally:
        s.close()


SIGNUP_BODY = {
    "first_name": "Sadik", "last_name": "Rahman",
    "email": "flow-e2e@engines.example.com",
    "password": STRONG_PASSWORD,
    "country": "GB", "plan": "verified", "intent": "trial", "expected_users": 1,
    "terms_version": "v1", "privacy_version": "v1",
    "client_context": {"locale": "en-GB", "timezone": "Europe/London"},
}

ORG_BODY = {
    "organization_name": "Acme Corporation",
    "role": "IT / Security Manager",
    "worker_band": "11-50",
    "country": "US",
    "timezone": "America/New_York",
    "workspace_slug": "acme-workspace",
}


# ══════════════════════════════════════════════════════════════════════════
# The happy path, end to end
# ══════════════════════════════════════════════════════════════════════════
def test_account_to_ready(flow, Session):
    client = flow["client"]

    # ── Step 1 — Account ────────────────────────────────────────────────
    r1 = client.post("/api/v1/signup", json=SIGNUP_BODY)
    assert r1.status_code in (200, 201), r1.text
    signup = r1.json()
    assert signup["verification_required"] is True
    comm_org_id = signup["organization_id"]
    op_org_id = signup["operational_org_id"]

    # No company was asked for, so the server named the organization itself.
    s = Session()
    assert s.query(Organization).filter_by(id=op_org_id).one().name == "Sadik's workspace"
    s.close()

    user = _sign_in_as(flow, Session, SIGNUP_BODY["email"])

    # ── Step 2 — Verify ─────────────────────────────────────────────────
    status = client.get("/api/v1/email-verification/status").json()
    assert status["email"] == SIGNUP_BODY["email"]
    assert status["verified"] is False
    assert status["code_length"] == constants.VERIFICATION_CODE_LENGTH

    code = _raw_code(Session, user.id)
    rv = client.post("/api/v1/email-verification/verify-code", json={"code": code})
    assert rv.status_code == 200, rv.text
    assert rv.json()["verified"] is True

    # ── Step 2 — Organisation ───────────────────────────────────────────
    ro = client.post("/api/v1/onboarding/organization", json=ORG_BODY)
    assert ro.status_code == 200, ro.text
    # Terminal, not "teams": leaving the wizard open bounced the admin into
    # the old /setup screens on the next navigation.
    assert ro.json()["current_step"] == "complete"
    assert ro.json()["setup_required"] is False

    # ── Step 3 — Workspace ──────────────────────────────────────────────
    rs = client.get("/api/v1/activation/status/me")
    assert rs.status_code == 200, rs.text
    st = rs.json()
    assert st["organization_id"] == comm_org_id
    # Provisioning ran inline during signup, so it is already terminal here.
    assert st["status"] == "ready"
    assert all(m["status"] == "complete" for m in st["milestones"]), st["milestones"]
    assert st["support_reference"].startswith("ZT-")

    # ── Step 4 — Ready ──────────────────────────────────────────────────
    assert st["owner_first_name"] == "Sadik"
    assert st["organization_name"] == "Acme Corporation"
    assert st["workspace_slug"] == "acme-workspace"      # chosen on Step 2
    assert st["trial"]["length_days"] == billing_trial.TRIAL_LENGTH_DAYS == 14
    assert st["trial"]["plan_code"] == "verified"
    assert st["workspace_url"]


def test_the_flow_creates_exactly_one_of_everything(flow, Session):
    before = _counts(Session)
    client = flow["client"]

    r1 = client.post("/api/v1/signup", json={**SIGNUP_BODY, "email": "flow-once@engines.example.com"})
    assert r1.status_code in (200, 201), r1.text
    user = _sign_in_as(flow, Session, "flow-once@engines.example.com")
    client.post("/api/v1/email-verification/verify-code",
                json={"code": _raw_code(Session, user.id)})
    client.post("/api/v1/onboarding/organization", json=ORG_BODY)
    client.get("/api/v1/activation/status/me")

    after = _counts(Session)
    for key in ("orgs", "comm_orgs", "workspaces", "subscriptions", "users"):
        assert after[key] == before[key] + 1, f"{key}: {before[key]} -> {after[key]}"


def test_step_2_updates_the_organization_step_1_created(flow, Session):
    client = flow["client"]
    r1 = client.post("/api/v1/signup", json={**SIGNUP_BODY, "email": "flow-rename@engines.example.com"})
    op_org_id = r1.json()["operational_org_id"]
    comm_org_id = r1.json()["organization_id"]

    user = _sign_in_as(flow, Session, "flow-rename@engines.example.com")
    client.post("/api/v1/email-verification/verify-code",
                json={"code": _raw_code(Session, user.id)})
    client.post("/api/v1/onboarding/organization", json=ORG_BODY)

    s = Session()
    try:
        # Same rows, new values — not a second tenant beside the first.
        assert s.query(Organization).filter_by(id=op_org_id).one().name == "Acme Corporation"
        assert s.query(CommercialOrganization).filter_by(
            id=comm_org_id).one().name == "Acme Corporation"
        settings = s.query(OrgSettings).filter_by(organization_id=op_org_id).one()
        assert settings.slug == "acme-workspace"
        assert settings.default_timezone == "America/New_York"
        assert s.query(User).filter_by(id=user.id).one().designation == "IT / Security Manager"
        # RBAC untouched.
        assert s.query(User).filter_by(id=user.id).one().role == "admin"
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# Repetition — refresh, double-click, two tabs
# ══════════════════════════════════════════════════════════════════════════
def test_repeating_every_step_changes_nothing(flow, Session):
    client = flow["client"]
    r1 = client.post("/api/v1/signup", json={**SIGNUP_BODY, "email": "flow-repeat@engines.example.com"})
    assert r1.status_code in (200, 201), r1.text
    user = _sign_in_as(flow, Session, "flow-repeat@engines.example.com")

    code = _raw_code(Session, user.id)
    client.post("/api/v1/email-verification/verify-code", json={"code": code})
    client.post("/api/v1/onboarding/organization", json=ORG_BODY)

    mid = _counts(Session)

    # Everything again, as a refresh-and-resubmit would.
    assert client.post("/api/v1/email-verification/verify-code",
                       json={"code": code}).status_code == 200
    assert client.post("/api/v1/onboarding/organization", json=ORG_BODY).status_code == 200
    assert client.post("/api/v1/onboarding/organization", json=ORG_BODY).status_code == 200
    assert client.get("/api/v1/activation/status/me").status_code == 200
    assert client.post("/api/v1/activation/retry").status_code == 200

    assert _counts(Session) == mid


def test_a_repeated_signup_is_rejected_rather_than_duplicated(flow, Session):
    client = flow["client"]
    body = {**SIGNUP_BODY, "email": "flow-dup@engines.example.com"}
    assert client.post("/api/v1/signup", json=body).status_code in (200, 201)
    before = _counts(Session)

    again = client.post("/api/v1/signup", json=body)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == constants.ERR_EMAIL_ALREADY_EXISTS
    assert _counts(Session) == before


def test_an_idempotency_key_replays_rather_than_reprovisioning(flow, Session):
    client = flow["client"]
    body = {**SIGNUP_BODY, "email": "flow-idem-flow@engines.example.com"}
    headers = {"Idempotency-Key": "flow-e2e-key-1"}

    before = _counts(Session)
    first = client.post("/api/v1/signup", json=body, headers=headers)
    second = client.post("/api/v1/signup", json=body, headers=headers)

    assert first.status_code in (200, 201), first.text
    assert second.json() == first.json()
    after = _counts(Session)
    assert after["workspaces"] == before["workspaces"] + 1
    assert after["subscriptions"] == before["subscriptions"] + 1


# ══════════════════════════════════════════════════════════════════════════
# Ordering — a step cannot be skipped
# ══════════════════════════════════════════════════════════════════════════
def test_the_status_route_refuses_a_caller_with_no_session(flow):
    # Step 3 before Step 1 is not a state; it is an unauthenticated request.
    assert flow["client"].get("/api/v1/activation/status/me").status_code == 401


def test_verification_is_not_required_to_reach_ready_but_is_reported(flow, Session):
    """Provisioning does not wait on the inbox, and the read-model says so.

    The workspace is `ready` whether or not the email has been confirmed —
    that is the existing behaviour, and entitlements carry the
    `email_unverified` restriction instead. What must not happen is the Ready
    screen implying the address was confirmed when it was not.
    """
    client = flow["client"]
    client.post("/api/v1/signup", json={**SIGNUP_BODY, "email": "flow-unverified@engines.example.com"})
    _sign_in_as(flow, Session, "flow-unverified@engines.example.com")

    st = client.get("/api/v1/activation/status/me").json()
    assert st["status"] == "ready"
    assert st["email_verified"] is False
    account_row = next(m for m in st["milestones"] if m["key"] == "account_verified")
    assert account_row["status"] == "pending"


def test_no_checkout_session_is_created_anywhere_in_the_flow(flow, Session):
    """$0 due today, no card, no charge — all the way to Ready."""
    from app.commercial.models import CheckoutSession

    before = Session().query(CheckoutSession).count()
    client = flow["client"]
    client.post("/api/v1/signup", json={**SIGNUP_BODY, "email": "flow-nocharge@engines.example.com"})
    user = _sign_in_as(flow, Session, "flow-nocharge@engines.example.com")
    client.post("/api/v1/email-verification/verify-code",
                json={"code": _raw_code(Session, user.id)})
    client.post("/api/v1/onboarding/organization", json=ORG_BODY)
    client.get("/api/v1/activation/status/me")

    s = Session()
    try:
        assert s.query(CheckoutSession).count() == before
        sub = s.query(CommercialSubscription).join(
            CommercialOrganization,
            CommercialOrganization.id == CommercialSubscription.organization_id,
        ).filter(CommercialOrganization.name == "Acme Corporation").first()
        assert sub is not None and sub.state == "trial_active"
    finally:
        s.close()
