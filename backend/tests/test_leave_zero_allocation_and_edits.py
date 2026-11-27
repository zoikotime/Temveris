"""An HR allocation of 0 blocks a leave type — on apply AND on edit.

Pins three fixes:
  * a span with no working day (weekend / holiday) costs 0 and used to pass
    every balance check, including an allocation of 0 — now refused;
  * an employee's EDIT of type / dates / half-day re-runs the apply checks
    (it ran none, so Unpaid could be edited into a blocked type);
  * an edit re-costs the stored day count.
And the contract HR relies on: allocation 0 blocks every capped type, never
the uncapped ones (Unpaid, Compensatory, Company).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models

CAPPED_TYPES = [
    "Annual Leave", "Casual Leave", "Sick Leave", "Emergency Leave",
    "Child Care Leave", "Parent Care Leave", "Medical Escort Leave",
    "Bereavement Leave", "Maternity Leave", "Paternity Leave", "Adoption Leave",
    "Half Day Leave",
]
UNCAPPED_TYPES = ["Unpaid Leave", "Compensatory Leave", "Company Leave"]
NO_BALANCE = "Insufficient leave balance"
NO_WORKING = "non-working days"


@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


def _session(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _weekday(min_days_ahead: int, weekday: int) -> datetime:
    """A date at least `min_days_ahead` out that falls on `weekday` (Mon=0)."""
    d = datetime.now() + timedelta(days=min_days_ahead)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return datetime(d.year, d.month, d.day)


WED = _weekday(90, 2)          # beyond every notice window (Maternity: 60)
WED_LATER = WED + timedelta(days=7)
SUN = _weekday(90, 6)


@pytest.fixture(scope="module")
def world(engine, app_):
    s = _session(engine)
    o = models.Organization(name=f"ZERO-ORG-{uuid.uuid4().hex[:6]}")
    s.add(o); s.commit(); s.refresh(o)
    s.add(models.OrgSettings(organization_id=o.id, default_timezone="Asia/Kolkata"))
    intern = models.User(name="Intern", email=f"{uuid.uuid4().hex}@x.com", password="x",
                         role="employee", is_active=True, organization_id=o.id,
                         official_start_date=datetime(2024, 1, 1))
    admin = models.User(name="Admin", email=f"{uuid.uuid4().hex}@x.com", password="x",
                        role="admin", is_active=True, organization_id=o.id)
    s.add_all([intern, admin]); s.commit()
    for lt in CAPPED_TYPES + UNCAPPED_TYPES:
        s.add(models.EmployeeLeaveBalance(user_id=intern.id, organization_id=o.id,
                                          leave_type=lt, allocated_days=0))
    s.commit()
    ids = {"org": o.id, "intern": intern.id, "admin": admin.id}
    s.close()
    return ids


def _validate(engine, world, leave_type, day, **kw):
    from app.leave.router import _run_validation
    s = _session(engine)
    try:
        errors, flags, _ = _run_validation(world["intern"], leave_type, day, day,
                                           True, s, org_id=world["org"], **kw)
        return errors, flags
    finally:
        s.close()


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


def _request(engine, world, leave_type, day, days=1.0):
    s = _session(engine)
    r = models.LeaveRequest(user_id=world["intern"], organization_id=world["org"],
                            leave_type=leave_type, from_date=day, to_date=day,
                            description="t", status="submitted", leave_days=days)
    s.add(r); s.commit(); s.refresh(r)
    rid = r.id
    s.close()
    return rid


def _row(engine, rid):
    s = _session(engine)
    try:
        r = s.get(models.LeaveRequest, rid)
        return r.leave_type, r.leave_days, r.from_date
    finally:
        s.close()


# ── allocation 0 on apply ────────────────────────────────────────────────────

@pytest.mark.parametrize("leave_type", CAPPED_TYPES)
def test_allocation_zero_blocks_every_capped_type(engine, world, leave_type):
    errors, flags = _validate(engine, world, leave_type, WED)
    assert any(NO_BALANCE in e for e in errors), errors
    assert "INSUFFICIENT_BALANCE" in flags


@pytest.mark.parametrize("leave_type", UNCAPPED_TYPES)
def test_allocation_zero_never_blocks_uncapped_types(engine, world, leave_type):
    errors, _ = _validate(engine, world, leave_type, WED)
    assert not any(NO_BALANCE in e for e in errors), errors


# ── no working day in the span ───────────────────────────────────────────────

@pytest.mark.parametrize("leave_type", ["Annual Leave", "Casual Leave", "Unpaid Leave"])
def test_a_weekend_only_request_is_refused(engine, world, leave_type):
    errors, flags = _validate(engine, world, leave_type, SUN)
    assert any(NO_WORKING in e for e in errors), errors
    assert "NO_WORKING_DAYS" in flags


def test_a_weekday_unpaid_request_is_still_allowed(engine, world):
    errors, _ = _validate(engine, world, "Unpaid Leave", WED)
    assert errors == []


# ── edits ────────────────────────────────────────────────────────────────────

def test_employee_cannot_edit_an_allowed_request_into_a_blocked_type(engine, world, app_):
    rid = _request(engine, world, "Unpaid Leave", WED)
    res = TestClient(app_).put(f"/leave/{rid}", json={"leave_type": "Casual Leave"},
                               headers=_auth(world["intern"]))
    assert res.status_code == 422, res.text
    assert NO_BALANCE in res.text
    assert _row(engine, rid)[0] == "Unpaid Leave"          # unchanged


def test_employee_cannot_edit_dates_onto_a_weekend(engine, world, app_):
    rid = _request(engine, world, "Unpaid Leave", WED)
    res = TestClient(app_).put(f"/leave/{rid}",
                               json={"from_date": SUN.strftime("%Y-%m-%d"),
                                     "to_date": SUN.strftime("%Y-%m-%d")},
                               headers=_auth(world["intern"]))
    assert res.status_code == 422, res.text
    assert _row(engine, rid)[2] == WED


def test_editing_only_the_description_is_not_rechecked(engine, world, app_):
    # Inside every notice window — a re-check would refuse it.
    soon = _weekday(1, 2)
    rid = _request(engine, world, "Unpaid Leave", soon)
    res = TestClient(app_).put(f"/leave/{rid}", json={"description": "updated"},
                               headers=_auth(world["intern"]))
    assert res.status_code == 200, res.text


def test_an_edit_re_costs_the_stored_days(engine, world, app_):
    rid = _request(engine, world, "Unpaid Leave", WED)
    friday = WED + timedelta(days=2)
    res = TestClient(app_).put(f"/leave/{rid}",
                               json={"from_date": WED.strftime("%Y-%m-%d"),
                                     "to_date": friday.strftime("%Y-%m-%d")},
                               headers=_auth(world["intern"]))
    assert res.status_code == 200, res.text
    assert _row(engine, rid)[1] == 3.0                      # Wed-Fri, was 1


def test_an_edit_is_not_counted_against_itself(engine, world, app_):
    """One Casual day allowed, one Casual day requested: moving it to another
    weekday must succeed (it would fail if the request counted against itself)."""
    s = _session(engine)
    row = s.query(models.EmployeeLeaveBalance).filter_by(
        user_id=world["intern"], leave_type="Casual Leave").one()
    row.allocated_days = 1
    s.commit(); s.close()
    try:
        rid = _request(engine, world, "Casual Leave", WED)
        res = TestClient(app_).put(f"/leave/{rid}",
                                   json={"from_date": WED_LATER.strftime("%Y-%m-%d"),
                                         "to_date": WED_LATER.strftime("%Y-%m-%d")},
                                   headers=_auth(world["intern"]))
        assert res.status_code == 200, res.text
    finally:
        s = _session(engine)
        s.query(models.LeaveRequest).filter_by(user_id=world["intern"],
                                               leave_type="Casual Leave").delete()
        s.query(models.EmployeeLeaveBalance).filter_by(
            user_id=world["intern"], leave_type="Casual Leave").one().allocated_days = 0
        s.commit(); s.close()


def test_admin_cannot_edit_a_request_onto_a_weekend_either(engine, world, app_):
    rid = _request(engine, world, "Unpaid Leave", WED)
    res = TestClient(app_).put(f"/leave/{rid}",
                               json={"from_date": SUN.strftime("%Y-%m-%d"),
                                     "to_date": SUN.strftime("%Y-%m-%d")},
                               headers=_auth(world["admin"]))
    assert res.status_code == 422, res.text
