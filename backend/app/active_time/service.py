"""
Active Time Engine — persistence & orchestration.

Bridges raw tracker data ↔ the pure scoring engine ↔ the materialised tables.

  materialize_window(...)  — called from /tracking/screenshot ingest (write-once,
                             cheap insert of one ActivityWindow + a provisional
                             standalone score).
  recompute_daily(...)     — authoritative: reads a user's day of windows, scores
                             them with full continuity, aggregates, upserts the
                             ActivityDailyScore, and appends per-minute rows for
                             windows past the continuity horizon (write-once).
  get_daily(...)           — read the cached daily score, recomputing on demand
                             if missing/stale (used by the API read paths).

Scores stored on ActivityWindow rows are PROVISIONAL (standalone, no continuity)
— authoritative continuity-aware scoring happens in-memory in recompute_daily so
ingest stays a single cheap insert and the worker never rewrites window rows.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from database import models
from .config import ScoringConfig, load_config
from .engine import (
    WindowInput, WindowScore, DailyScore,
    score_window, score_windows, expand_minutes, aggregate_day,
)


# Daily-score freshness: a read served from a row computed within this window
# is trusted; otherwise the read path recomputes synchronously.
_DAILY_FRESH_SECONDS = 90


# ──────────────────────────────────────────────────────────────────────────
# Time helpers
# ──────────────────────────────────────────────────────────────────────────
def local_today(db=None, org_id=None) -> "datetime.date":
    """Today on the organization's wall clock.

    Was ``ist_today()``, a fixed +05:30. Active-time day buckets have to line up
    with the dashboard's "today", and that is the tenant's midnight, not India's
    — an employee in New York had their daily active score roll over at 14:30
    local. ``db``/``org_id`` are optional so the handful of call sites that
    genuinely have no tenant context (a bare worker tick) still resolve to the
    documented system default.
    """
    from app import _tz as _tzmod
    if db is None:
        return datetime.now(timezone.utc).astimezone(
            _tzmod._load_zone(_tzmod.FALLBACK_TZ_NAME) or _tzmod._IST_FIXED).date()
    return _tzmod.local_date(db, org_id)


# Deprecated alias — kept for one release so an unmigrated caller still works.
def ist_today(db=None, org_id=None) -> "datetime.date":
    return local_today(db, org_id)


def local_day_bounds_utc(target_date, db=None, org_id=None) -> Tuple[datetime, datetime]:
    """[start, end) UTC instants for the given organization-local calendar date."""
    from app import _tz as _tzmod
    if db is None:
        zone = _tzmod._load_zone(_tzmod.FALLBACK_TZ_NAME) or _tzmod._IST_FIXED
        start = datetime.combine(target_date, datetime.min.time(), tzinfo=zone)
        return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
    return _tzmod.local_day_bounds(db, org_id, target_date)


def ist_day_bounds_utc(target_date, db=None, org_id=None) -> Tuple[datetime, datetime]:
    """Deprecated alias for ``local_day_bounds_utc``."""
    return local_day_bounds_utc(target_date, db, org_id)


def day_anchor_utc(target_date, db=None, org_id=None) -> datetime:
    """Canonical ``ActivityDailyScore.day`` value (local midnight as UTC)."""
    return local_day_bounds_utc(target_date, db, org_id)[0]


# ──────────────────────────────────────────────────────────────────────────
# Worked-seconds (denominator) — reuses the employees router's session/break math
# ──────────────────────────────────────────────────────────────────────────
def worked_seconds_for_day(db: Session, user_id: int, target_date) -> int:
    """Clocked working time minus approved breaks (+ admin adjustments) for the
    IST day — the same figure /me/day-summary computes, factored out so the
    worker and the engine share one denominator."""
    from app.employees.router import _actual_break_seconds_for_sessions  # lazy
    try:
        from app._time_adjustments import adjustment_seconds_for_day
    except Exception:  # pragma: no cover
        adjustment_seconds_for_day = lambda *a, **k: 0  # noqa: E731

    day_start, day_end = ist_day_bounds_utc(target_date)
    now_utc = datetime.now(timezone.utc)
    today = ist_today()

    sessions = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id == user_id,
            models.ClockSession.clock_in_time < day_end,
        )
    ).all()
    sessions = [
        s for s in sessions
        if (s.clock_out_time is None and target_date == today)
           or (s.clock_out_time is not None and s.clock_out_time > day_start)
    ]

    bounds: dict = {}
    for s in sessions:
        # Coerce DB-returned datetimes to aware UTC (SQLite drops tzinfo;
        # Postgres keeps it) so the min/max comparisons never mix naive/aware.
        ci = _aware(s.clock_in_time)
        s_out = now_utc if s.clock_out_time is None else _aware(s.clock_out_time)
        eff_start = max(ci, day_start)
        eff_end = min(s_out, day_end)
        if eff_end > eff_start:
            bounds[s.id] = (eff_start, eff_end)

    actual_breaks = _actual_break_seconds_for_sessions(db, bounds)
    worked = 0
    for sid, (st, en) in bounds.items():
        worked += max(0, int((en - st).total_seconds()) - actual_breaks.get(sid, 0))
    try:
        worked += int(adjustment_seconds_for_day(db, user_id, target_date) or 0)
    except Exception:
        pass
    return worked


def _has_open_break(db: Session, user_id: int, at: datetime) -> bool:
    """True if the user was on a break overlapping ``at`` (defensive — the
    tracker normally pauses during breaks so no window should arrive)."""
    try:
        brk = (
            db.query(models.BreakSession.id)
            .filter(
                models.BreakSession.user_id == user_id,
                models.BreakSession.break_start <= at,
                models.BreakSession.is_active == True,  # noqa: E712
            )
            .order_by(models.BreakSession.break_start.desc())
            .first()
        )
        return brk is not None
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────
# Window ↔ engine conversion
# ──────────────────────────────────────────────────────────────────────────
def _attach_browser_ratios(
    db: Session, user_id: int, rows: List["models.ActivityWindow"],
    inputs: List[WindowInput], day_start: datetime, day_end: datetime,
) -> None:
    """Best-effort: set ``browser_active_ratio`` (active_sec/duration_sec) on each
    window input from BrowserActivity rows whose focus falls inside the window.

    Never raises — browser data is an optional sub-score; if anything goes wrong
    the windows simply score on the six core channels as before.
    """
    try:
        brows = (
            db.query(
                models.BrowserActivity.started_at,
                models.BrowserActivity.active_sec,
                models.BrowserActivity.duration_sec,
            )
            .filter(
                models.BrowserActivity.user_id == user_id,
                models.BrowserActivity.started_at >= day_start,
                models.BrowserActivity.started_at < day_end,
            )
            .all()
        )
        if not brows:
            return
        # Bucket each browser session into the window that contains its start.
        for row, wi in zip(rows, inputs):
            ws = _aware(row.window_start)
            we = ws + timedelta(seconds=int(row.interval_seconds or 0))
            active = dur = 0
            for b in brows:
                bs = _aware(b.started_at)
                if ws <= bs < we:
                    active += int(b.active_sec or 0)
                    dur += int(b.duration_sec or 0)
            if dur > 0:
                wi.browser_active_ratio = max(0.0, min(1.0, active / dur))
    except Exception:
        pass


def _row_to_input(w: models.ActivityWindow) -> WindowInput:
    return WindowInput(
        interval_seconds=int(w.interval_seconds or 0),
        key_presses=int(w.key_presses or 0),
        mouse_clicks=int(w.mouse_clicks or 0),
        mouse_moves=int(w.mouse_moves or 0),
        mouse_scrolls=int(w.mouse_scrolls or 0),
        active_seconds=int(w.active_seconds or 0),
        has_screenshot=bool(w.has_screenshot),
        hook_active=bool(w.hook_active),
        on_break=bool(w.on_break),
        app_name=w.app_name,
        window_start=w.window_start,
    )


# ──────────────────────────────────────────────────────────────────────────
# Ingest — materialize one window (cheap, write-once)
# ──────────────────────────────────────────────────────────────────────────
def materialize_window(
    db: Session,
    *,
    user_id: int,
    session_id: Optional[int],
    activity_id: Optional[int],
    timestamp: datetime,
    interval_seconds: int,
    key_presses: int,
    mouse_clicks: int,
    mouse_moves: int,
    mouse_scrolls: int,
    active_seconds: int,
    app_name: Optional[str],
    has_screenshot: bool,
    hook_active: bool,
    on_break: bool,
    config: Optional[ScoringConfig] = None,
    org_id=None,
) -> Optional[models.ActivityWindow]:
    """Insert one ActivityWindow for a screenshot interval. Never raises into
    the caller — ingest must not fail because scoring did."""
    try:
        config = config or load_config(db, org_id)
        interval = max(1, int(interval_seconds or 0))
        mouse_events = int(mouse_clicks or 0) + int(mouse_scrolls or 0) + int(mouse_moves or 0)

        wi = WindowInput(
            interval_seconds=interval,
            key_presses=int(key_presses or 0),
            mouse_clicks=int(mouse_clicks or 0),
            mouse_moves=int(mouse_moves or 0),
            mouse_scrolls=int(mouse_scrolls or 0),
            active_seconds=int(active_seconds or 0),
            has_screenshot=bool(has_screenshot),
            hook_active=bool(hook_active),
            on_break=bool(on_break),
            app_name=app_name,
        )
        ws = score_window(wi, config)  # provisional (standalone) score

        row = models.ActivityWindow(
            user_id=user_id,
            session_id=session_id,
            activity_id=activity_id,
            window_start=timestamp,
            window_end=timestamp + timedelta(seconds=interval),
            interval_seconds=interval,
            key_presses=wi.key_presses,
            mouse_clicks=wi.mouse_clicks,
            mouse_moves=wi.mouse_moves,
            mouse_scrolls=wi.mouse_scrolls,
            mouse_events=mouse_events,
            active_seconds=wi.active_seconds,
            idle_seconds=max(0, interval - wi.active_seconds),
            app_name=app_name,
            has_screenshot=bool(has_screenshot),
            hook_active=bool(hook_active),
            on_break=bool(on_break),
            keyboard_score=ws.keyboard_score,
            mouse_score=ws.mouse_score,
            focus_score=ws.focus_score,
            continuity_score=ws.continuity_score,
            screenshot_score=ws.screenshot_score,
            app_engagement_score=ws.app_engagement_score,
            score=ws.score,
            label=ws.label,
            fully_active=ws.fully_active,
            flags=ws.flags or None,
            active_credit_seconds=ws.active_credit_seconds,
        )
        db.add(row)
        return row
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────
# Authoritative daily recompute
# ──────────────────────────────────────────────────────────────────────────
def recompute_daily(
    db: Session,
    user_id: int,
    target_date,
    *,
    worked_seconds: Optional[int] = None,
    persist_minutes: bool = True,
    config: Optional[ScoringConfig] = None,
    commit: bool = True,
    org_id=None,
) -> Optional[models.ActivityDailyScore]:
    """Score a user's whole day with full continuity and upsert the cached
    ActivityDailyScore. Returns the row (or None if it could not be computed)."""
    config = config or load_config(db, org_id)
    day_start, day_end = ist_day_bounds_utc(target_date)

    rows: List[models.ActivityWindow] = (
        db.query(models.ActivityWindow)
        .filter(
            and_(
                models.ActivityWindow.user_id == user_id,
                models.ActivityWindow.window_start >= day_start,
                models.ActivityWindow.window_start < day_end,
            )
        )
        .order_by(models.ActivityWindow.window_start)
        .all()
    )
    if not rows:
        return None

    inputs = [_row_to_input(w) for w in rows]
    _attach_browser_ratios(db, user_id, rows, inputs, day_start, day_end)
    scored = score_windows(inputs, config)

    if worked_seconds is None:
        worked_seconds = worked_seconds_for_day(db, user_id, target_date)
    agg: DailyScore = aggregate_day(scored, worked_seconds, config)

    anchor = day_anchor_utc(target_date)
    daily = (
        db.query(models.ActivityDailyScore)
        .filter(
            models.ActivityDailyScore.user_id == user_id,
            models.ActivityDailyScore.day == anchor,
        )
        .first()
    )
    if daily is None:
        daily = models.ActivityDailyScore(user_id=user_id, day=anchor)
        db.add(daily)

    daily.worked_seconds = agg.worked_seconds
    daily.active_seconds = agg.active_seconds
    daily.idle_seconds = agg.idle_seconds
    daily.break_seconds = agg.break_seconds
    daily.active_pct = agg.active_pct
    daily.raw_active_pct = agg.raw_active_pct
    daily.coverage_ratio = agg.coverage_ratio
    daily.minutes_active = agg.minutes_active
    daily.minutes_partial = agg.minutes_partial
    daily.minutes_low = agg.minutes_low
    daily.minutes_idle = agg.minutes_idle
    daily.window_count = agg.window_count
    daily.flags = agg.flags or None
    daily.computed_at = datetime.now(timezone.utc)

    if persist_minutes:
        try:
            _persist_finalized_minutes(db, daily, rows, scored, config)
        except Exception:
            pass  # minute persistence is best-effort; daily score is the truth

    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            return None
    return daily


def _persist_finalized_minutes(
    db: Session,
    daily: models.ActivityDailyScore,
    rows: List[models.ActivityWindow],
    scored: List[WindowScore],
    config: ScoringConfig,
) -> None:
    """Append per-minute rows for windows past the continuity horizon (so their
    score will not change again), advancing ``finalized_through``. Write-once:
    each minute is inserted exactly once across the day."""
    n = len(scored)
    r = max(0, int(config.continuity_neighbors))
    last_finalized = daily.finalized_through
    new_through = last_finalized

    for i, (row, ws) in enumerate(zip(rows, scored)):
        finalizable = (n - 1 - i) >= r          # enough later windows exist
        if not finalizable:
            break                                 # rows are ordered; rest aren't final
        if last_finalized is not None and row.window_start <= last_finalized:
            continue                              # already persisted
        # Stamp window_start on the score so minutes get real timestamps.
        ws.window_start = row.window_start
        for m in expand_minutes([ws]):
            db.add(models.ActivityMinuteScore(
                user_id=daily.user_id,
                window_id=row.id,
                minute_start=m.minute_start,
                classification=m.classification,
                score=m.score,
                active_seconds=m.active_seconds,
                on_break=m.on_break,
            ))
        if new_through is None or row.window_start > new_through:
            new_through = row.window_start

    if new_through is not None and new_through != last_finalized:
        daily.finalized_through = new_through


# ──────────────────────────────────────────────────────────────────────────
# Read path — cached daily score, recompute on demand
# ──────────────────────────────────────────────────────────────────────────
def get_daily(
    db: Session,
    user_id: int,
    target_date,
    *,
    allow_recompute: bool = True,
    config: Optional[ScoringConfig] = None,
) -> Optional[models.ActivityDailyScore]:
    """Return the cached daily score, recomputing synchronously if it is missing
    or stale (today only). Returns None when the user has no windows for the day
    (caller should fall back to the legacy computation)."""
    anchor = day_anchor_utc(target_date)
    daily = (
        db.query(models.ActivityDailyScore)
        .filter(
            models.ActivityDailyScore.user_id == user_id,
            models.ActivityDailyScore.day == anchor,
        )
        .first()
    )
    is_today = target_date == ist_today()
    fresh = False
    if daily is not None and daily.computed_at is not None:
        age = (datetime.now(timezone.utc) - _aware(daily.computed_at)).total_seconds()
        fresh = age <= _DAILY_FRESH_SECONDS
    # Past days never change once computed; today recomputes when stale.
    if daily is not None and (not is_today or fresh):
        return daily
    if allow_recompute:
        recomputed = recompute_daily(db, user_id, target_date, config=config)
        if recomputed is not None:
            return recomputed
    return daily


def get_daily_map_for_today(db: Session, org_id=None) -> dict:
    """One indexed query → {user_id: ActivityDailyScore} for TODAY.

    ``org_id`` does two things and both matter:

      * it anchors the day on that ORGANIZATION's midnight. Without it the
        anchor was a fixed +05:30, so a New York tenant's dashboard read the
        wrong calendar day's active time.
      * it restricts the rows to that tenant. ActivityDailyScore has no
        organization_id column, so the scope comes from a join on users —
        defence in depth for a query that otherwise loads every tenant's rows
        into one process-local dict.
    """
    anchor = day_anchor_utc(local_today(db, org_id))
    q = db.query(models.ActivityDailyScore).filter(
        models.ActivityDailyScore.day == anchor)
    if org_id is not None:
        q = q.filter(models.ActivityDailyScore.user_id.in_(
            db.query(models.User.id).filter(
                models.User.organization_id == org_id)))
    return {r.user_id: r for r in q.all()}


def active_users_with_windows_since(db: Session, since: datetime) -> List[int]:
    """Distinct user_ids that produced a window at/after ``since`` — the work
    list for the reconcile worker."""
    rows = (
        db.query(models.ActivityWindow.user_id)
        .filter(models.ActivityWindow.window_start >= since)
        .distinct()
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
