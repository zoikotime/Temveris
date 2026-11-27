"""Attendance lifecycle regression tests.

Covers the clock-in → break → resume → clock-out state machine and, in
particular, the bug where POST /employees/clock-out was the ONLY transition
that failed to invalidate the desktop tracker's 60s status cache
(app.tracking.router._STATUS_CACHE). That gap left /tracking/ping reporting
``clocked_in: True`` for up to a minute after clock-out, so the tracker kept
screenshotting and the tray stayed on "Working".

These call the router functions directly (not over HTTP) so they need no auth
plumbing. SQLite returns naive datetimes, so we deliberately avoid the
clock-out-after-break path here (its break-seconds union math compares against
tz-aware UTC bounds, which only round-trips on Postgres) and exercise the break
duration math through the tz-safe /break/resume handler instead.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException

from database import models
from database.database import Base

from app.employees.router import (
    clock_in,
    clock_out,
    start_break as emp_start_break,
    _StartBreakBody,
)
from app.breaks.router import (
    resume_from_break,
    get_active_break,
    ResumePayload,
)
from app.tracking import router as tracking_router


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    """conftest only creates a subset of tables; the attendance flow needs the
    full schema (break_sessions, app_settings, live_statuses, outbox_events,
    audit_logs, …). create_all is idempotent via checkfirst."""
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def user(db):
    u = models.User(
        name="Test Worker",
        email=f"worker_{datetime.now(timezone.utc).timestamp()}@example.com",
        role="employee",
        is_active=True,
        employment_type="full_time",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    # Each test starts from a clean tracker cache.
    tracking_router._cache_invalidate(u.id)
    return u


def _open_session(db, user):
    return (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id == user.id,
            models.ClockSession.clock_out_time.is_(None),
        )
        .first()
    )


# ── Clock In ─────────────────────────────────────────────────────────────────
def test_clock_in_creates_session_and_reports_working(db, user):
    res = clock_in(db=db, user=user)
    assert res["success"] is True

    session = _open_session(db, user)
    assert session is not None
    assert session.clock_out_time is None

    ping = tracking_router.tracking_ping(db=db, user=user)
    assert ping["clocked_in"] is True
    assert ping["status"] == "working"


def test_duplicate_clock_in_is_rejected(db, user):
    first = clock_in(db=db, user=user)
    assert first["success"] is True
    second = clock_in(db=db, user=user)
    assert second["success"] is False  # "Already clocked in"

    open_sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id == user.id,
            models.ClockSession.clock_out_time.is_(None),
        )
        .count()
    )
    assert open_sessions == 1  # no duplicate session created


# ── Break flow ───────────────────────────────────────────────────────────────
def test_start_break_then_resume(db, user):
    clock_in(db=db, user=user)
    res = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=user)
    assert res["success"] is True

    active = get_active_break(db=db, user=user)
    assert active["on_break"] is True

    ping = tracking_router.tracking_ping(db=db, user=user)
    assert ping["on_break"] is True
    assert ping["status"] == "on_break"

    resumed = resume_from_break(
        body=ResumePayload(reason="Coffee"), db=db, user=user
    )
    assert resumed["success"] is True

    assert get_active_break(db=db, user=user)["on_break"] is False
    ping_after = tracking_router.tracking_ping(db=db, user=user)
    assert ping_after["on_break"] is False
    assert ping_after["status"] == "working"


def test_only_one_active_break_at_a_time(db, user):
    clock_in(db=db, user=user)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=user)
    second = emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=user)
    assert second["success"] is False  # "Already on break"

    active_breaks = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.user_id == user.id,
            models.BreakSession.break_end.is_(None),
            models.BreakSession.is_active.is_(True),
        )
        .count()
    )
    assert active_breaks == 1


def test_break_duration_is_recorded(db, user):
    clock_in(db=db, user=user)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=user)

    # Backdate the break start by 5 minutes so resume computes a real duration.
    brk = (
        db.query(models.BreakSession)
        .filter(models.BreakSession.user_id == user.id, models.BreakSession.break_end.is_(None))
        .first()
    )
    brk.break_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()

    res = resume_from_break(body=ResumePayload(reason="Lunch"), db=db, user=user)
    assert res["success"] is True
    # ~300s; allow slack for execution time.
    assert 295 <= res["break_seconds"] <= 360

    session = _open_session(db, user)
    assert session.break_seconds >= 295  # rolled up onto the parent session


def test_resume_with_no_active_break_errors(db, user):
    clock_in(db=db, user=user)
    with pytest.raises(HTTPException) as ei:
        resume_from_break(body=ResumePayload(reason="x"), db=db, user=user)
    assert ei.value.status_code == 400


# ── Clock Out ────────────────────────────────────────────────────────────────
def _complete_required_hours(db, user):
    """Backdate the open session so worked time exceeds any required-hours
    threshold. Phase-2 Controlled Clock-Out asks for a reason when clocking out
    below required hours on a non-flexible day; these tests are about session
    closure / cache behaviour, not the hours gate, so they use the normal
    complete-hours path."""
    s = _open_session(db, user)
    s.clock_in_time = datetime.now(timezone.utc) - timedelta(hours=10)
    db.commit()


def test_clock_out_from_working_closes_session(db, user):
    clock_in(db=db, user=user)
    _complete_required_hours(db, user)
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is True
    assert "worked_seconds" in res

    assert _open_session(db, user) is None  # session closed
    ping = tracking_router.tracking_ping(db=db, user=user)
    assert ping["clocked_in"] is False
    assert ping["status"] == "not_clocked_in"


def test_clock_out_blocked_while_on_break(db, user):
    clock_in(db=db, user=user)
    emp_start_break(body=_StartBreakBody(break_type="break"), db=db, user=user)
    with pytest.raises(HTTPException) as ei:
        clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert ei.value.status_code == 409  # must resume before clocking out

    assert _open_session(db, user) is not None  # session left open


def test_clock_out_invalidates_tracking_status_cache(db, user):
    """Regression guard for the root-cause fix.

    Before the fix, clock-out cleared only _TEAM_CACHE and left the per-user
    _STATUS_CACHE intact, so /tracking/ping kept returning clocked_in=True for
    up to 60s. Warm the cache, clock out, and assert the entry is gone.
    """
    clock_in(db=db, user=user)

    # Warm the tracker status cache the way a desktop poll would.
    warmed = tracking_router.tracking_ping(db=db, user=user)
    assert warmed["clocked_in"] is True
    assert tracking_router._cache_get(user.id) is not None

    _complete_required_hours(db, user)  # normal (complete-hours) clock-out path
    clock_out(background_tasks=BackgroundTasks(), db=db, user=user)

    # The clock-out must have purged the stale "working" entry.
    assert tracking_router._cache_get(user.id) is None

    # And a fresh poll now reflects reality.
    fresh = tracking_router.tracking_ping(db=db, user=user)
    assert fresh["clocked_in"] is False
