"""POST /api/v1/onboarding/organization — the Step 2 organisation form.

The defect this endpoint exists to avoid is a second organisation. Step 1 has
already provisioned one, named provisionally after the admin's first name
because the Account step collects no company details. Step 2 must *rename* that
record, and every test here that counts rows is guarding exactly that.

The other thing worth pinning is where each field lands. Four tables are
involved and two of them have a field that looks like the one being written but
must not be: ``User.role`` is RBAC and is not the job title, and
``CommercialSubscription.seat_count`` is the billed quantity and is not the
self-declared headcount band.
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database.database import get_db
from database import models
from database.models import User, Organization, OrgSettings
from app.auth.service import get_current_user, hash_password
from app.commercial import constants, ids
from app.commercial.models import (
    CommercialOrganization, CommercialWorkspace, CommercialSubscription,
)
import app.commercial.routers.onboarding as onboarding_mod


VALID = {
    "organization_name": "Analytical Engines Ltd",
    "role": "IT / Security Manager",
    "worker_band": "11-50",
    "country": "GB",
    "timezone": "Europe/London",
    "workspace_slug": "analytical-engines",
}


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def tenant(Session):
    """A tenant in exactly the shape Step 1 leaves behind."""
    suffix = int(time.time() * 1_000_000) % 10_000_000
    s = Session()

    org = Organization(name="Ada's workspace")
    s.add(org)
    s.flush()

    admin = User(
        name="Ada Lovelace",
        email=f"org-{suffix}@engines.example.com",
        password=hash_password("Passw0rd!23"),
        role="admin",
        organization_id=org.id,
        is_active=True,
    )
    s.add(admin)
    s.add(OrgSettings(organization_id=org.id, company_name="Ada's workspace"))

    comm_id = ids.org_id()
    s.add(CommercialOrganization(
        id=comm_id, name="Ada's workspace", commercial_state="trial_active",
        operational_org_id=org.id, expected_users=1,
    ))
    s.add(CommercialWorkspace(
        id=ids.workspace_id(), organization_id=comm_id,
        slug=f"ada-s-workspace-{suffix}", name="Ada's workspace",
        provisioning_state="workspace_created",
    ))
    s.add(CommercialSubscription(
        id=ids.subscription_id(), organization_id=comm_id,
        plan_code="verified", state="trial_active", seat_count=1,
    ))
    # Signup provisioning opens the wizard (provisioning.py calls
    # setup_state.begin), so a tenant arriving at Step 2 is already sitting at
    # the "organization" step. Without this the fixture would be a tenant that
    # never entered setup, which setup_state deliberately treats as complete.
    from app.org import setup_state
    setup_state.begin(s, org.id)

    s.commit()
    s.refresh(admin)
    s.expunge(admin)
    # The id is captured as a plain int: `admin` is detached once this session
    # closes, so reading attributes off it in an assertion is unreliable.
    out = {"admin": admin, "admin_id": admin.id,
           "op_org_id": org.id, "comm_org_id": comm_id}
    s.close()
    return out


@pytest.fixture
def client(Session, tenant):
    app = FastAPI()
    app.include_router(onboarding_mod.router)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: tenant["admin"]
    return TestClient(app, raise_server_exceptions=True)


def _post(client, **overrides):
    body = {**VALID, **overrides}
    return client.post("/api/v1/onboarding/organization", json=body)


# ══════════════════════════════════════════════════════════════════════════
# It updates; it never creates
# ══════════════════════════════════════════════════════════════════════════
def test_the_existing_organization_is_renamed_not_duplicated(client, Session, tenant):
    before_op = Session().query(Organization).count()
    before_comm = Session().query(CommercialOrganization).count()

    r = _post(client)
    assert r.status_code == 200, r.text

    s = Session()
    try:
        assert s.query(Organization).count() == before_op
        assert s.query(CommercialOrganization).count() == before_comm
        assert s.query(Organization).filter_by(id=tenant["op_org_id"]).one().name \
            == "Analytical Engines Ltd"
        assert s.query(CommercialOrganization).filter_by(id=tenant["comm_org_id"]).one().name \
            == "Analytical Engines Ltd"
    finally:
        s.close()


def test_no_workspace_subscription_or_user_is_created(client, Session):
    before = {
        "ws": Session().query(CommercialWorkspace).count(),
        "sub": Session().query(CommercialSubscription).count(),
        "user": Session().query(User).count(),
    }
    assert _post(client).status_code == 200

    s = Session()
    try:
        assert s.query(CommercialWorkspace).count() == before["ws"]
        assert s.query(CommercialSubscription).count() == before["sub"]
        assert s.query(User).count() == before["user"]
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# Where each field lands
# ══════════════════════════════════════════════════════════════════════════
def test_org_settings_carries_name_country_timezone_and_slug(client, Session, tenant):
    assert _post(client).status_code == 200
    s = Session()
    try:
        row = s.query(OrgSettings).filter_by(organization_id=tenant["op_org_id"]).one()
        assert row.company_name == "Analytical Engines Ltd"
        assert row.primary_country == "GB"
        assert row.default_timezone == "Europe/London"
        assert row.slug == "analytical-engines"
    finally:
        s.close()


def test_role_is_stored_as_designation_and_rbac_is_untouched(client, Session, tenant):
    """The single most dangerous confusion in this form.

    `User.role` is what `_admin_user`, `AdminRoute` and every authorization gate
    read. Writing "IT / Security Manager" into it would lock the signup admin
    out of their own workspace on the very next request.
    """
    assert _post(client).status_code == 200
    s = Session()
    try:
        u = s.query(User).filter_by(id=tenant["admin_id"]).one()
        assert u.designation == "IT / Security Manager"
        assert u.role == "admin"
    finally:
        s.close()


def test_worker_band_lands_on_expected_users(client, Session, tenant):
    assert _post(client, worker_band="51-200").status_code == 200
    s = Session()
    try:
        comm = s.query(CommercialOrganization).filter_by(id=tenant["comm_org_id"]).one()
        assert comm.expected_users == constants.WORKER_COUNT_BAND_VALUES["51-200"]
    finally:
        s.close()


def test_worker_band_never_reaches_billed_seats(client, Session, tenant):
    """Segmentation, not billing.

    Billable seats are counted from real users by app.billing.seats. If a
    self-declared band set `seat_count`, a customer choosing "201-500" on a
    signup form would be billed for 500 people who do not exist.
    """
    before = Session().query(CommercialSubscription).filter_by(
        organization_id=tenant["comm_org_id"]).one().seat_count

    assert _post(client, worker_band="201-500").status_code == 200

    s = Session()
    try:
        sub = s.query(CommercialSubscription).filter_by(
            organization_id=tenant["comm_org_id"]).one()
        assert sub.seat_count == before == 1
    finally:
        s.close()


def test_commercial_country_is_mirrored(client, Session, tenant):
    assert _post(client, country="us").status_code == 200
    s = Session()
    try:
        comm = s.query(CommercialOrganization).filter_by(id=tenant["comm_org_id"]).one()
        assert comm.country == "US"          # normalised to upper case
    finally:
        s.close()


def test_country_change_invalidates_the_entitlement_cache(client, Session, tenant):
    """Country feeds the jurisdiction layer, so a change must not leave a stale
    entitlement profile behind. Uses the one existing invalidator.
    """
    from app.commercial import entitlements
    from app.commercial.models import EntitlementsCache

    s = Session()
    try:
        entitlements.compute(s, tenant["comm_org_id"])
        s.commit()
        assert s.query(EntitlementsCache).filter_by(
            organization_id=tenant["comm_org_id"]).one_or_none() is not None
    finally:
        s.close()

    assert _post(client, country="de").status_code == 200

    s = Session()
    try:
        assert s.query(EntitlementsCache).filter_by(
            organization_id=tenant["comm_org_id"]).one_or_none() is None,             "the cached profile should have been dropped when country changed"
        # And the next read reflects the new country.
        assert entitlements.get(s, tenant["comm_org_id"])["tenant_id"] == tenant["comm_org_id"]
        comm = s.query(CommercialOrganization).filter_by(id=tenant["comm_org_id"]).one()
        assert comm.country == "DE"
    finally:
        s.close()


def test_unchanged_country_does_not_churn_the_cache(client, Session, tenant):
    """Re-posting the same country must not drop a warm profile."""
    from app.commercial import entitlements
    from app.commercial.models import EntitlementsCache

    assert _post(client, country="gb").status_code == 200
    s = Session()
    try:
        entitlements.compute(s, tenant["comm_org_id"])
        s.commit()
    finally:
        s.close()

    assert _post(client, country="gb").status_code == 200

    s = Session()
    try:
        assert s.query(EntitlementsCache).filter_by(
            organization_id=tenant["comm_org_id"]).one_or_none() is not None
    finally:
        s.close()


def test_workspace_timezone_mirror_is_updated(client, Session, tenant):
    assert _post(client).status_code == 200
    s = Session()
    try:
        ws = s.query(CommercialWorkspace).filter_by(
            organization_id=tenant["comm_org_id"]).first()
        assert ws.timezone == "Europe/London"
    finally:
        s.close()


def test_the_workspace_slug_generated_at_signup_is_left_alone(client, Session, tenant):
    """`CommercialWorkspace.slug` is globally unique and already referenced.

    Step 2 stores the chosen name in `OrgSettings.slug`; re-keying the workspace
    row would risk the unique constraint for no benefit, since nothing routes by
    it.
    """
    before = Session().query(CommercialWorkspace).filter_by(
        organization_id=tenant["comm_org_id"]).first().slug
    assert _post(client).status_code == 200
    s = Session()
    try:
        assert s.query(CommercialWorkspace).filter_by(
            organization_id=tenant["comm_org_id"]).first().slug == before
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# Validation — reusing PUT /org/settings' rules, not a second set
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("bad_slug", ["Not Lowercase", "trailing-", "-leading",
                                      "double--hyphen", "under_score", "spaces here"])
def test_invalid_slug_is_rejected(client, bad_slug):
    assert _post(client, workspace_slug=bad_slug).status_code == 400


def test_invalid_country_is_rejected(client):
    assert _post(client, country="GBR").status_code in (400, 422)


@pytest.mark.parametrize("bad_tz", ["California, Sacramento", "IST", "GMT+5.5", "Not/AZone"])
def test_invalid_timezone_is_rejected(client, bad_tz):
    # The same `app._tz.is_valid_timezone` check PUT /org/settings applies —
    # this field decides the tenant's attendance day and payroll periods.
    assert _post(client, timezone=bad_tz).status_code == 400


def test_blank_organization_name_is_rejected(client):
    assert _post(client, organization_name="   ").status_code in (400, 422)


def test_unknown_worker_band_is_rejected_not_coerced(client):
    assert _post(client, worker_band="a-lot").status_code == 400


def test_a_rejected_request_writes_nothing(client, Session, tenant):
    """A validation failure late in the handler must not half-apply the form."""
    assert _post(client, timezone="Not/AZone").status_code == 400
    s = Session()
    try:
        assert s.query(Organization).filter_by(id=tenant["op_org_id"]).one().name \
            == "Ada's workspace"
        assert s.query(User).filter_by(id=tenant["admin_id"]).one().designation is None
        assert s.query(CommercialOrganization).filter_by(
            id=tenant["comm_org_id"]).one().expected_users == 1
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# Authorization
# ══════════════════════════════════════════════════════════════════════════
def test_a_non_admin_is_refused(Session, tenant):
    s = Session()
    member = User(name="Bob", email=f"member-{tenant['op_org_id']}@engines.example.com",
                  password=hash_password("Passw0rd!23"), role="employee",
                  organization_id=tenant["op_org_id"], is_active=True)
    s.add(member)
    s.commit()
    s.refresh(member)
    s.expunge(member)
    s.close()

    app = FastAPI()
    app.include_router(onboarding_mod.router)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: member
    c = TestClient(app, raise_server_exceptions=True)

    assert c.post("/api/v1/onboarding/organization", json=VALID).status_code == 403


def test_an_org_less_caller_is_refused(Session):
    s = Session()
    stray = User(name="Nobody", email=f"stray-{int(time.time()*1e6) % 10**7}@x.example.com",
                 password=hash_password("Passw0rd!23"), role="admin",
                 organization_id=None, is_active=True)
    s.add(stray)
    s.commit()
    s.refresh(stray)
    s.expunge(stray)
    s.close()

    app = FastAPI()
    app.include_router(onboarding_mod.router)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: stray
    c = TestClient(app, raise_server_exceptions=True)

    assert c.post("/api/v1/onboarding/organization", json=VALID).status_code == 409


def test_no_organization_id_is_accepted_from_the_caller(client, Session, tenant):
    """Tenant identity comes from the session, never the request body."""
    other = ids.org_id()
    s = Session()
    s.add(CommercialOrganization(id=other, name="Victim Corp",
                                 commercial_state="trial_active"))
    s.commit()
    s.close()

    r = client.post("/api/v1/onboarding/organization",
                    json={**VALID, "organization_id": other,
                          "operational_org_id": 999999})
    assert r.status_code == 200

    s = Session()
    try:
        # The extra fields were ignored, not honoured.
        assert s.query(CommercialOrganization).filter_by(id=other).one().name == "Victim Corp"
        assert s.query(CommercialOrganization).filter_by(
            id=tenant["comm_org_id"]).one().name == "Analytical Engines Ltd"
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# The wizard, and idempotency
# ══════════════════════════════════════════════════════════════════════════
def test_the_setup_wizard_is_closed_not_left_open_at_teams(client, Session, tenant):
    """Regression: the old wizard reappearing mid-flow.

    Signup opens the wizard at "organization". This step used to advance it to
    "teams", which leaves `setup_required` true — and `SetupGate` re-checks that
    on every navigation, so the admin was thrown out of the activation flow into
    the old three-step wizard's Teams screen. The four-step flow replaced that
    wizard and has no teams step, so the honest end state is complete.
    """
    from app.org import setup_state

    s = Session()
    assert setup_state.get_state(s, tenant["op_org_id"])["current_step"] == "organization"
    s.close()

    body = _post(client).json()
    assert body["current_step"] == "complete"
    assert body["completed"] is True
    # The flag SetupGate actually reads.
    assert body["setup_required"] is False


def test_setup_gate_has_nothing_to_redirect_to_after_step_2(client, Session, tenant):
    """The gate's own question, asked the way the gate asks it."""
    from app.org import setup_state

    _post(client)

    s = Session()
    try:
        assert setup_state.setup_required(s, tenant["op_org_id"]) is False
    finally:
        s.close()


