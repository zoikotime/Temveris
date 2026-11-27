"""
/employees/team carries `half_day_leave` so the Team Directory can badge a
worker who clocked in for one half of the day and is on approved leave for the
other. Their status stays Working / Stopped (they did work); the badge explains
the short day. Full-day leave, pending leave and no leave carry None.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from database import models
from database.database import Base

_seq = itertools.count(976000)


@pytest.fixture(scope="module", autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)
    yield


def _user(db, org, role="employee", name="HD"):
    u = models.User(name=name, email=f"hd_{uuid.uuid4().hex}@x.com", password="x",
                    role=role, is_active=True, organization_id=org)
    db.add(u); db.commit(); db.refresh(u)
    return u


def _leave(db, user, org, day, *, status="approved", half=None):
    db.add(models.LeaveRequest(
        user_id=user.id, organization_id=org, leave_type="Compensatory Leave",
        from_date=day, to_date=day, description="", status=status,
        is_half_day=bool(half), half_day_period=half))
    db.commit()


def test_team_reports_half_day_leave_without_hiding_worked_time(db):
    from fastapi.testclient import TestClient
    from app.auth.service import create_access_token
    import main

    org = next(_seq)
    db.add(models.Organization(id=org, name=f"HDOrg{org}"))
    db.add(models.OrgSettings(organization_id=org, default_timezone="Asia/Kolkata"))
    db.commit()
    admin = _user(db, org, role="admin", name="HD-ADMIN")

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    day = datetime.combine(today, datetime.min.time())
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Worked the morning, clocked out, approved afternoon half-day.
    worked = _user(db, org, name="HD-WORKED")
    _leave(db, worked, org, day, half="second_half")
    db.add(models.ClockSession(
        user_id=worked.id, organization_id=org,
        clock_in_time=now - timedelta(minutes=30), clock_out_time=now - timedelta(minutes=5),
        is_active=False, total_seconds=1500, break_seconds=0))
    db.commit()

    full = _user(db, org, name="HD-FULL")
    _leave(db, full, org, day)

    pending = _user(db, org, name="HD-PENDING")
    _leave(db, pending, org, day, status="submitted", half="first_half")

    none = _user(db, org, name="HD-NONE")

    H = {"Authorization": f"Bearer {create_access_token({'user_id': admin.id})}"}
    r = TestClient(main.app, raise_server_exceptions=False).get("/employees/team", headers=H)
    assert r.status_code == 200, r.text[:300]
    rows = {row["id"]: row for row in r.json()}

    assert rows[worked.id]["half_day_leave"] == "second_half"
    assert rows[worked.id]["status"] != "On Leave"
    assert rows[worked.id]["clock_in_time"] is not None

    assert rows[full.id]["status"] == "On Leave"
    assert rows[full.id]["half_day_leave"] is None
    assert rows[pending.id]["half_day_leave"] is None
    assert rows[none.id]["half_day_leave"] is None
