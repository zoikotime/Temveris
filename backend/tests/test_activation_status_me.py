"""The session-scoped activation status and self-service retry (Steps 3 and 4).

Two things are being guarded here.

The first is tenant isolation. The unauthenticated ``/status`` endpoint is keyed
by an unguessable id and carries no PII; ``/status/me`` is keyed by the session
and therefore *may* carry the owner's name. That difference is the whole reason
the second endpoint exists, so the tests pin both halves of it: the identifying
fields appear on the session route and nowhere else, and no id in a request can
make either route answer for someone else's tenant.

The second is that Step 3 and Step 4 render facts rather than guesses — the
milestones, the reference id and the trial window all come from persisted state,
and the retry is the existing idempotent operation rather than a new one.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database.models import User, Organization, OrgSettings
from app.auth.service import get_current_user, hash_password
from app.billing import trial as billing_trial
from app.commercial import activation_status, constants, ids
from app.commercial.models import (
    CommercialOrganization, CommercialWorkspace, CommercialSubscription,
    CommercialSignup, EntitlementsCache,
)
import app.commercial.routers.activation as activation_mod


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def tenant(Session):
    """A provisioned tenant in the shape signup actually leaves behind."""
    suffix = int(time.time() * 1_000_000) % 10_000_000
    s = Session()

    org = Organization(name="Acme Corporation")
    s.add(org)
    s.flush()

    admin = User(
        name="Sadik Rahman", email=f"ready-{suffix}@engines.example.com",
        password=hash_password("Passw0rd!23"), role="admin",
        organization_id=org.id, is_active=True,
    )
    s.add(admin)
    # The workspace name the admin chose on Step 2.
    s.add(OrgSettings(organization_id=org.id, company_name="Acme Corporation", slug="asd"))

    comm_id = ids.org_id()
    s.add(CommercialOrganization(
        id=comm_id, name="Acme Corporation", commercial_state="trial_active",
        operational_org_id=org.id, owner_user_id=None, expected_users=50,
    ))
    ws_id = ids.workspace_id()
    s.add(CommercialWorkspace(
        id=ws_id, organization_id=comm_id, slug=f"acme-{suffix}",
        name="Acme Corporation", provisioning_state="ready",
    ))
    started = datetime.now(timezone.utc)
    s.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=comm_id, plan_code="verified",
        state="trial_active", seat_count=1, trial_started_at=started,
        # Stored as start + 30 by provisioning — the known discrepancy. The DTO
        # must NOT echo this value back as the customer-visible window.
        trial_ends_at=started + timedelta(days=constants.TRIAL_LENGTH_DAYS),
    ))
    s.add(EntitlementsCache(
        organization_id=comm_id, plan_code="verified",
        commercial_state="trial_active", profile_json={},
        expires_at=started + timedelta(hours=1),
    ))
    s.commit()
    s.refresh(admin)
    s.expunge(admin)
    out = {"admin": admin, "admin_id": admin.id, "op_org_id": org.id,
           "comm_org_id": comm_id, "workspace_id": ws_id, "trial_started": started}
    s.close()
    return out


def _client(Session, user):
    app = FastAPI()
    app.include_router(activation_mod.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def client(Session, tenant):
    return _client(Session, tenant["admin"])


def _set_state(Session, workspace_id, state):
    s = Session()
    ws = s.query(CommercialWorkspace).filter_by(id=workspace_id).one()
    ws.provisioning_state = state
    s.commit()
    s.close()


# ══════════════════════════════════════════════════════════════════════════
# Session-scoped status
# ══════════════════════════════════════════════════════════════════════════
def test_status_me_resolves_the_tenant_from_the_session(client, tenant):
    r = client.get("/api/v1/activation/status/me")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["organization_id"] == tenant["comm_org_id"]
    assert body["workspace_id"] == tenant["workspace_id"]
    assert body["status"] == "ready"


def test_status_me_carries_the_identifying_fields_step_4_needs(client):
    body = client.get("/api/v1/activation/status/me").json()
    assert body["owner_first_name"] == "Sadik"
    assert body["organization_name"] == "Acme Corporation"
    # The name chosen on Step 2 (OrgSettings.slug), not the machine-generated
    # CommercialWorkspace.slug that provisioning produced.
    assert body["workspace_slug"] == "asd"


def test_the_public_status_endpoint_still_leaks_no_pii(client, tenant):
    # It is keyed by an unguessable id rather than by a session, so the DTO must
    # stay anonymous. Adding the greeting to the shared read-model would have
    # quietly published names to anyone holding an organization id.
    body = client.get(
        f"/api/v1/activation/status?organization_id={tenant['comm_org_id']}"
    ).json()
    assert "owner_first_name" not in body
    assert "workspace_slug" not in body
    blob = str(body).lower()
    assert "sadik" not in blob
    assert "@engines.example.com" not in blob


def test_status_me_requires_a_session(Session, tenant):
    anon = _client(Session, None)
    assert anon.get("/api/v1/activation/status/me").status_code in (401, 403)


def test_a_user_cannot_read_another_tenants_status(Session, tenant):
    """No id is accepted, so there is nothing to substitute."""
    s = Session()
    other_org = Organization(name="Victim Ltd")
    s.add(other_org)
    s.flush()
    other_comm = ids.org_id()
    s.add(CommercialOrganization(id=other_comm, name="Victim Ltd",
                                 commercial_state="trial_active",
                                 operational_org_id=other_org.id))
    outsider = User(name="Mallory", email=f"mallory-{other_org.id}@x.example.com",
                    password=hash_password("Passw0rd!23"), role="admin",
                    organization_id=other_org.id, is_active=True)
    s.add(outsider)
    s.commit()
    s.refresh(outsider)
    s.expunge(outsider)
    s.close()

    c = _client(Session, outsider)
    body = c.get("/api/v1/activation/status/me").json()
    assert body["organization_id"] == other_comm
    assert body["organization_id"] != tenant["comm_org_id"]
    assert body.get("organization_name") == "Victim Ltd"


def test_an_org_less_caller_gets_404_not_someone_elses_workspace(Session):
    s = Session()
    stray = User(name="Nobody", email=f"stray-{int(time.time()*1e6) % 10**7}@x.example.com",
                 password=hash_password("Passw0rd!23"), role="admin",
                 organization_id=None, is_active=True)
    s.add(stray)
    s.commit()
    s.refresh(stray)
    s.expunge(stray)
    s.close()

    assert _client(Session, stray).get("/api/v1/activation/status/me").status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Milestones — mapped from real provisioning state
# ══════════════════════════════════════════════════════════════════════════
def test_ready_reports_every_milestone_complete(client):
    body = client.get("/api/v1/activation/status/me").json()
    ms = body["milestones"]
    assert [m["key"] for m in ms] == [
        "account_verified", "organisation_created", "workspace_provisioning",
        "trial_entitlements", "security_audit", "ready",
    ]
    # account_verified tracks the email fact, which this fixture has not set.
    # It is `pending`, never `active`: a spinner under five completed rows
    # would imply work nothing is actually doing.
    assert ms[0]["status"] == "pending"
    assert not any(m["status"] == "active" for m in ms)
    assert all(m["status"] == "complete" for m in ms[1:])


@pytest.mark.parametrize("state,expected_complete", [
    ("queued", []),
    ("organization_created", ["organisation_created"]),
    ("owner_assigned", ["organisation_created", "workspace_provisioning"]),
    ("entitlements_applied", ["organisation_created", "workspace_provisioning",
                              "trial_entitlements"]),
    ("ready", ["organisation_created", "workspace_provisioning",
               "trial_entitlements", "security_audit", "ready"]),
])
def test_milestones_follow_the_real_provisioning_state(client, Session, tenant,
                                                       state, expected_complete):
    _set_state(Session, tenant["workspace_id"], state)
    ms = client.get("/api/v1/activation/status/me").json()["milestones"]
    done = [m["key"] for m in ms if m["status"] == "complete"]
    assert done == expected_complete


def test_milestone_order_never_completes_a_later_step_first(client, Session, tenant):
    """The display order must be monotonic.

    The pipeline really finishes audit setup before entitlements, so a naive
    mapping would light row 5 while row 4 was still spinning. The audit row is
    held until `ready` instead — later than the truth, never earlier.
    """
    for state in ["queued", "identity_bound", "organization_created", "workspace_created",
                  "owner_assigned", "defaults_applied", "audit_initialized",
                  "entitlements_applied", "ready"]:
        _set_state(Session, tenant["workspace_id"], state)
        ms = client.get("/api/v1/activation/status/me").json()["milestones"]
        statuses = [m["status"] == "complete" for m in ms[1:]]   # skip the email fact
        # Once a False appears, everything after it must also be False.
        first_false = statuses.index(False) if False in statuses else len(statuses)
        assert not any(statuses[first_false:]), f"non-monotonic at {state}: {statuses}"


def test_exactly_one_milestone_is_active_while_in_progress(client, Session, tenant):
    _set_state(Session, tenant["workspace_id"], "workspace_created")
    ms = client.get("/api/v1/activation/status/me").json()["milestones"]
    assert sum(1 for m in ms if m["status"] == "active") == 1


def test_a_failed_workspace_marks_the_stalled_milestone_not_a_complete_one(client, Session, tenant):
    _set_state(Session, tenant["workspace_id"], "failed_retryable")
    body = client.get("/api/v1/activation/status/me").json()
    assert body["status"] == "failed"
    ms = body["milestones"]
    assert any(m["status"] == "failed" for m in ms)
    assert not any(m["status"] == "active" for m in ms)
    assert ms[-1]["status"] != "complete"


# ══════════════════════════════════════════════════════════════════════════
# Reference id
# ══════════════════════════════════════════════════════════════════════════
def test_reference_id_is_grouped_and_derived_from_the_correlation_id(Session, tenant):
    s = Session()
    s.add(CommercialSignup(
        id=ids.signup_id(), email="ref@engines.example.com", state="signup_completed",
        intent="trial", correlation_id="cor_7f9a3b2c11223344",
        organization_id=tenant["comm_org_id"], workspace_id=tenant["workspace_id"],
    ))
    s.commit()
    body = activation_status.compute(s, organization_id=tenant["comm_org_id"])
    s.close()
    # The signup lookup is by id; computing by organization alone has no signup,
    # so assert the helper directly for the format.
    assert activation_status._support_reference("cor_7f9a3b2c11223344") == "ZT-7F9A-3B2C"
    assert activation_status._support_reference(None) is None
    assert body["support_reference"] is None or body["support_reference"].startswith("ZT-")


def test_reference_id_is_stable_across_calls(Session, tenant):
    s = Session()
    sig_id = ids.signup_id()
    s.add(CommercialSignup(
        id=sig_id, email="stable@engines.example.com", state="signup_completed",
        intent="trial", correlation_id="cor_abcdef0112345678",
        organization_id=tenant["comm_org_id"], workspace_id=tenant["workspace_id"],
    ))
    s.commit()
    first = activation_status.compute(s, signup_id=sig_id)["support_reference"]
    second = activation_status.compute(s, signup_id=sig_id)["support_reference"]
    s.close()
    assert first == second == "ZT-ABCD-EF01"


# ══════════════════════════════════════════════════════════════════════════
# Trial facts — Step 4
# ══════════════════════════════════════════════════════════════════════════
def test_trial_length_is_the_customer_visible_clock(client):
    body = client.get("/api/v1/activation/status/me").json()
    # app/billing owns the countdown the customer sees and the ladder that
    # sends the expiry mail. app/commercial's 30 is NOT what is reported.
    assert body["trial"]["length_days"] == billing_trial.TRIAL_LENGTH_DAYS == 14
    assert body["trial"]["length_days"] != constants.TRIAL_LENGTH_DAYS


def test_trial_end_is_computed_from_the_recorded_start(client, tenant):
    body = client.get("/api/v1/activation/status/me").json()
    ends = datetime.fromisoformat(body["trial"]["ends_at"])
    delta = (ends - tenant["trial_started"]).days
    assert delta == billing_trial.TRIAL_LENGTH_DAYS


def test_the_stored_thirty_day_value_is_not_echoed_back(Session, tenant):
    """Pinning the known discrepancy without repairing it.

    `commercial_subscriptions.trial_ends_at` is still start + 30. This read does
    not change that column and does not report it; it reports the window the
    customer actually experiences. Fixing the column is a separate, controlled
    change.
    """
    s = Session()
    sub = s.query(CommercialSubscription).filter_by(
        organization_id=tenant["comm_org_id"]).one()
    stored_days = (sub.trial_ends_at - sub.trial_started_at).days
    s.close()
    assert stored_days == constants.TRIAL_LENGTH_DAYS == 30

    body = _client(Session, tenant["admin"]).get("/api/v1/activation/status/me").json()
    assert body["trial"]["length_days"] == 14


def test_trial_plan_is_reported(client):
    assert client.get("/api/v1/activation/status/me").json()["trial"]["plan_code"] == "verified"


# ══════════════════════════════════════════════════════════════════════════
# Self-service retry
# ══════════════════════════════════════════════════════════════════════════
def test_retry_is_a_no_op_on_a_ready_workspace(client, Session, tenant):
    before = Session().query(CommercialSubscription).filter_by(
        organization_id=tenant["comm_org_id"]).count()

    r = client.post("/api/v1/activation/retry")
    assert r.status_code == 200
    assert r.json() == {"retried": False, "reason": "already_ready",
                        "provisioning_state": "ready"}

    s = Session()
    try:
        assert s.query(CommercialSubscription).filter_by(
            organization_id=tenant["comm_org_id"]).count() == before
    finally:
        s.close()


def test_retry_recovers_a_retryable_failure(client, Session, tenant):
    _set_state(Session, tenant["workspace_id"], "failed_retryable")

    r = client.post("/api/v1/activation/retry")
    assert r.status_code == 200, r.text
    assert r.json()["retried"] is True

    s = Session()
    try:
        assert s.query(CommercialWorkspace).filter_by(
            id=tenant["workspace_id"]).one().provisioning_state == "ready"
    finally:
        s.close()


def test_retry_is_idempotent(client, Session, tenant):
    """Two tabs, a double-click, a network retry — one outcome."""
    _set_state(Session, tenant["workspace_id"], "failed_retryable")

    before = {
        "org": Session().query(CommercialOrganization).count(),
        "ws": Session().query(CommercialWorkspace).count(),
        "sub": Session().query(CommercialSubscription).count(),
        "ent": Session().query(EntitlementsCache).count(),
    }

    for _ in range(3):
        assert client.post("/api/v1/activation/retry").status_code == 200

    s = Session()
    try:
        assert s.query(CommercialOrganization).count() == before["org"]
        assert s.query(CommercialWorkspace).count() == before["ws"]
        assert s.query(CommercialSubscription).count() == before["sub"]
        assert s.query(EntitlementsCache).count() == before["ent"]
    finally:
        s.close()


def test_retry_recreates_a_missing_subscription_exactly_once(client, Session, tenant):
    s = Session()
    s.query(CommercialSubscription).filter_by(
        organization_id=tenant["comm_org_id"]).delete()
    s.commit()
    s.close()
    _set_state(Session, tenant["workspace_id"], "failed_retryable")

    for _ in range(2):
        assert client.post("/api/v1/activation/retry").status_code == 200

    s = Session()
    try:
        assert s.query(CommercialSubscription).filter_by(
            organization_id=tenant["comm_org_id"]).count() == 1
    finally:
        s.close()


def test_a_terminal_failure_is_not_auto_retried(client, Session, tenant):
    # That state exists precisely to stop automatic recovery attempts.
    _set_state(Session, tenant["workspace_id"], "failed_terminal")
    r = client.post("/api/v1/activation/retry")
    assert r.status_code == 409
    assert "manual recovery" in r.json()["detail"].lower()

    s = Session()
    try:
        assert s.query(CommercialWorkspace).filter_by(
            id=tenant["workspace_id"]).one().provisioning_state == "failed_terminal"
    finally:
        s.close()


def test_retry_requires_a_session(Session, tenant):
    anon = _client(Session, None)
    assert anon.post("/api/v1/activation/retry").status_code in (401, 403)


def test_retry_only_ever_touches_the_callers_own_workspace(Session, tenant):
    s = Session()
    other_org = Organization(name="Bystander Ltd")
    s.add(other_org)
    s.flush()
    other_comm = ids.org_id()
    s.add(CommercialOrganization(id=other_comm, name="Bystander Ltd",
                                 commercial_state="trial_active",
                                 operational_org_id=other_org.id))
    other_ws = ids.workspace_id()
    s.add(CommercialWorkspace(id=other_ws, organization_id=other_comm,
                              slug=f"bystander-{other_org.id}", name="Bystander Ltd",
                              provisioning_state="failed_retryable"))
    outsider = User(name="Mallory", email=f"m2-{other_org.id}@x.example.com",
                    password=hash_password("Passw0rd!23"), role="admin",
                    organization_id=other_org.id, is_active=True)
    s.add(outsider)
    s.commit()
    s.refresh(outsider)
    s.expunge(outsider)
    s.close()

    # The outsider retries; only their own workspace moves.
    assert _client(Session, outsider).post("/api/v1/activation/retry").status_code == 200

    s = Session()
    try:
        assert s.query(CommercialWorkspace).filter_by(id=other_ws).one().provisioning_state == "ready"
        assert s.query(CommercialWorkspace).filter_by(
            id=tenant["workspace_id"]).one().provisioning_state == "ready"   # untouched
    finally:
        s.close()
