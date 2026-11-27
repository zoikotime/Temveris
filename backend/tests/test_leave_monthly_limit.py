"""Per-calendar-month limit on a leave type (organization-configured).

Temveris's rule: at most 2 Compensatory Leave days per month, changeable
later (e.g. to 3) by admin/HR. Compensatory Leave is uncapped — the balance
check skips it — so the monthly limit is the only cap it has.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models

COMP = "Compensatory Leave"
LIMIT_ERR = "per month"


@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


def _session(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


def _weekdays_of(y, m):
    d, out = date(y, m, 1), []
    while d.month == m:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# A month comfortably past every notice window.
_far = date.today() + timedelta(days=100)
Y, M = _far.year, _far.month
WD = _weekdays_of(Y, M)                         # weekdays of that month
NEXT = date(Y + (M == 12), 1 if M == 12 else M + 1, 1)
WD_NEXT = _weekdays_of(NEXT.year, NEXT.month)


def _dt(d):
    return datetime(d.year, d.month, d.day)


def _org(s):
    o = models.Organization(name=f"MLIM-{uuid.uuid4().hex[:6]}")
    s.add(o); s.commit(); s.refresh(o)
    s.add(models.OrgSettings(organization_id=o.id, default_timezone="Asia/Kolkata"))
    s.commit()
    return o.id


def _user(s, org_id, role="employee"):
    u = models.User(name=role, email=f"{uuid.uuid4().hex}@x.com", password="x", role=role,
                    is_active=True, organization_id=org_id, official_start_date=datetime(2024, 1, 1))
    s.add(u); s.commit(); s.refresh(u)
    return u.id


def _leave(engine, uid, org_id, start, end=None, status="approved", half=False, lt=COMP):
    s = _session(engine)
    r = models.LeaveRequest(user_id=uid, organization_id=org_id, leave_type=lt,
                            from_date=_dt(start), to_date=_dt(end or start), description="t",
                            status=status, is_half_day=half, leave_days=0.5 if half else 1)
    s.add(r); s.commit(); s.refresh(r)
    rid = r.id
    s.close()
    return rid


def _check(engine, uid, org_id, start, end=None, half=False, exclude=None):
    from app.leave.router import _run_validation
    s = _session(engine)
    try:
        errors, flags, _ = _run_validation(uid, COMP, _dt(start), _dt(end or start), True, s,
                                           org_id=org_id, is_half_day=half, exclude_leave_id=exclude)
        return errors, flags
    finally:
        s.close()


def _blocked(result):
    errors, flags = result
    return "MONTHLY_LIMIT" in flags and any(LIMIT_ERR in e for e in errors)


@pytest.fixture()
def org2(engine, app_):
    """A fresh org whose admin set Compensatory to 2 days/month via the API."""
    s = _session(engine)
    org_id = _org(s)
    emp, admin = _user(s, org_id), _user(s, org_id, "admin")
    s.close()
    res = TestClient(app_).put(f"/leave/admin/types/{COMP}/monthly-limit",
                               json={"monthly_limit": 2}, headers=_auth(admin))
    assert res.status_code == 200, res.text
    return {"org": org_id, "emp": emp, "admin": admin}


def test_third_day_in_the_same_month_is_refused(engine, org2):
    _leave(engine, org2["emp"], org2["org"], WD[0])
    _leave(engine, org2["emp"], org2["org"], WD[1], status="submitted")   # pending counts
    assert _blocked(_check(engine, org2["emp"], org2["org"], WD[2]))


def test_up_to_the_limit_is_allowed(engine, org2):
    _leave(engine, org2["emp"], org2["org"], WD[0])
    assert _check(engine, org2["emp"], org2["org"], WD[1])[0] == []


def test_next_month_has_its_own_allowance(engine, org2):
    _leave(engine, org2["emp"], org2["org"], WD[0])
    _leave(engine, org2["emp"], org2["org"], WD[1])
    assert not _blocked(_check(engine, org2["emp"], org2["org"], WD_NEXT[0]))


def test_rejected_requests_do_not_count(engine, org2):
    _leave(engine, org2["emp"], org2["org"], WD[0], status="rejected")
    _leave(engine, org2["emp"], org2["org"], WD[1], status="rejected")
    assert not _blocked(_check(engine, org2["emp"], org2["org"], WD[2]))


def test_half_days_count_as_half(engine, org2):
    _leave(engine, org2["emp"], org2["org"], WD[0])
    _leave(engine, org2["emp"], org2["org"], WD[1], half=True)            # 1.5 used
    assert not _blocked(_check(engine, org2["emp"], org2["org"], WD[2], half=True))   # = 2.0
    _leave(engine, org2["emp"], org2["org"], WD[2], half=True)            # 2.0 used
    assert _blocked(_check(engine, org2["emp"], org2["org"], WD[3], half=True))


def test_a_multi_day_request_over_the_limit_is_refused(engine, org2):
    assert _blocked(_check(engine, org2["emp"], org2["org"], WD[0], WD[2]))   # 3 weekdays


def test_a_span_across_two_months_is_counted_in_each_month(engine, org2):
    last, first = WD[-1], WD_NEXT[0]
    _leave(engine, org2["emp"], org2["org"], WD[0])
    _leave(engine, org2["emp"], org2["org"], WD[1])                        # this month full
    assert _blocked(_check(engine, org2["emp"], org2["org"], last, first))   # 1 day lands here


def test_an_edited_request_is_not_counted_against_itself(engine, org2):
    _leave(engine, org2["emp"], org2["org"], WD[0])
    rid = _leave(engine, org2["emp"], org2["org"], WD[1], status="submitted")
    assert not _blocked(_check(engine, org2["emp"], org2["org"], WD[2], exclude=rid))


def test_limit_can_be_raised_to_three(engine, org2, app_):
    _leave(engine, org2["emp"], org2["org"], WD[0])
    _leave(engine, org2["emp"], org2["org"], WD[1])
    assert _blocked(_check(engine, org2["emp"], org2["org"], WD[2]))
    res = TestClient(app_).put(f"/leave/admin/types/{COMP}/monthly-limit",
                               json={"monthly_limit": 3}, headers=_auth(org2["admin"]))
    assert res.status_code == 200
    assert not _blocked(_check(engine, org2["emp"], org2["org"], WD[2]))


def test_clearing_the_limit_removes_it(engine, org2, app_):
    _leave(engine, org2["emp"], org2["org"], WD[0])
    _leave(engine, org2["emp"], org2["org"], WD[1])
    res = TestClient(app_).put(f"/leave/admin/types/{COMP}/monthly-limit",
                               json={"monthly_limit": None}, headers=_auth(org2["admin"]))
    assert res.status_code == 200
    assert not _blocked(_check(engine, org2["emp"], org2["org"], WD[2]))


def test_no_limit_configured_means_no_monthly_cap(engine):
    s = _session(engine)
    org_id = _org(s); emp = _user(s, org_id)
    s.close()
    for d in WD[:4]:
        _leave(engine, emp, org_id, d)
    assert not _blocked(_check(engine, emp, org_id, WD[4]))


def test_one_orgs_limit_does_not_affect_another(engine, org2):
    s = _session(engine)
    other = _org(s); emp = _user(s, other)
    s.close()
    _leave(engine, emp, other, WD[0])
    _leave(engine, emp, other, WD[1])
    assert not _blocked(_check(engine, emp, other, WD[2]))


def test_the_endpoint_changes_only_the_monthly_limit(engine, org2, app_):
    res = TestClient(app_).get("/leave/types", headers=_auth(org2["emp"]))
    comp = next(t for t in res.json() if t["leave_type"] == COMP)
    assert comp["monthly_limit"] == 2
    assert comp["notice_days"] == 7                     # untouched shipped rule
    s = _session(engine)
    stored = s.query(models.AppSettings).filter_by(
        organization_id=org2["org"], key=f"leave_policies:{org2['org']}").one()
    assert json.loads(stored.value)[COMP] == {"monthly_limit": 2}
    s.close()


@pytest.mark.parametrize("value", [-1, 32])
def test_out_of_range_values_are_rejected(org2, app_, value):
    res = TestClient(app_).put(f"/leave/admin/types/{COMP}/monthly-limit",
                               json={"monthly_limit": value}, headers=_auth(org2["admin"]))
    assert res.status_code == 400


def test_an_employee_cannot_set_the_limit(org2, app_):
    res = TestClient(app_).put(f"/leave/admin/types/{COMP}/monthly-limit",
                               json={"monthly_limit": 30}, headers=_auth(org2["emp"]))
    assert res.status_code == 403


def test_unknown_leave_type_is_404(org2, app_):
    res = TestClient(app_).put("/leave/admin/types/Nope Leave/monthly-limit",
                               json={"monthly_limit": 2}, headers=_auth(org2["admin"]))
    assert res.status_code == 404
