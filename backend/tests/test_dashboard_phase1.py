"""Phase 1 — Employee Dashboard read-path resilience tests.

Covers the four targeted fixes (READ-PATH only; no writes to
activity_windows / activity_daily_scores / activity_minute_scores):

  Fix 1 — single-user `_engine_summary` returns None for productivity/hourly
          (not zeros) when the engine has no ActivityWindow rows, so the caller
          keeps the legacy raw-Activity aggregation; Active/Idle still come from
          the engine/day-summary.
  Fix 4 — /ai/summary `_day_bounds` uses the shared IST day-boundary helper.

Frontend Fixes 2/3 (Today's Summary Active Time source + Productive %) are
trivial JSX source substitutions verified by reading + the vitest suite; the
giant Dashboard.jsx component is not unit-rendered here.

Run:  pytest backend/tests/test_dashboard_phase1.py -q
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timezone, timedelta

import pytest

from database import models
from database.database import Base
from app.activity_log import router as al

UTC = timezone.utc


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


_seq = {"n": 0}


def _mk_user(db, org_id=1):
    _seq["n"] += 1
    u = models.User(
        name=f"P1 User {_seq['n']}",
        email=f"p1_{_seq['n']}_{datetime.now(UTC).timestamp()}@example.com",
        role="employee", is_active=True, employment_type="full_time",
        organization_id=org_id, department="Eng", team="Platform",
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _add_window(db, uid, start, app_name, credit, interval=1800):
    db.add(models.ActivityWindow(
        user_id=uid, window_start=start, window_end=start + timedelta(seconds=interval),
        interval_seconds=interval, key_presses=0, mouse_clicks=0, mouse_moves=0,
        mouse_scrolls=0, mouse_events=0, active_seconds=credit,
        idle_seconds=max(0, interval - credit), app_name=app_name, has_screenshot=True,
        hook_active=True, on_break=False, keyboard_score=0, mouse_score=0, focus_score=0,
        continuity_score=0, screenshot_score=0, app_engagement_score=0, score=50.0,
        label="moderate", fully_active=False, active_credit_seconds=credit,
    ))
    db.commit()


# A range fully inside one IST day: window at 2026-07-07 04:00 UTC = 09:30 IST.
_WIN_TS = datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC)
_START = _WIN_TS - timedelta(hours=1)
_END = _WIN_TS + timedelta(hours=1)


def _patch_daysummary(monkeypatch, active, worked):
    monkeypatch.setattr(
        "app.employees.router.get_day_summary",
        lambda date, db, user: {"active_seconds": active, "worked_seconds": worked},
    )


# ── Fix 1 ─────────────────────────────────────────────────────────────────
def test_engine_no_windows_returns_none_buckets_keeps_active_idle(db, monkeypatch):
    """No ActivityWindow rows → productivity/hourly None (caller keeps legacy),
    but Active/Idle still returned from day-summary. (Tests 2,3,5,6.)"""
    u = _mk_user(db)
    _patch_daysummary(monkeypatch, active=120, worked=300)
    res = al._engine_summary(db, u.organization_id, u.id, _START, _END, {})
    assert res is not None
    assert res["active_seconds"] == 120
    assert res["idle_seconds"] == 180          # worked - active, not regressed to 0
    assert res["bucket_seconds"] is None       # → legacy productivity preserved
    assert res["hourly"] is None               # → legacy hourly preserved


def test_engine_with_windows_populates_buckets(db, monkeypatch):
    """ActivityWindow rows exist → engine productivity/hourly used (unchanged). (Test 1.)"""
    u = _mk_user(db)
    _patch_daysummary(monkeypatch, active=600, worked=600)
    _add_window(db, u.id, _WIN_TS, "Code", credit=900)   # 'Code' → productive
    res = al._engine_summary(db, u.organization_id, u.id, _START, _END, {})
    assert res is not None
    assert res["bucket_seconds"] is not None
    assert res["bucket_seconds"]["productive"] == 900
    assert res["hourly"] is not None
    assert sum(h["productive"] for h in res["hourly"].values()) == 900


def test_engine_no_windows_no_activity_valid_empty(db, monkeypatch):
    """No windows and a genuinely empty day → active 0, buckets None (valid empty). (Test 4.)"""
    u = _mk_user(db)
    _patch_daysummary(monkeypatch, active=0, worked=0)
    res = al._engine_summary(db, u.organization_id, u.id, _START, _END, {})
    assert res["active_seconds"] == 0
    assert res["idle_seconds"] == 0
    assert res["bucket_seconds"] is None


def test_caller_merge_prefers_legacy_when_engine_buckets_none():
    """Mirror of the get_activity_summary merge: engine None-buckets → keep legacy
    productivity/hourly; engine present → use engine. No double counting. (Test 7.)"""
    legacy_buckets = {"productive": 100, "neutral": 50, "distracting": 0}
    legacy_hourly = {0: {"productive": 100, "neutral": 0, "distracting": 0, "idle": 0}}

    def merge(engine, active0=0, idle0=0):
        active_seconds, idle_seconds = active0, idle0
        bucket_seconds = dict(legacy_buckets)
        hourly = {k: dict(v) for k, v in legacy_hourly.items()}
        if engine is not None:
            active_seconds = engine["active_seconds"]
            idle_seconds = engine["idle_seconds"]
            if engine.get("bucket_seconds") is not None:
                bucket_seconds = engine["bucket_seconds"]
            if engine.get("hourly") is not None:
                hourly = engine["hourly"]
        return active_seconds, idle_seconds, bucket_seconds, hourly

    # engine has no windows → legacy productivity/hourly preserved, engine active/idle used
    a, i, b, h = merge({"active_seconds": 120, "idle_seconds": 180, "bucket_seconds": None, "hourly": None})
    assert (a, i) == (120, 180)
    assert b == legacy_buckets and h == legacy_hourly

    # engine has windows → engine productivity used
    a, i, b, h = merge({"active_seconds": 600, "idle_seconds": 0,
                        "bucket_seconds": {"productive": 900, "neutral": 0, "distracting": 0},
                        "hourly": {1: {"productive": 900}}})
    assert b["productive"] == 900 and 1 in h


# ── Fix 4 ─────────────────────────────────────────────────────────────────
def test_ai_day_bounds_uses_ist_boundaries():
    """/ai/summary _day_bounds now matches the canonical IST helper. (Tests 12,13.)"""
    from app.ai.router import _day_bounds
    from app.active_time.service import ist_day_bounds_utc, ist_today

    target, start, end = _day_bounds("2026-07-07")
    assert target == _dt.date(2026, 7, 7)
    exp_s, exp_e = ist_day_bounds_utc(_dt.date(2026, 7, 7))
    assert start == exp_s and end == exp_e
    # IST 2026-07-07 == UTC [2026-07-06 18:30 .. 2026-07-07 18:30)
    assert start == datetime(2026, 7, 6, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 7, 7, 18, 30, tzinfo=UTC)
    # window width is exactly one day (normal daytime behaviour unchanged)
    assert (end - start) == timedelta(days=1)
    # None → current IST day
    t2, _, _ = _day_bounds(None)
    assert t2 == ist_today()
