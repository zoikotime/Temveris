"""
The admin balances endpoint says which leave year its figures cover, so the
leave review modal can show the right "Policy Year" for organizations whose
leave year does not start in January.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models
from app.leave import policy as leave_policy


@pytest.mark.parametrize("start, label, end", [
    (datetime(2026, 1, 1), "2026", "2026-12-31"),
    (datetime(2026, 4, 1), "Apr 2026 – Mar 2027", "2027-03-31"),
    (datetime(2025, 7, 1), "Jul 2025 – Jun 2026", "2026-06-30"),
    (datetime(2027, 3, 1), "Mar 2027 – Feb 2028", "2028-02-29"),   # leap February
    (date(2026, 12, 1), "Dec 2026 – Nov 2027", "2027-11-30"),
])
def test_describe_leave_year(start, label, end):
    out = leave_policy.describe_leave_year(start)
    assert out == {"start": start.strftime("%Y-%m-%d"), "end": end, "label": label}


@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


def test_admin_balances_report_the_leave_year(app_, engine):
    s = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    org = models.Organization(name=f"LY-ORG-{uuid.uuid4().hex[:6]}")
    s.add(org); s.commit(); s.refresh(org)
    s.add(models.OrgSettings(organization_id=org.id, default_timezone="Asia/Kolkata"))
    users = []
    for role in ("employee", "admin"):
        u = models.User(name=f"ly-{role}", email=f"{uuid.uuid4().hex}@x.com", password="x",
                        role=role, is_active=True, organization_id=org.id)
        s.add(u); s.commit(); s.refresh(u)
        users.append(u.id)
    expected = leave_policy.describe_leave_year(leave_policy.leave_year_start(s, org.id))
    s.close()

    emp, admin = users
    res = TestClient(app_).get(f"/leave/admin/balances/{emp}", headers=_auth(admin))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["leave_year"] == expected
    assert body["leave_year"]["label"] == expected["start"][:4]   # default January start
    assert "balances" in body
