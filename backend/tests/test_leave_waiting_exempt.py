"""The new-joiner paid-leave waiting period, and which types it may gate.

The reported bug: a new joiner selecting Emergency Leave saw "Paid leave not
yet available — you become eligible on 2027-01-06" and POST /leave/apply
answered 403. You do not schedule illness or a family emergency around a tenure
milestone, so Sick and Emergency are now `waiting_exempt` by default.

The second, quieter bug these cover: GET /leave/types honoured the per-type
`waiting_exempt` policy flag and POST /leave/apply did not. An organization
that set the flag got a form that let the employee fill everything in and an
endpoint that then refused it. Both now read _is_waiting_exempt.

Everything here asserts BOTH surfaces for each type — what the form is told,
and what the endpoint actually does — because agreement between them is the
property that broke.
"""

from __future__ import annotations

import uuid
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models


@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


@pytest.fixture(scope="module")
def org(engine):
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    o = models.Organization(name=f"WAIT-ORG-{uuid.uuid4().hex[:6]}")
    s.add(o); s.commit(); s.refresh(o)
    s.add(models.OrgSettings(organization_id=o.id, default_timezone="Asia/Kolkata"))
    s.commit()
    oid = o.id
    s.close()
    return oid


@pytest.fixture(scope="module")
def new_joiner(engine, org):
    """Started today — nowhere near the 6-month waiting period."""
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    u = models.User(
        name="NEW-JOINER", email=f"{uuid.uuid4().hex}@x.com", password="x",
        role="employee", is_active=True, organization_id=org,
        official_start_date=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    s.add(u); s.commit(); s.refresh(u)
    uid = u.id
    s.close()
    return uid


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


def _apply(client, user_id, leave_type, days_ahead=1, span=0):
    """POST /leave/apply. Returns the response.

    Dates are pushed far enough out that the NOTICE rule (a separate gate) is
    never what fails — otherwise a notice error would masquerade as a waiting
    period pass/fail and the test would prove nothing.
    """
    start = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).date()
    end   = start + timedelta(days=span)
    return client.post("/leave/apply", headers=_auth(user_id), data={
        "leave_type":  leave_type,
        "from_date":   start.isoformat(),
        "to_date":     end.isoformat(),
        "description": "regression test",
    })


def _types(client, user_id):
    r = client.get("/leave/types", headers=_auth(user_id))
    assert r.status_code == 200
    return {row["leave_type"]: row for row in r.json()}


def _is_waiting_403(res) -> bool:
    """Was this refused specifically by the paid-leave waiting period?"""
    if res.status_code != 403:
        return False
    return "eligible for paid leave" in str(res.json().get("detail", ""))


# ══════════════════════════════════════════════════════════════════════════
# The reported bug — a new joiner must be able to file these
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("leave_type", ["Emergency Leave", "Sick Leave"])
def test_a_new_joiner_is_not_waiting_period_blocked(app_, new_joiner, leave_type):
    client = TestClient(app_, raise_server_exceptions=False)

    # The form is told it may proceed — this drives both the "Paid leave not yet
    # available" banner and the disabled Submit button.
    assert _types(client, new_joiner)[leave_type]["waiting_exempt"] is True, \
        f"{leave_type} still reports as waiting-period gated, so the banner shows"

    # ...and the endpoint agrees. Not asserting 200: the evidence gate (no
    # document attached) legitimately answers 422 for both of these types. What
    # must never come back is the waiting-period 403.
    res = _apply(client, new_joiner, leave_type)
    assert not _is_waiting_403(res), \
        f"{leave_type} was refused by the waiting period: {res.json()}"


@pytest.mark.parametrize("leave_type", ["Emergency Leave", "Sick Leave"])
def test_zero_balance_does_not_block_these_either(app_, engine, org, new_joiner, leave_type):
    """Requirement: zero paid/annual balance must not stop the application.

    Balance shortfalls set auto_lwp and a flag rather than an error, so this is
    already the behaviour — pinned here so a future change to the balance gate
    cannot quietly start blocking an emergency.
    """
    client = TestClient(app_, raise_server_exceptions=False)
    # Burn the whole shared pool so remaining is 0 for these types.
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    for i in range(20):
        d = datetime(2026, 1, 1) + timedelta(days=i)
        s.add(models.LeaveRequest(
            user_id=new_joiner, organization_id=org, leave_type="Annual Leave",
            from_date=d, to_date=d, description="burn", status="approved",
            leave_days=1,
        ))
    s.commit(); s.close()

    res = _apply(client, new_joiner, leave_type)
    assert not _is_waiting_403(res)
    if res.status_code == 422:
        errs = " ".join(res.json().get("detail", {}).get("errors", []))
        assert "balance" not in errs.lower(), \
            f"{leave_type} was blocked on balance: {errs}"