def test_submitting_twice_is_idempotent(client, Session, tenant):
    """Idempotent by construction: every write is an UPDATE to a fixed value and
    `setup_state.set_step` is monotonic, so a double-click converges."""
    first = _post(client)
    second = _post(client)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["current_step"] == "complete"

    # Scoped to this tenant: the suite's engine is session-scoped, so other
    # tests in this file have renamed their own organisations to the same
    # string. What matters is that THIS tenant produced no second row.
    s = Session()
    try:
        assert s.query(Organization).filter_by(id=tenant["op_org_id"]).count() == 1
        assert s.query(CommercialOrganization).filter_by(
            operational_org_id=tenant["op_org_id"]).count() == 1
        assert s.query(OrgSettings).filter_by(organization_id=tenant["op_org_id"]).count() == 1
        assert s.query(CommercialWorkspace).filter_by(
            organization_id=tenant["comm_org_id"]).count() == 1
    finally:
        s.close()


def test_a_second_submit_cannot_rewind_a_completed_wizard(client, Session, tenant):
    from app.org import setup_state

    s = Session()
    setup_state.mark_complete(s, tenant["op_org_id"])
    s.commit()
    s.close()

    body = _post(client).json()
    assert body["completed"] is True
    assert body["current_step"] == "complete"


def test_the_response_carries_the_worker_bands(client):
    body = _post(client).json()
    bands = body["worker_count_bands"]
    assert [b["value"] for b in bands] == [b["value"] for b in constants.WORKER_COUNT_BANDS]
    # Served rather than hardcoded client-side, so label and stored value agree.
    assert all("label" in b and "expected_users" in b for b in bands)


def test_optional_fields_may_be_omitted(client, Session, tenant):
    r = client.post("/api/v1/onboarding/organization",
                    json={"organization_name": "Minimal Ltd"})
    assert r.status_code == 200, r.text
    s = Session()
    try:
        assert s.query(Organization).filter_by(id=tenant["op_org_id"]).one().name == "Minimal Ltd"
        # Nothing was blanked out by omission.
        assert s.query(CommercialOrganization).filter_by(
            id=tenant["comm_org_id"]).one().expected_users == 1
    finally:
        s.close()
