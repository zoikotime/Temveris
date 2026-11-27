"""`pending` on the per-type leave balance (admin leave review card).

`pending` is the subset of `used` still awaiting a decision (submitted /
under_review). It is reported separately so the reviewer can see decided vs
pending days; it must never change `used` or `remaining`.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models

LEAVE_TYPE = "Casual Leave"


@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


def _session(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _org(s) -> int:
    o = models.Organization(name=f"PEND-ORG-{uuid.uuid4().hex[:6]}")
    s.add(o); s.commit(); s.refresh(o)
    s.add(models.OrgSettings(organization_id=o.id, default_timezone="Asia/Kolkata"))
    s.commit()
    return o.id


def _user(s, org_id, role="employee") -> int:
    u = models.User(name=f"{role}-{uuid.uuid4().hex[:4]}", email=f"{uuid.uuid4().hex}@x.com",
                    password="x", role=role, is_active=True, organization_id=org_id)
    s.add(u); s.commit(); s.refresh(u)
    return u.id


def _leave(s, user_id, org_id, day_offset, status, days=1):
    from app.leave import policy as leave_policy
    start = leave_policy.leave_year_start(s, org_id) + timedelta(days=day_offset)
    s.add(models.LeaveRequest(
        user_id=user_id, organization_id=org_id, leave_type=LEAVE_TYPE,
        from_date=start, to_date=start + timedelta(days=days - 1),
        description="t", status=status, leave_days=days,
    ))
    s.commit()


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


@pytest.fixture(scope="module")
def seeded(engine, app_):
    s = _session(engine)
    org_id = _org(s)
    emp = _user(s, org_id)
    admin = _user(s, org_id, role="admin")
    _leave(s, emp, org_id, 10, "approved", days=2)
    _leave(s, emp, org_id, 20, "approved", days=1)
    _leave(s, emp, org_id, 30, "submitted", days=1)
    _leave(s, emp, org_id, 40, "under_review", days=1)
    _leave(s, emp, org_id, 50, "rejected", days=3)      # never counted
    other_org = _org(s)
    outsider = _user(s, other_org, role="admin")
    s.close()
    return {"org": org_id, "emp": emp, "admin": admin, "outsider": outsider}


def _balances(engine, user_id):
    from app.leave.router import _compute_user_balances
    s = _session(engine)
    try:
        user = s.get(models.User, user_id)
        return _compute_user_balances(user, s)
    finally:
        s.close()


def test_pending_counts_only_submitted_and_under_review(engine, seeded):
    b = _balances(engine, seeded["emp"])[LEAVE_TYPE]
    assert b["pending"] == 2.0


def test_pending_is_a_subset_of_used(engine, seeded):
    b = _balances(engine, seeded["emp"])[LEAVE_TYPE]
    # used = 2 + 1 approved + 1 submitted + 1 under_review; rejected excluded.
    assert b["used"] == 5.0
    assert b["used"] - b["pending"] == 3.0          # decided days


def test_a_new_pending_request_moves_used_and_pending_together(engine):
    """`pending` is reporting only: a new submitted request changes `used`
    and `remaining` exactly as it did before this field existed."""
    s = _session(engine)
    org_id = _org(s)
    emp = _user(s, org_id)
    s.close()
    before = _balances(engine, emp)[LEAVE_TYPE]
    s = _session(engine)
    _leave(s, emp, org_id, 15, "submitted", days=1)
    s.close()
    after = _balances(engine, emp)[LEAVE_TYPE]
    assert after["used"] - before["used"] == 1.0
    assert after["pending"] - before["pending"] == 1.0
    assert after["remaining"] == max(0.0, before["remaining"] - 1.0)


def test_every_type_reports_pending(engine, seeded):
    for key, row in _balances(engine, seeded["emp"]).items():
        if key.startswith("_"):
            continue
        assert "pending" in row and row["pending"] >= 0


def test_admin_endpoint_returns_pending(app_, seeded):
    client = TestClient(app_)
    res = client.get(f"/leave/admin/balances/{seeded['emp']}", headers=_auth(seeded["admin"]))
    assert res.status_code == 200, res.text
    assert res.json()["balances"][LEAVE_TYPE]["pending"] == 2.0


def test_other_tenant_admin_cannot_read_the_balance(app_, seeded):
    client = TestClient(app_)
    res = client.get(f"/leave/admin/balances/{seeded['emp']}", headers=_auth(seeded["outsider"]))
    assert res.status_code == 404