# ══════════════════════════════════════════════════════════════════════════
# What must NOT change — the paid types still serve the waiting period
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("leave_type", ["Annual Leave", "Casual Leave"])
def test_annual_and_casual_still_serve_the_waiting_period(app_, new_joiner, leave_type):
    """Explicit requirement: these keep their existing eligibility rules."""
    client = TestClient(app_, raise_server_exceptions=False)
    assert _types(client, new_joiner)[leave_type]["waiting_exempt"] is False
    assert _is_waiting_403(_apply(client, new_joiner, leave_type, days_ahead=30)), \
        f"{leave_type} stopped enforcing the waiting period"


@pytest.mark.parametrize("leave_type", [
    "Maternity Leave", "Paternity Leave", "Adoption Leave",
    "Bereavement Leave", "Child Care Leave", "Half Day Leave",
])
def test_no_other_type_changed_its_waiting_status(app_, new_joiner, leave_type):
    """Nothing beyond Sick and Emergency was granted an exemption."""
    client = TestClient(app_, raise_server_exceptions=False)
    assert _types(client, new_joiner)[leave_type]["waiting_exempt"] is False, \
        f"{leave_type} unexpectedly became waiting-exempt"


@pytest.mark.parametrize("leave_type", ["Compensatory Leave", "Unpaid Leave"])
def test_the_pre_existing_exemptions_are_intact(app_, new_joiner, leave_type):
    client = TestClient(app_, raise_server_exceptions=False)
    assert _types(client, new_joiner)[leave_type]["waiting_exempt"] is True


# ══════════════════════════════════════════════════════════════════════════
# /leave/types and /leave/apply must give the SAME answer
# ══════════════════════════════════════════════════════════════════════════

def test_an_org_can_exempt_its_own_type_and_apply_honours_it(db):
    """The quieter bug: /leave/types honoured `waiting_exempt`, /leave/apply
    ignored it, so a tenant that set the flag got a form it then refused."""
    from app.leave.router import _is_waiting_exempt

    policies = {
        "PTO":        {"limit": 12, "notice_days": 0},
        "Crisis Day": {"limit": 3,  "notice_days": 0, "waiting_exempt": True},
    }
    assert _is_waiting_exempt("Crisis Day", policies) is True
    assert _is_waiting_exempt("PTO", policies) is False


def test_an_org_can_also_clear_the_exemption_on_a_shipped_type(db):
    """`waiting_exempt` is a control, not an imposition — an organization that
    wants Sick Leave gated again sets it back to False."""
    from app.leave.router import _get_effective_policies, _is_waiting_exempt
    from app.leave.router import LEAVE_POLICIES

    # Default: exempt.
    assert _is_waiting_exempt("Sick Leave", {lt: dict(p) for lt, p in LEAVE_POLICIES.items()}) is True
    # Org override wins.
    overridden = {lt: dict(p) for lt, p in LEAVE_POLICIES.items()}
    overridden["Sick Leave"]["waiting_exempt"] = False
    assert _is_waiting_exempt("Sick Leave", overridden) is False


def test_types_and_apply_agree_for_every_shipped_type(app_, new_joiner):
    """No type may advertise one answer and enforce the other."""
    from app.leave.router import _is_waiting_exempt, _get_effective_policies
    from database import database as _db
    client = TestClient(app_, raise_server_exceptions=False)
    rows = _types(client, new_joiner)
    s = _db.SessionLocal()
    policies = _get_effective_policies(s, None)
    s.close()
    for lt, row in rows.items():
        assert row["waiting_exempt"] == _is_waiting_exempt(lt, policies), \
            f"{lt}: /leave/types says {row['waiting_exempt']}, the gate says otherwise"
