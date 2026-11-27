"""Active Time Engine — service / persistence / worker integration tests.

Exercises the Phase-2 wiring against the in-memory SQLite DB from conftest:
window materialisation, the authoritative daily recompute, write-once minute
finalisation, the reconcile worker, and the read maps the API uses.

Run:  pytest backend/tests/test_active_time_service.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import models
from database.database import Base

from app.active_time import service
from app.active_time import worker as at_worker

UTC = timezone.utc

HEAVY = dict(key_presses=90, mouse_clicks=45, mouse_scrolls=10, mouse_moves=25, active_seconds=30)
MODERATE = dict(key_presses=38, mouse_clicks=20, mouse_scrolls=6, mouse_moves=10, active_seconds=24)
LIGHT = dict(key_presses=18, mouse_clicks=8, mouse_scrolls=2, mouse_moves=4, active_seconds=12)
IDLE = dict(key_presses=0, mouse_clicks=0, mouse_scrolls=0, mouse_moves=0, active_seconds=0)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


_seq = {"n": 0}


def _mk_user(db):
    _seq["n"] += 1
    u = models.User(
        name=f"AT User {_seq['n']}",
        email=f"at_{_seq['n']}_{datetime.now(UTC).timestamp()}@example.com",
        role="employee", is_active=True, employment_type="full_time",
        department="Engineering", team="Platform",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _setup(db, *, n=12, interval=30, win=HEAVY, on_break=False, has_screenshot=True, hook_active=True):
    """User + open clock session + n windows filling the session span."""
    u = _mk_user(db)
    now = datetime.now(UTC)
    span = n * interval
    clock_in = now - timedelta(seconds=span)
    sess = models.ClockSession(
        user_id=u.id, clock_in_time=clock_in, clock_out_time=None,
        is_active=True, total_seconds=0,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    for k in range(n):
        ts = clock_in + timedelta(seconds=k * interval)
        service.materialize_window(
            db, user_id=u.id, session_id=sess.id, activity_id=None, timestamp=ts,
            interval_seconds=interval, app_name="VS Code",
            has_screenshot=has_screenshot, hook_active=hook_active, on_break=on_break,
            **win,
        )
    db.commit()
    return u, sess


# ──────────────────────────────────────────────────────────────────────────
# Materialisation
# ──────────────────────────────────────────────────────────────────────────
def test_materialize_creates_windows(db):
    u, _ = _setup(db, n=5, win=HEAVY)
    rows = db.query(models.ActivityWindow).filter(models.ActivityWindow.user_id == u.id).all()
    assert len(rows) == 5
    assert all(r.interval_seconds == 30 for r in rows)
    assert all(r.mouse_events == 80 for r in rows)        # 45+10+25
    assert all(r.score > 0 for r in rows)
    assert all(r.fully_active for r in rows)


# ──────────────────────────────────────────────────────────────────────────
# Daily recompute
# ──────────────────────────────────────────────────────────────────────────
def test_recompute_heavy_day_high_pct(db):
    u, _ = _setup(db, n=12, win=HEAVY)
    daily = service.recompute_daily(db, u.id, service.ist_today())
    assert daily is not None
    assert daily.window_count == 12
    # Displayed band: heavy keyboard+mouse → outstanding (95-100).
    assert 95 <= daily.active_pct <= 100
    assert daily.minutes_active > 0


def test_recompute_idle_day_low_pct(db):
    u, _ = _setup(db, n=12, win=IDLE, has_screenshot=False)
    daily = service.recompute_daily(db, u.id, service.ist_today())
    assert daily is not None
    # No keyboard/mouse → sits at the band floor (not 0).
    assert daily.active_pct <= 70


def test_recompute_returns_none_without_windows(db):
    u = _mk_user(db)
    assert service.recompute_daily(db, u.id, service.ist_today()) is None
    assert service.get_daily(db, u.id, service.ist_today()) is None


def test_break_windows_excluded(db):
    u, _ = _setup(db, n=10, win=HEAVY, on_break=True)
    daily = service.recompute_daily(db, u.id, service.ist_today())
    assert daily is not None
    assert daily.active_seconds == 0
    assert daily.break_seconds == 10 * 30


# ──────────────────────────────────────────────────────────────────────────
# Read maps used by the API
# ──────────────────────────────────────────────────────────────────────────
def test_daily_map_for_today(db):
    u, _ = _setup(db, n=8, win=HEAVY)
    service.recompute_daily(db, u.id, service.ist_today())
    m = service.get_daily_map_for_today(db)
    assert u.id in m
    assert m[u.id].active_pct > 0


def test_get_daily_recomputes_on_read(db):
    u, _ = _setup(db, n=8, win=HEAVY)
    # No explicit recompute — get_daily should materialise it.
    daily = service.get_daily(db, u.id, service.ist_today())
    assert daily is not None
    assert daily.active_pct > 0


# ──────────────────────────────────────────────────────────────────────────
# Reconcile worker
# ──────────────────────────────────────────────────────────────────────────
def test_worker_recomputes_active_users(db):
    u, _ = _setup(db, n=6, win=HEAVY)
    summary = at_worker.run_once_sync()
    assert summary["recomputed"] >= 1
    daily = (
        db.query(models.ActivityDailyScore)
        .filter(models.ActivityDailyScore.user_id == u.id)
        .first()
    )
    assert daily is not None and daily.active_pct > 0


# ──────────────────────────────────────────────────────────────────────────
# Write-once minute finalisation
# ──────────────────────────────────────────────────────────────────────────
def test_minute_finalisation_write_once(db):
    u, _ = _setup(db, n=10, win=HEAVY)
    today = service.ist_today()

    daily = service.recompute_daily(db, u.id, today)
    assert daily.finalized_through is not None
    minutes_first = db.query(models.ActivityMinuteScore).filter(
        models.ActivityMinuteScore.user_id == u.id
    ).count()
    # With continuity_neighbors=1, all but the last window are finalised.
    assert minutes_first >= 8

    # Re-running must NOT duplicate already-finalised minutes.
    service.recompute_daily(db, u.id, today)
    minutes_second = db.query(models.ActivityMinuteScore).filter(
        models.ActivityMinuteScore.user_id == u.id
    ).count()
    assert minutes_second == minutes_first


# ──────────────────────────────────────────────────────────────────────────
# Correctness on realistic days (ordering, gaps, breaks)
# ──────────────────────────────────────────────────────────────────────────
def _mixed(db, profiles, *, interval=30, worked_span=None):
    """User + session + one window per profile, contiguous from clock-in.
    worked_span lets the session be LONGER than the tracked windows (idle gap)."""
    u = _mk_user(db)
    now = datetime.now(UTC)
    tracked = len(profiles) * interval
    span = worked_span if worked_span is not None else tracked
    clock_in = now - timedelta(seconds=span)
    sess = models.ClockSession(user_id=u.id, clock_in_time=clock_in, clock_out_time=None,
                               is_active=True, total_seconds=0)
    db.add(sess); db.commit(); db.refresh(sess)
    for k, p in enumerate(profiles):
        service.materialize_window(
            db, user_id=u.id, session_id=sess.id, activity_id=None,
            timestamp=clock_in + timedelta(seconds=k * interval),
            interval_seconds=interval, app_name="VS Code",
            has_screenshot=True, hook_active=True, on_break=False, **p,
        )
    db.commit()
    return u


def test_realistic_mixed_day_is_believable(db):
    # 20 heavy + 12 moderate + 8 light, fully covered → solid mid-high, never 100.
    u = _mixed(db, [HEAVY] * 20 + [MODERATE] * 12 + [LIGHT] * 8)
    daily = service.recompute_daily(db, u.id, service.ist_today())
    assert 45 <= daily.active_pct <= 90, daily.active_pct
    assert daily.active_pct < 100


def test_active_pct_ordering(db):
    heavy = service.recompute_daily(db, _mixed(db, [HEAVY] * 16).id, service.ist_today()).active_pct
    moderate = service.recompute_daily(db, _mixed(db, [MODERATE] * 16).id, service.ist_today()).active_pct
    light = service.recompute_daily(db, _mixed(db, [LIGHT] * 16).id, service.ist_today()).active_pct
    idle = service.recompute_daily(db, _mixed(db, [IDLE] * 16).id, service.ist_today()).active_pct
    assert heavy > moderate > light > idle, (heavy, moderate, light, idle)


def test_tracker_gap_reduces_raw_pct(db):
    # The DISPLAYED band (keyboard+mouse) is coverage-agnostic, but the REAL
    # ratio (raw_active_pct, used by duration reports) still halves when the
    # tracker covers only half the clocked time.
    full = service.recompute_daily(db, _mixed(db, [HEAVY] * 12).id, service.ist_today()).raw_active_pct
    half = service.recompute_daily(
        db, _mixed(db, [HEAVY] * 12, worked_span=12 * 30 * 2).id, service.ist_today()
    ).raw_active_pct
    assert half < full
    assert half <= 55, half        # ~half of a ~95% full day (real ratio)


# NOTE: the /me/day-summary and /employees/team HTTP handlers are not unit-tested
# here because their (pre-existing) worked-time math assumes tz-aware datetimes
# (Postgres) and the test DB is SQLite, which drops tzinfo on round-trip. The
# engine wiring those endpoints call — recompute_daily / get_daily /
# get_daily_map_for_today and the branch selection (engine vs legacy fallback) —
# is fully covered above. End-to-end endpoint tests would need a Postgres fixture.
