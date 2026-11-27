"""HR "Leave Request" email: no fake balance for types with no annual limit.

An Unpaid Leave request used to tell HR "The employee has 87.0 day(s) of leave
remaining for Unpaid Leave" — allowance-minus-used computed for a type that has
no limit. Types with no annual limit now say so; capped types are unchanged.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from database import models


def _email_html(monkeypatch, **kw):
    from app import email_service
    captured = {}
    monkeypatch.setattr(email_service, "_send_all",
                        lambda recipients, subject, html, **k: captured.setdefault("html", html) or True)
    email_service.send_leave_request_to_admin(
        recipients=["hr@org.test"], leave_id=1, employee_name="Radha",
        employee_email="radha@org.test", from_date="2026-11-19", to_date="2026-12-06",
        description="d", **kw)
    return captured["html"]


def test_uncapped_type_says_no_limit_instead_of_a_number(monkeypatch):
    html = _email_html(monkeypatch, leave_type="Unpaid Leave",
                       balance_remaining=87.0, balance_unlimited=True)
    assert "no annual limit" in html
    assert "87.0" not in html


def test_capped_type_still_shows_remaining_days(monkeypatch):
    html = _email_html(monkeypatch, leave_type="Annual Leave", balance_remaining=7.5)
    assert "7.5 day(s)" in html and "no annual limit" not in html


@pytest.mark.parametrize("row, expected", [
    ({"is_unlimited": True}, True),                       # Unpaid / Company
    ({"no_annual_limit": True, "is_earned": True}, True),  # Compensatory
    ({"is_unlimited": True, "has_override": True}, True),  # overrides never cap these
    ({"remaining": 7.5}, False),                          # Annual / Casual / ...
    ({}, False),
])
def test_uncapped_rule_matches_the_apply_check(row, expected):
    from app.leave.router import _balance_is_uncapped
    assert _balance_is_uncapped(row) is expected


# ── end to end: the apply route hands the flag to the email ─────────────────

@pytest.fixture(scope="module")
def app_(engine):
    from database.models import Base
    Base.metadata.create_all(engine)
    import main
    return main.app


def _auth(user_id):
    from app.auth.service import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'user_id': user_id})}"}


def _next_weekday(min_days):
    d = date.today() + timedelta(days=min_days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


@pytest.mark.parametrize("leave_type, uncapped", [("Unpaid Leave", True), ("Annual Leave", False)])
def test_apply_passes_the_no_limit_flag_to_the_hr_email(engine, app_, monkeypatch, leave_type, uncapped):
    from app.leave import router as leave_router
    calls = []
    monkeypatch.setattr(leave_router, "send_leave_request_to_admin", lambda **kw: calls.append(kw))
    monkeypatch.setattr(leave_router, "send_leave_submitted_to_employee", lambda *a, **k: None)

    s = sessionmaker(bind=engine)()
    org = models.Organization(name=f"EMAILBAL-{uuid.uuid4().hex[:6]}")
    s.add(org); s.commit()
    s.add(models.OrgSettings(organization_id=org.id, default_timezone="Asia/Kolkata"))
    u = models.User(name="Radha", email=f"{uuid.uuid4().hex}@x.test", password="x", role="employee",
                    is_active=True, organization_id=org.id, official_start_date=datetime(2024, 1, 1))
    s.add(u); s.commit(); uid = u.id; s.close()

    day = _next_weekday(30).isoformat()
    res = TestClient(app_).post("/leave/apply", headers=_auth(uid), data={
        "leave_type": leave_type, "from_date": day, "to_date": day, "description": "test"})
    assert res.status_code in (200, 201), res.text
    assert len(calls) == 1
    assert calls[0]["balance_unlimited"] is uncapped
