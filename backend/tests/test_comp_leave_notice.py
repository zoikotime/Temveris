"""Compensatory Leave advance-notice policy (2026-07).

Comp-off used to be exempt from the notice gate (apply directly for time
already worked). Per HR policy it now requires a minimum of 7 days advance
notice and is HARD-BLOCKED inside that window — a short-notice request is
rejected at submission (no DB row, no balance consumed), exactly like
Annual / Casual Leave. These tests pin that behaviour.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timezone, timedelta

import pytest

from database import models
from database.database import Base
from app.leave.router import _run_validation, _get_effective_policies

_seq = itertools.count(1)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_user(db):
    uid = next(_seq)
    u = models.User(
        name=f"CompUser {uid}",
        email=f"comp_{uid}@example.com",
        role="employee",
        is_active=True,
        employment_type="full_time",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_policy_table_requires_7_day_notice(db):
    pol = _get_effective_policies(db)["Compensatory Leave"]
    assert pol["notice_days"] == 7
    assert pol["hard_notice"] is True


def test_comp_leave_blocked_under_7_days_notice(db):
    u = _mk_user(db)
    start = datetime.now(timezone.utc) + timedelta(days=1)   # only 1 day notice
    errors, flags, _ = _run_validation(u.id, "Compensatory Leave", start, start, False, db)
    assert "NOTICE_BLOCKED" in flags
    assert any("7 days advance notice" in e for e in errors)


def test_comp_leave_allowed_with_enough_notice(db):
    u = _mk_user(db)
    start = datetime.now(timezone.utc) + timedelta(days=8)   # 8 days notice
    errors, flags, _ = _run_validation(u.id, "Compensatory Leave", start, start, False, db)
    assert "NOTICE_BLOCKED" not in flags
