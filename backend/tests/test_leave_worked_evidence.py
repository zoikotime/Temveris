"""GET /leave/{id}/worked-evidence — what Temveris recorded on each claimed
worked date of a Compensatory Leave request, for the reviewer's modal.

Worked time uses the shared session rule (app._worked) plus active manual time
adjustments; days are the organization's local days; reviewers only, and a
request from another organization is "not found".
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models

WORKED = "2026-09-05"      # sessions + screenshots recorded
EMPTY = "2026-09-06"       # nothing recorded
ADJUSTED = "2026-09-07"    # only a manual time adjustment


@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


def _user(s, org_id, role="employee"):
    u = models.User(name=role, email=f"{uuid.uuid4().hex}@x.test", password="x", role=role,
                    is_active=True, organization_id=org_id)
    s.add(u); s.commit(); s.refresh(u)
    return u.id


@pytest.fixture(scope="module")
def world(engine, app_):
    s = sessionmaker(bind=engine)()
    org = models.Organization(name=f"EVID-{uuid.uuid4().hex[:6]}")
    other = models.Organization(name=f"EVID2-{uuid.uuid4().hex[:6]}")
    s.add_all([org, other]); s.commit()
    s.add(models.OrgSettings(organization_id=org.id, default_timezone="Asia/Kolkata"))
    s.commit()
    emp, admin = _user(s, org.id), _user(s, org.id, "admin")
    bystander = _user(s, org.id)
    outsider = _user(s, other.id, "admin")

    # 5 Sep, IST: 09:00-13:00 (4h) and 14:00-17:30 (3h30m) -> 7h30m worked.
    for cin, cout, secs in [(datetime(2026, 9, 5, 3, 30), datetime(2026, 9, 5, 7, 30), 4 * 3600),
                            (datetime(2026, 9, 5, 8, 30), datetime(2026, 9, 5, 12, 0), 12600)]:
        s.add(models.ClockSession(user_id=emp, organization_id=org.id, clock_in_time=cin,
                                  clock_out_time=cout, total_seconds=secs, is_active=False))
    # Not counted: another employee that day, and this employee the day before.
    s.add(models.ClockSession(user_id=bystander, organization_id=org.id,
                              clock_in_time=datetime(2026, 9, 5, 4, 0),
                              clock_out_time=datetime(2026, 9, 5, 10, 0), total_seconds=21600))
    s.add(models.ClockSession(user_id=emp, organization_id=org.id,
                              clock_in_time=datetime(2026, 9, 4, 4, 0),
                              clock_out_time=datetime(2026, 9, 4, 10, 0), total_seconds=21600))
    for minute in (0, 10, 20):
        s.add(models.Activity(user_id=emp, organization_id=org.id,
                              timestamp=datetime(2026, 9, 5, 4, minute), file_path=f"s{minute}.png"))
    s.add(models.Activity(user_id=emp, organization_id=org.id,          # no image: not a screenshot
                          timestamp=datetime(2026, 9, 5, 5, 0), file_path=None))
    s.add(models.TimeAdjustment(user_id=emp, adjustment_date=date(2026, 9, 7),
                                seconds_added=2 * 3600, reason="power cut", is_active=True))
    req = models.LeaveRequest(user_id=emp, organization_id=org.id, leave_type="Compensatory Leave",
                              from_date=datetime(2026, 10, 21), to_date=datetime(2026, 10, 21),
                              description="comp", status="submitted", leave_days=1,
                              worked_dates=json.dumps([WORKED, EMPTY, ADJUSTED]))
    s.add(req); s.commit()
    ids = {"req": req.id, "emp": emp, "admin": admin, "outsider": outsider}
    s.close()
    return ids


def _get(app_, world, who):
    return TestClient(app_).get(f"/leave/{world['req']}/worked-evidence", headers=_auth(world[who]))


def _by_date(res):
    return {d["date"]: d for d in res.json()["dates"]}


def test_a_worked_day_reports_hours_times_and_screenshots(app_, world):
    res = _get(app_, world, "admin")
    assert res.status_code == 200, res.text
    d = _by_date(res)[WORKED]
    assert d["worked_seconds"] == 4 * 3600 + 12600            # 7h30m
    assert d["sessions"] == 2
    assert d["first_clock_in"] == "09:00"                      # org-local (IST)
    assert d["last_clock_out"] == "17:30"
    assert d["screenshots"] == 3
    assert d["still_clocked_in"] is False


def test_a_day_with_nothing_recorded_says_so(app_, world):
    d = _by_date(_get(app_, world, "admin"))[EMPTY]
    assert d["worked_seconds"] == 0 and d["sessions"] == 0 and d["screenshots"] == 0
    assert d["first_clock_in"] is None


def test_manual_time_adjustments_count_as_worked_time(app_, world):
    d = _by_date(_get(app_, world, "admin"))[ADJUSTED]
    assert d["worked_seconds"] == 2 * 3600
    assert d["adjustment_seconds"] == 2 * 3600 and d["sessions"] == 0


def test_dates_are_returned_in_claim_order(app_, world):
    assert [d["date"] for d in _get(app_, world, "admin").json()["dates"]] == [WORKED, EMPTY, ADJUSTED]


def test_another_organizations_admin_gets_not_found(app_, world):
    assert _get(app_, world, "outsider").status_code == 404


def test_the_employee_cannot_read_the_review_evidence(app_, world):
    assert _get(app_, world, "emp").status_code == 403


def test_unknown_request_is_not_found(app_, world):
    res = TestClient(app_).get("/leave/99999999/worked-evidence", headers=_auth(world["admin"]))
    assert res.status_code == 404
