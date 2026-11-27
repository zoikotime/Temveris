"""
Temveris - Activity Log Router  (Unified per-user event timeline)
==================================================================
Returns a chronological merge of every "event" the platform records about
a user's day:

  * screenshot / app activity rows  (table: activities)
  * browser tab visits              (table: browser_activities)
  * clock-in / clock-out events     (table: clock_sessions)
  * break start / end events        (table: break_sessions)

Each event carries a productivity classification resolved once per request
from the AppCategory + DomainCategory overrides plus the curated defaults.

Endpoints
---------
GET /activity-log/timeline   Paginated timeline (cursor-based, newest-first).
GET /activity-log/summary    Aggregate KPIs + hourly buckets + top apps/domains.

Role gating
-----------
Employees can only query their own user_id (the param is silently overridden
to caller.id). Admins / HR can pass any user_id, or omit it for the whole org.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, timezone
from typing import List, Optional, Dict, Tuple, Any
import time as _time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, or_, func, text
from sqlalchemy.exc import OperationalError, DBAPIError
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user, get_current_organization
from app.app_review.productivity import (
    categorise as categorise_app,
    load_overrides as load_app_overrides,
    CATEGORY_PRODUCTIVE,
    CATEGORY_NEUTRAL,
    CATEGORY_DISTRACTING,
    CATEGORY_BLOCKED,
)
from app.browser.productivity import (
    categorise as categorise_domain,
    load_overrides as load_domain_overrides,
    CATEGORY_WORK,
    CATEGORY_NEUTRAL as DOMAIN_NEUTRAL,
    CATEGORY_DISTRACTION,
)
from app.commercial.enforcement import shadow_capability as _shadow

router = APIRouter(prefix="/activity-log", tags=["Activity Log"])

# ──────────────────────────────────────────────────────────────────────
# Constants & helpers
# ──────────────────────────────────────────────────────────────────────

# Cap a single timeline page at this many merged events. The merge step
# scans up to ~4x this many rows across the four source tables, so keep
# the cap modest to keep p95 latency low.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

# Cap the date range at 31 days per request. Wider ranges should be
# stitched together client-side via repeated "Load more" cursor calls.
MAX_RANGE_DAYS = 31

# Productivity buckets we expose to the client. We collapse the two source
# vocabularies (app: productive/neutral/distracting/blocked, domain:
# work/neutral/distraction) into a single 3-bucket taxonomy so the UI
# can filter and chart consistently.
P_PRODUCTIVE   = "productive"
P_NEUTRAL      = "neutral"
P_DISTRACTING  = "distracting"

# Validates the `types` filter so we don't trust raw user input in joins.
VALID_TYPES = {
    "screenshot",       # an Activity row that includes a captured image
    "app",              # an Activity row without an image (capture failed / disabled)
    "browser",          # a BrowserActivity row
    "session_start",    # ClockSession.clock_in_time
    "session_end",      # ClockSession.clock_out_time
    "break_start",      # BreakSession.break_start
    "break_end",        # BreakSession.break_end
}

VALID_PRODUCTIVITY = {P_PRODUCTIVE, P_NEUTRAL, P_DISTRACTING}


# Per-request statement timeout for the timeline endpoint (Postgres only).
# Wide-range "app activity" filters can scan the activities table heavily
# when no user_id is set, and PgBouncer transaction pooling forbids the
# startup-option timeout we'd otherwise set in database.py. Issuing
# SET LOCAL inside the implicit transaction gives us a real upper bound
# so we surface a friendly empty result instead of FastAPI's blanket 503.
_TIMELINE_STATEMENT_TIMEOUT_MS = 12000


def _apply_timeline_timeout(db: Session) -> None:
    try:
        db.execute(text(f"SET LOCAL statement_timeout = {_TIMELINE_STATEMENT_TIMEOUT_MS}"))
    except Exception:
        # SQLite / non-PG backends ignore this; we don't want a setting that
        # only the prod backend supports to take down local dev or tests.
        try:
            db.rollback()
        except Exception:
            pass


def _is_admin(user: models.User) -> bool:
    return getattr(user, "role", "") in ("admin", "hr")


def _normalise_app_category(cat: Optional[str]) -> str:
    """App vocab -> 3-bucket UI vocab. Blocked maps to distracting because
    that is how the dashboard already counts it (it's the same coloured tile)."""
    if cat == CATEGORY_PRODUCTIVE:
        return P_PRODUCTIVE
    if cat == CATEGORY_DISTRACTING or cat == CATEGORY_BLOCKED:
        return P_DISTRACTING
    return P_NEUTRAL  # CATEGORY_NEUTRAL or unknown


def _normalise_domain_category(cat: Optional[str]) -> str:
    """Domain vocab -> 3-bucket UI vocab. Same idea as _normalise_app_category."""
    if cat == CATEGORY_WORK:
        return P_PRODUCTIVE
    if cat == CATEGORY_DISTRACTION:
        return P_DISTRACTING
    return P_NEUTRAL


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Force a datetime to UTC-aware. The legacy `activities`,
    `clock_sessions`, and `break_sessions` tables were created with
    `TIMESTAMP WITHOUT TIME ZONE` columns, so SQLAlchemy returns naive
    datetimes for them — but we always store UTC. The timeline endpoint
    sorts/merges across these *and* `browser_activities` (which is tzaware),
    so without this normalisation Python would raise "can't compare
    offset-naive and offset-aware datetimes" → 500."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_window(
    start: Optional[str], end: Optional[str]
) -> Tuple[datetime, datetime]:
    """Coerce string params into a [start, end] UTC window with sane defaults
    (last 24h) and the MAX_RANGE_DAYS cap applied. Always returns aware
    datetimes — callers can rely on tzinfo being non-None."""
    end_dt = _parse_iso(end) or datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    start_dt = _parse_iso(start)
    if start_dt is None:
        start_dt = end_dt - timedelta(days=1)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    # Range cap — protect the merge step from runaway scans.
    if (end_dt - start_dt) > timedelta(days=MAX_RANGE_DAYS):
        start_dt = end_dt - timedelta(days=MAX_RANGE_DAYS)

    return start_dt, end_dt


def _parse_csv(raw: Optional[str], allowed: set) -> Optional[set]:
    """Parse a CSV like 'screenshot,session_start' into a validated set."""
    if not raw:
        return None
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    parts &= allowed
    return parts or None


def _parse_cursor(raw: Optional[str]) -> Optional[Tuple[datetime, str]]:
    """Cursor format: '<iso_ts>|<event_id>'. Returns (ts, id) or None on bad
    input. Treating malformed cursors as 'first page' is intentional — we
    never want a bad cursor to 500."""
    if not raw:
        return None
    try:
        ts_part, id_part = raw.split("|", 1)
    except ValueError:
        return None
    ts = _parse_iso(ts_part)
    if ts is None:
        return None
    return ts, id_part


def _make_cursor(ts: datetime, eid: str) -> str:
    return f"{ts.isoformat()}|{eid}"


# A user_id picker that enforces role gating. Returns the effective user_id
# filter (None means "all users", which is admin-only).
#
# `scope=self` forces self-scoping even for admin callers — the employee
# dashboard sets it so an admin viewing their personal Activity Log sees
# their own day, not the org-wide aggregate.
def _resolve_user_filter(
    requested: Optional[int], caller: models.User, scope: Optional[str] = None,
) -> Optional[int]:
    if scope == "self":
        return caller.id
    if not _is_admin(caller):
        return caller.id
    return requested  # may be None → "all users", admin-only


# Pull the configured tracker tick interval (`screenshot_interval` in
# AppSettings) so KPI math stays correct when admins change it. Cached for
# 60s per org — the activity-log endpoints are polled every 20s by the
# dashboard and the setting changes rarely.
_TICK_CACHE: Dict[Any, Any] = {}   # keyed by org_id
_TICK_TTL = 60
_TICK_FALLBACK = 30
_TICK_MIN, _TICK_MAX = 1, 3600


def _get_tick_seconds(db: Session, org_id=None) -> int:
    now = _time.monotonic()
    cached = _TICK_CACHE.get(org_id)
    if cached and now <= cached["expires"]:
        return cached["value"]
    val = _TICK_FALLBACK
    try:
        row = (
            db.query(models.AppSettings)
            .filter(
                models.AppSettings.organization_id == org_id,
                models.AppSettings.key == "screenshot_interval",
                models.AppSettings.team_key.is_(None),   # org level only
                models.AppSettings.user_id.is_(None),
            )
            .first()
        )
        if row and row.value:
            n = int(str(row.value).strip())
            if _TICK_MIN <= n <= _TICK_MAX:
                val = n
    except Exception:
        pass
    _TICK_CACHE[org_id] = {"value": val, "expires": now + _TICK_TTL}
    return val


# Reload productivity overrides at most every 5 minutes per process. The
# /activity-log/* endpoints are heavy on per-row classification; loading
# both override tables on every request was the hot path under load.
_OVERRIDE_CACHE: Dict[Optional[int], Dict[str, Any]] = {}
_OVERRIDE_TTL = 300  # seconds


def _get_overrides(db: Session, organization_id: Optional[int] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (app_overrides, domain_overrides) cached per org for up to 5 min.

    Both app and domain overrides are scoped to the caller's organization_id."""
    now = _time.monotonic()
    cached = _OVERRIDE_CACHE.get(organization_id)
    if cached and now <= cached["expires"]:
        return cached["app"], cached["domain"]
    try:
        app_ov = load_app_overrides(db, organization_id)
    except Exception:
        app_ov = {}
    try:
        domain_ov = load_domain_overrides(db, organization_id)
    except Exception:
        domain_ov = {}
    _OVERRIDE_CACHE[organization_id] = {
        "app": app_ov,
        "domain": domain_ov,
        "expires": now + _OVERRIDE_TTL,
    }
    return app_ov, domain_ov


# ──────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────


class TimelineEvent(BaseModel):
    id: str
    timestamp: str
    type: str
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    app_name: Optional[str] = None
    window_title: Optional[str] = None
    domain: Optional[str] = None
    url: Optional[str] = None
    duration_seconds: Optional[int] = None
    mouse_clicks: Optional[int] = None
    key_presses: Optional[int] = None
    idle_seconds: Optional[int] = None
    is_active: Optional[bool] = None
    productivity: Optional[str] = None  # productive | neutral | distracting | None
    thumbnail_url: Optional[str] = None
    label: Optional[str] = None  # human-readable description


class TimelineResponse(BaseModel):
    events: List[TimelineEvent]
    next_cursor: Optional[str] = None
    has_more: bool = False
    total_scanned: int = 0
    # True when one or more source queries hit the statement-timeout cap
    # (e.g. a 30-day org-wide "app only" filter on a multi-million-row
    # activities table). The client should hint the user to narrow the
    # filter rather than show an empty timeline as if no events existed.
    partial: bool = False


class SummaryResponse(BaseModel):
    range: Dict[str, str]                       # { start, end }
    totals: Dict[str, int]                      # active_seconds, idle_seconds, ...
    productivity: Dict[str, int]                # bucket -> seconds
    hourly: List[Dict[str, Any]]                # [{ hour, productive, neutral, distracting, idle }]
    top_apps: List[Dict[str, Any]]              # [{ app_name, category, seconds, screenshots }]
    top_domains: List[Dict[str, Any]]           # [{ domain, category, active_seconds, visits }]


# ──────────────────────────────────────────────────────────────────────
# Source -> normalised event mappers
# ──────────────────────────────────────────────────────────────────────


def _user_lookup(
    db: Session, user_ids: set
) -> Dict[int, str]:
    if not user_ids:
        return {}
    rows = (
        db.query(models.User.id, models.User.name)
        .filter(models.User.id.in_(user_ids))
        .all()
    )
    return {r.id: r.name for r in rows}


def _activity_event(
    a: models.Activity,
    user_name: str,
    app_overrides: Dict[str, str],
) -> Dict[str, Any]:
    has_image = bool(a.file_path or a.thumbnail)
    bucket = _normalise_app_category(categorise_app(a.app_name, app_overrides))
    label = a.app_name or "Activity"
    if a.window_title and a.window_title != a.app_name:
        label = f"{label} — {a.window_title}"
    return {
        "id": f"act-{a.id}",
        "timestamp": _aware(a.timestamp),
        "type": "screenshot" if has_image else "app",
        "user_id": a.user_id,
        "user_name": user_name,
        "app_name": a.app_name,
        "window_title": a.window_title,
        "domain": None,
        "url": None,
        "duration_seconds": None,
        "mouse_clicks": int(a.mouse_clicks or 0),
        "key_presses": int(a.key_presses or 0),
        "idle_seconds": int(a.idle_seconds or 0),
        "is_active": bool(a.is_active),
        "productivity": bucket,
        "thumbnail_url": (a.file_path or a.thumbnail) if has_image else None,
        "label": label,
    }


def _browser_event(
    b: models.BrowserActivity,
    user_name: str,
    domain_overrides: Dict[str, str],
) -> Dict[str, Any]:
    bucket = _normalise_domain_category(categorise_domain(b.domain, domain_overrides))
    label = b.tab_title or b.domain or b.url or "Browser"
    return {
        "id": f"browser-{b.id}",
        "timestamp": _aware(b.started_at),
        "type": "browser",
        "user_id": b.user_id,
        "user_name": user_name,
        "app_name": b.browser,
        "window_title": b.tab_title,
        "domain": b.domain,
        "url": b.url,
        "duration_seconds": int(b.duration_sec or 0),
        "mouse_clicks": int(b.mouse_clicks or 0),
        "key_presses": int(b.key_presses or 0),
        "idle_seconds": int(b.idle_sec or 0),
        "is_active": bool(b.active_sec or 0) > 0,
        "productivity": bucket,
        "thumbnail_url": None,
        "label": label,
    }


def _app_session_event(
    s: models.AppSession,
    user_name: str,
    app_overrides: Dict[str, str],
) -> Dict[str, Any]:
    """Desktop-tool foreground switch → timeline 'app' event. Mirrors
    _activity_event but sourced from the lightweight app_sessions table (which
    captures tool usage far more frequently than screenshot-driven activities)."""
    bucket = _normalise_app_category(categorise_app(s.app_name, app_overrides))
    label = s.app_name or "App"
    if s.window_title and s.window_title != s.app_name:
        label = f"{label} — {s.window_title}"
    return {
        "id": f"appsess-{s.id}",
        "timestamp": _aware(s.started_at),
        "type": "app",
        "user_id": s.user_id,
        "user_name": user_name,
        "app_name": s.app_name,
        "window_title": s.window_title,
        "domain": None,
        "url": None,
        "duration_seconds": None,
        "mouse_clicks": None,
        "key_presses": None,
        "idle_seconds": None,
        "is_active": True,
        "productivity": bucket,
        "thumbnail_url": None,
        "label": label,
    }


def _session_start_event(s: models.ClockSession, user_name: str) -> Dict[str, Any]:
    return {
        "id": f"session-in-{s.id}",
        "timestamp": _aware(s.clock_in_time),
        "type": "session_start",
        "user_id": s.user_id,
        "user_name": user_name,
        "app_name": None, "window_title": None, "domain": None, "url": None,
        "duration_seconds": None,
        "mouse_clicks": None, "key_presses": None, "idle_seconds": None,
        "is_active": True,
        "productivity": None,
        "thumbnail_url": None,
        "label": "Clocked In",
    }


def _session_end_event(s: models.ClockSession, user_name: str) -> Dict[str, Any]:
    return {
        "id": f"session-out-{s.id}",
        "timestamp": _aware(s.clock_out_time),
        "type": "session_end",
        "user_id": s.user_id,
        "user_name": user_name,
        "app_name": None, "window_title": None, "domain": None, "url": None,
        "duration_seconds": int(s.total_seconds or 0),
        "mouse_clicks": None, "key_presses": None, "idle_seconds": None,
        "is_active": False,
        "productivity": None,
        "thumbnail_url": None,
        "label": "Clocked Out",
    }


def _break_start_event(b: models.BreakSession, user_name: str) -> Dict[str, Any]:
    return {
        "id": f"break-start-{b.id}",
        "timestamp": _aware(b.break_start),
        "type": "break_start",
        "user_id": b.user_id,
        "user_name": user_name,
        "app_name": None, "window_title": None, "domain": None, "url": None,
        "duration_seconds": None,
        "mouse_clicks": None, "key_presses": None, "idle_seconds": None,
        "is_active": False,
        "productivity": None,
        "thumbnail_url": None,
        "label": "Break Started",
    }


def _break_end_event(b: models.BreakSession, user_name: str) -> Dict[str, Any]:
    return {
        "id": f"break-end-{b.id}",
        "timestamp": _aware(b.break_end),
        "type": "break_end",
        "user_id": b.user_id,
        "user_name": user_name,
        "app_name": None, "window_title": None, "domain": None, "url": None,
        "duration_seconds": int(b.break_seconds or 0),
        "mouse_clicks": None, "key_presses": None, "idle_seconds": None,
        "is_active": False,
        "productivity": None,
        "thumbnail_url": None,
        "label": "Break Ended" + (f": {b.reason}" if b.reason else ""),
    }


# ──────────────────────────────────────────────────────────────────────
# GET /activity-log/timeline
# ──────────────────────────────────────────────────────────────────────


@router.get("/timeline", response_model=TimelineResponse)
def get_activity_timeline(
    start: Optional[str] = Query(None, description="ISO 8601 lower bound (UTC)."),
    end: Optional[str]   = Query(None, description="ISO 8601 upper bound (UTC)."),
    user_id: Optional[int] = Query(None, description="Admin-only. Employees forced to self."),
    types: Optional[str]  = Query(None, description="CSV of event types to include."),
    productivity: Optional[str] = Query(None, description="CSV of productivity buckets."),
    search: Optional[str] = Query(None, description="Substring match on app / window / url / domain."),
    cursor: Optional[str] = Query(None, description="Pagination cursor: '<iso_ts>|<event_id>'."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    scope: Optional[str] = Query(None, description="'self' forces caller-scoped data even for admins."),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    org: models.Organization = Depends(get_current_organization),
):
    start_dt, end_dt = _resolve_window(start, end)
    effective_user = _resolve_user_filter(user_id, user, scope)
    _shadow(db, user, "activity.log.view",        # SHADOW ONLY
            route="GET /activity-log/timeline")
    type_filter = _parse_csv(types, VALID_TYPES)
    prod_filter = _parse_csv(productivity, VALID_PRODUCTIVITY)
    search_q = (search or "").strip().lower() or None
    cur = _parse_cursor(cursor)
    cur_ts = cur[0] if cur else None
    cur_id = cur[1] if cur else None

    per_source_cap = max(limit * 4, 100)
    wide_range = (end_dt - start_dt) > timedelta(days=7)
    if wide_range and effective_user is None:
        per_source_cap = max(limit * 2, 80)

    _apply_timeline_timeout(db)

    app_overrides, domain_overrides = _get_overrides(db, org.id)

    events: List[Dict[str, Any]] = []

    # Track whether any source query was truncated by the statement timeout
    # so we can surface a hint to the UI instead of returning a misleading
    # empty page when the DB simply ran out of budget.
    partial_result = False

    # ── activities (screenshots / app rows) ──
    if type_filter is None or ("screenshot" in type_filter or "app" in type_filter):
        q = db.query(models.Activity).filter(
            models.Activity.organization_id == org.id,
            models.Activity.timestamp >= start_dt,
            models.Activity.timestamp <= end_dt,
        )
        if effective_user is not None:
            q = q.filter(models.Activity.user_id == effective_user)
        # Push the screenshot-vs-app split into SQL so we don't over-fetch
        # the wrong half when only one of the two is requested. The
        # post-fetch filter below still runs as a safety net but the DB
        # query now skips rows that would be discarded anyway, which
        # avoids reading huge swaths of the screenshots table when an
        # admin filters for "app" only over a wide date range.
        if type_filter is not None:
            want_screenshot = "screenshot" in type_filter
            want_app = "app" in type_filter
            if want_screenshot and not want_app:
                q = q.filter(or_(models.Activity.file_path.isnot(None), models.Activity.thumbnail.isnot(None)))
            elif want_app and not want_screenshot:
                q = q.filter(models.Activity.file_path.is_(None), models.Activity.thumbnail.is_(None))
        if cur_ts is not None:
            # Strict "older-than-cursor" predicate keeps pagination stable
            # when several events share a timestamp.
            q = q.filter(
                or_(
                    models.Activity.timestamp < cur_ts,
                    and_(
                        models.Activity.timestamp == cur_ts,
                        # id-based tiebreaker: only keep ids smaller than cursor's
                        # (cursor id is "act-<n>"; non-act ids can't collide here)
                        models.Activity.id
                        < (
                            int(cur_id.split("-", 1)[1])
                            if cur_id and cur_id.startswith("act-")
                            and cur_id.split("-", 1)[1].isdigit()
                            else 2**31
                        ),
                    ),
                )
            )
        if search_q:
            like = f"%{search_q}%"
            q = q.filter(
                or_(
                    func.lower(models.Activity.app_name).like(like),
                    func.lower(models.Activity.window_title).like(like),
                )
            )
        try:
            rows = (
                q.order_by(models.Activity.timestamp.desc(), models.Activity.id.desc())
                .limit(per_source_cap)
                .all()
            )
        except (OperationalError, DBAPIError):
            # Statement-timeout (typical for 30-day org-wide "app only" with
            # millions of rows) or transient DB hiccup. Reset the session so
            # we can still try the other sources, and flag the response.
            try: db.rollback()
            except Exception: pass
            rows = []
            partial_result = True
        user_names = _user_lookup(db, {r.user_id for r in rows if r.user_id})
        for r in rows:
            ev = _activity_event(r, user_names.get(r.user_id, ""), app_overrides)
            # Apply post-fetch type filter (split screenshot vs app by file_path)
            if type_filter is not None and ev["type"] not in type_filter:
                continue
            if prod_filter is not None and ev["productivity"] not in prod_filter:
                continue
            events.append(ev)

        # Re-apply the statement timeout — `SET LOCAL` only lives for the
        # current transaction, and the rollback above ended it.
        _apply_timeline_timeout(db)

    # ── browser_activities ──
    if type_filter is None or "browser" in type_filter:
        q = db.query(models.BrowserActivity).filter(
            models.BrowserActivity.organization_id == org.id,
            models.BrowserActivity.started_at >= start_dt,
            models.BrowserActivity.started_at <= end_dt,
        )
        if effective_user is not None:
            q = q.filter(models.BrowserActivity.user_id == effective_user)
        if cur_ts is not None:
            q = q.filter(
                or_(
                    models.BrowserActivity.started_at < cur_ts,
                    and_(
                        models.BrowserActivity.started_at == cur_ts,
                        models.BrowserActivity.id
                        < (
                            int(cur_id.split("-", 1)[1])
                            if cur_id and cur_id.startswith("browser-")
                            and cur_id.split("-", 1)[1].isdigit()
                            else 2**31
                        ),
                    ),
                )
            )
        if search_q:
            like = f"%{search_q}%"
            q = q.filter(
                or_(
                    func.lower(models.BrowserActivity.domain).like(like),
                    func.lower(models.BrowserActivity.tab_title).like(like),
                    func.lower(models.BrowserActivity.url).like(like),
                )
            )
        try:
            rows = (
                q.order_by(
                    models.BrowserActivity.started_at.desc(),
                    models.BrowserActivity.id.desc(),
                )
                .limit(per_source_cap)
                .all()
            )
        except (OperationalError, DBAPIError):
            try: db.rollback()
            except Exception: pass
            rows = []
            partial_result = True
        user_names = _user_lookup(db, {r.user_id for r in rows if r.user_id})
        for r in rows:
            ev = _browser_event(r, user_names.get(r.user_id, ""), domain_overrides)
            if prod_filter is not None and ev["productivity"] not in prod_filter:
                continue
            events.append(ev)
        _apply_timeline_timeout(db)

    # ── app_sessions (desktop tool usage — Eclipse, IDEs, Office, etc.) ──
    # Counted under the "app" type alongside screenshot/app activities, so the
    # timeline reflects desktop-tool usage, not just website visits.
    if type_filter is None or "app" in type_filter:
        q = db.query(models.AppSession).filter(
            models.AppSession.organization_id == org.id,
            models.AppSession.started_at >= start_dt,
            models.AppSession.started_at <= end_dt,
        )
        if effective_user is not None:
            q = q.filter(models.AppSession.user_id == effective_user)
        if cur_ts is not None:
            q = q.filter(
                or_(
                    models.AppSession.started_at < cur_ts,
                    and_(
                        models.AppSession.started_at == cur_ts,
                        models.AppSession.id
                        < (
                            int(cur_id.split("-", 1)[1])
                            if cur_id and cur_id.startswith("appsess-")
                            and cur_id.split("-", 1)[1].isdigit()
                            else 2**31
                        ),
                    ),
                )
            )
        if search_q:
            like = f"%{search_q}%"
            q = q.filter(
                or_(
                    func.lower(models.AppSession.app_name).like(like),
                    func.lower(models.AppSession.window_title).like(like),
                )
            )
        try:
            rows = (
                q.order_by(
                    models.AppSession.started_at.desc(),
                    models.AppSession.id.desc(),
                )
                .limit(per_source_cap)
                .all()
            )
        except (OperationalError, DBAPIError):
            try: db.rollback()
            except Exception: pass
            rows = []
            partial_result = True
        user_names = _user_lookup(db, {r.user_id for r in rows if r.user_id})
        for r in rows:
            ev = _app_session_event(r, user_names.get(r.user_id, ""), app_overrides)
            if prod_filter is not None and ev["productivity"] not in prod_filter:
                continue
            events.append(ev)
        _apply_timeline_timeout(db)

    # ── clock sessions: in/out are separate events ──
    want_in  = type_filter is None or "session_start" in type_filter
    want_out = type_filter is None or "session_end"   in type_filter
    if (want_in or want_out) and prod_filter is None and not search_q:
        q = db.query(models.ClockSession).filter(
            or_(
                and_(
                    models.ClockSession.clock_in_time >= start_dt,
                    models.ClockSession.clock_in_time <= end_dt,
                ),
                and_(
                    models.ClockSession.clock_out_time.isnot(None),
                    models.ClockSession.clock_out_time >= start_dt,
                    models.ClockSession.clock_out_time <= end_dt,
                ),
            )
        )
        if effective_user is not None:
            q = q.filter(models.ClockSession.user_id == effective_user)
        try:
            rows = q.order_by(models.ClockSession.clock_in_time.desc()).limit(per_source_cap).all()
        except (OperationalError, DBAPIError):
            try: db.rollback()
            except Exception: pass
            rows = []
            partial_result = True
        user_names = _user_lookup(db, {r.user_id for r in rows if r.user_id})
        for r in rows:
            uname = user_names.get(r.user_id, "")
            ci = _aware(r.clock_in_time)
            co = _aware(r.clock_out_time)
            if want_in and ci is not None and start_dt <= ci <= end_dt:
                ev = _session_start_event(r, uname)
                if cur_ts is None or _is_before_cursor(ev["timestamp"], ev["id"], cur_ts, cur_id):
                    events.append(ev)
            if want_out and co is not None and start_dt <= co <= end_dt:
                ev = _session_end_event(r, uname)
                if cur_ts is None or _is_before_cursor(ev["timestamp"], ev["id"], cur_ts, cur_id):
                    events.append(ev)

    # ── break sessions: start/end are separate events ──
    want_bs = type_filter is None or "break_start" in type_filter
    want_be = type_filter is None or "break_end"   in type_filter
    if (want_bs or want_be) and prod_filter is None and not search_q:
        q = db.query(models.BreakSession).filter(
            or_(
                and_(
                    models.BreakSession.break_start >= start_dt,
                    models.BreakSession.break_start <= end_dt,
                ),
                and_(
                    models.BreakSession.break_end.isnot(None),
                    models.BreakSession.break_end >= start_dt,
                    models.BreakSession.break_end <= end_dt,
                ),
            )
        )
        if effective_user is not None:
            q = q.filter(models.BreakSession.user_id == effective_user)
        try:
            rows = q.order_by(models.BreakSession.break_start.desc()).limit(per_source_cap).all()
        except (OperationalError, DBAPIError):
            try: db.rollback()
            except Exception: pass
            rows = []
            partial_result = True
        _apply_timeline_timeout(db)
        user_names = _user_lookup(db, {r.user_id for r in rows if r.user_id})
        for r in rows:
            uname = user_names.get(r.user_id, "")
            bs = _aware(r.break_start)
            be = _aware(r.break_end)
            if want_bs and bs is not None and start_dt <= bs <= end_dt:
                ev = _break_start_event(r, uname)
                if cur_ts is None or _is_before_cursor(ev["timestamp"], ev["id"], cur_ts, cur_id):
                    events.append(ev)
            if want_be and be is not None and start_dt <= be <= end_dt:
                ev = _break_end_event(r, uname)
                if cur_ts is None or _is_before_cursor(ev["timestamp"], ev["id"], cur_ts, cur_id):
                    events.append(ev)

    total_scanned = len(events)

    # Merge + sort newest-first; stable id tiebreaker so the cursor is
    # deterministic across requests with identical timestamps.
    events.sort(
        key=lambda e: (e["timestamp"], e["id"]),
        reverse=True,
    )

    page = events[:limit]
    has_more = len(events) > limit

    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _make_cursor(last["timestamp"], last["id"])

    # Stringify timestamps once for the response — the model expects str.
    out_events = []
    for ev in page:
        ev_copy = dict(ev)
        ts = ev_copy["timestamp"]
        if isinstance(ts, datetime):
            ev_copy["timestamp"] = ts.isoformat()
        out_events.append(TimelineEvent(**ev_copy))

    return TimelineResponse(
        events=out_events,
        next_cursor=next_cursor,
        has_more=has_more,
        total_scanned=total_scanned,
        partial=partial_result,
    )


def _is_before_cursor(
    ts: datetime, eid: str, cur_ts: datetime, cur_id: Optional[str]
) -> bool:
    """Strictly older-than-cursor for clock/break events. Same semantics as
    the SQL-level filter we apply to activities/browser, but evaluated in
    Python because clock/break events flow through two columns each."""
    if ts < cur_ts:
        return True
    if ts > cur_ts:
        return False
    # equal timestamps — fall back to id ordering so we never repeat an event
    return eid < (cur_id or "")


# ──────────────────────────────────────────────────────────────────────
# GET /activity-log/summary
# ──────────────────────────────────────────────────────────────────────


def _engine_summary(
    db: Session,
    org_id: Any,
    effective_user: Optional[int],
    start_dt: datetime,
    end_dt: datetime,
    app_overrides: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Active Time Engine view of this window — the SINGLE SOURCE OF TRUTH for
    Active Time, Idle Time, Productivity and the Hourly chart (T3).

      * Active / Idle  <- ActivityDailyScore (per IST day; identical to the
                          figure the employee Dashboard shows via
                          /employees/me/day-summary).
      * Productivity   <- ActivityWindow.active_credit_seconds, classified by app
                          category (the engine's continuity-/screenshot-aware
                          per-window active credit).
      * Hourly         <- the same window credit, bucketed by UTC hour (the
                          frontend localises it, unchanged).

    Returns None when the engine has no data for this window (no windows for the
    range, or no daily scores for the covered IST days) so the caller falls back
    to the legacy per-tick aggregation. Browser time is deliberately NOT folded
    in here (it has no engine window) — it stays in browser_visits / top_domains.
    Counters (clicks/keys/screenshots/sessions/breaks) and the top lists are
    computed by the caller from `activities` and are left unchanged.
    """
    # Organization-local calendar days (and their UTC midnight anchors)
    # intersecting the window. The engine's daily rows are anchored on the
    # tenant's midnight, so reading them on a fixed +05:30 would miss the row
    # entirely for a tenant in another zone and silently fall back to the
    # legacy per-tick aggregation.
    from app import _tz as _tzmod
    IST = _tzmod.org_timezone(db, org_id)
    d = start_dt.astimezone(IST).date()
    d_last = (end_dt - timedelta(seconds=1)).astimezone(IST).date()
    covered_dates: List[Any] = []
    while d <= d_last:
        covered_dates.append(d)
        d += timedelta(days=1)
    if not covered_dates:
        return None
    anchors: List[datetime] = [
        datetime.combine(cd, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
        for cd in covered_dates
    ]

    # ── Productivity + Hourly source: per-window engine credit (4 cols only) ──
    wq = db.query(
        models.ActivityWindow.window_start,
        models.ActivityWindow.app_name,
        models.ActivityWindow.active_credit_seconds,
        models.ActivityWindow.interval_seconds,
    ).filter(
        models.ActivityWindow.window_start >= start_dt,
        models.ActivityWindow.window_start <= end_dt,
    )
    if effective_user is not None:
        wq = wq.filter(models.ActivityWindow.user_id == effective_user)
    else:
        # ActivityWindow has no organization_id column — scope via the owning
        # user so an admin's org-wide view never bleeds another tenant's rows.
        wq = wq.join(models.User, models.User.id == models.ActivityWindow.user_id).filter(
            models.User.organization_id == org_id
        )
    win_rows = wq.all()

    # ── Active / Idle ──
    if effective_user is not None:
        # Single user: delegate to the SAME computation the employee Dashboard
        # uses (/employees/me/day-summary), per covered IST day. That function
        # already encapsulates "engine score when the day has windows, else the
        # legacy per-tick estimate clamped to worked time" — so the Activity Log
        # matches the Dashboard byte-for-byte in every case, including days whose
        # (backfilled) windows score zero (daily window_count == 0 → Dashboard
        # legacy fallback). We deliberately do NOT re-derive active time here.
        from app.employees.router import get_day_summary as _day_summary
        user_obj = (
            db.query(models.User).filter(models.User.id == effective_user).first()
        )
        if user_obj is None:
            return None
        active_seconds = 0
        idle_seconds = 0
        for cd in covered_dates:
            ds = _day_summary(date=cd.strftime("%Y-%m-%d"), db=db, user=user_obj)
            a = int(ds.get("active_seconds") or 0)
            w = int(ds.get("worked_seconds") or 0)
            active_seconds += a
            idle_seconds += max(0, w - a)  # Dashboard shows idle = worked - active
    else:
        # Org-wide: sum the cached daily scores (read-only; the reconcile worker
        # keeps active users fresh). Scoped to the org via the owning user, so
        # windows with a NULL/foreign user_id never leak into another tenant.
        # No windows / no scored day → fall back to the legacy aggregation.
        if not win_rows:
            return None
        dq = (
            db.query(
                func.coalesce(func.sum(models.ActivityDailyScore.active_seconds), 0),
                func.coalesce(func.sum(models.ActivityDailyScore.idle_seconds), 0),
                func.count(models.ActivityDailyScore.id),
            )
            .join(models.User, models.User.id == models.ActivityDailyScore.user_id)
            .filter(
                models.ActivityDailyScore.day.in_(anchors),
                models.User.organization_id == org_id,
            )
        )
        active_seconds, idle_seconds, daily_n = dq.one()
        if not daily_n:
            return None  # windows exist but day not scored yet → legacy fallback

    # Productivity + Hourly are derived from per-window engine credit. When the
    # engine has NO windows for this user/range (the OPEN production staging gap:
    # a subset of users produce raw Activity but no ActivityWindow rows), emitting
    # zeroed buckets/hourly here would OVERWRITE the caller's usable legacy
    # raw-Activity aggregation and show a fake 0% pie + empty hourly chart.
    # Instead return None for THOSE TWO ONLY, so the caller keeps its legacy
    # productivity/hourly, while still using the engine's authoritative active/idle
    # above (get_day_summary, which itself falls back to the legacy per-tick
    # estimate clamped to worked time on window-less days). Active/Idle are a
    # separate axis from the productivity breakdown, so this is not double-counting.
    # (The org-wide branch already returned None entirely when win_rows is empty,
    # so this guard only triggers for the single-user path.)
    if not win_rows:
        return {
            "active_seconds": int(active_seconds or 0),
            "idle_seconds": int(idle_seconds or 0),
            "bucket_seconds": None,   # → caller keeps legacy productivity
            "hourly": None,           # → caller keeps legacy hourly
        }

    bucket_seconds = {P_PRODUCTIVE: 0, P_NEUTRAL: 0, P_DISTRACTING: 0}
    hourly: Dict[int, Dict[str, int]] = {
        h: {"productive": 0, "neutral": 0, "distracting": 0, "idle": 0} for h in range(24)
    }
    for window_start, app_name, credit, interval in win_rows:
        credit = int(credit or 0)
        bucket = _normalise_app_category(categorise_app(app_name, app_overrides))
        bucket_seconds[bucket] += credit
        ts = _aware(window_start)
        if ts is not None:
            hr = ts.astimezone(timezone.utc).hour
            hourly[hr][bucket] += credit
            hourly[hr]["idle"] += max(0, int(interval or 0) - credit)

    return {
        "active_seconds": int(active_seconds or 0),
        "idle_seconds": int(idle_seconds or 0),
        "bucket_seconds": bucket_seconds,
        "hourly": hourly,
    }


@router.get("/summary", response_model=SummaryResponse)
def get_activity_summary(
    start: Optional[str] = Query(None),
    end: Optional[str]   = Query(None),
    user_id: Optional[int] = Query(None),
    scope: Optional[str] = Query(None, description="'self' forces caller-scoped data even for admins."),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    org: models.Organization = Depends(get_current_organization),
):
    start_dt, end_dt = _resolve_window(start, end)
    effective_user = _resolve_user_filter(user_id, user, scope)
    _shadow(db, user, "time.playback.basic",      # SHADOW ONLY
            route="GET /activity-log/summary")
    app_overrides, domain_overrides = _get_overrides(db, org.id)

    # Same per-request statement-timeout the timeline endpoint uses — the
    # summary aggregates over the same tables, so it has the same risk of
    # tripping the DB error handler with a 503 on wide windows.
    _apply_timeline_timeout(db)

    # ── totals from activities ──
    # Each Activity row represents one tracker tick. Tick length is the
    # configured `screenshot_interval` (5s in dev, 30 min in prod), so we
    # *must* read it dynamically — hard-coding 30s under-counts active
    # time by 60× when the admin sets the interval to 1800s.
    #
    # For accuracy we cap each tick at the gap to the next tick from the
    # same user (so a clock-out or app-quit mid-interval doesn't credit
    # the user with a full 30 min of presence) and subtract the per-tick
    # `idle_seconds` from active time.
    tick_max = _get_tick_seconds(db, org.id)

    aq = db.query(models.Activity).filter(
        models.Activity.organization_id == org.id,
        models.Activity.timestamp >= start_dt,
        models.Activity.timestamp <= end_dt,
    )
    if effective_user is not None:
        aq = aq.filter(models.Activity.user_id == effective_user)
    # Order by user then ts so we can cheaply compute per-user gaps below.
    activities = (
        aq.order_by(models.Activity.user_id, models.Activity.timestamp)
        .all()
    )

    active_seconds = 0
    idle_seconds = 0
    mouse_clicks = 0
    key_presses = 0
    screenshot_count = 0
    bucket_seconds = {P_PRODUCTIVE: 0, P_NEUTRAL: 0, P_DISTRACTING: 0}
    hourly: Dict[int, Dict[str, int]] = {
        h: {"productive": 0, "neutral": 0, "distracting": 0, "idle": 0}
        for h in range(24)
    }
    app_counter: Dict[str, Dict[str, Any]] = {}

    n = len(activities)
    for i, a in enumerate(activities):
        if a.file_path or a.thumbnail:
            screenshot_count += 1
        mc = int(a.mouse_clicks or 0)
        kp = int(a.key_presses or 0)
        mouse_clicks += mc
        key_presses += kp
        is_active = bool(a.is_active)

        # Per-tick effective length: min(row tick length, gap to next tick
        # from the same user). Prefer the tracker's REAL per-tick length
        # (Activity.interval_seconds) so a 30s tracker is never credited a
        # full configured interval; legacy rows that don't carry it fall back
        # to the org's configured screenshot interval. Long gaps
        # (offline/clocked-out) still clamp to the tick so we never credit
        # hours of real-world time to a single row.
        row_tick = (
            a.interval_seconds
            if (a.interval_seconds and a.interval_seconds > 0)
            else tick_max
        )
        tick = row_tick
        if i + 1 < n:
            nxt = activities[i + 1]
            if nxt.user_id == a.user_id:
                a_ts = _aware(a.timestamp)
                n_ts = _aware(nxt.timestamp)
                if a_ts is not None and n_ts is not None:
                    gap = int((n_ts - a_ts).total_seconds())
                    if 0 < gap < tick:
                        tick = gap

        # Subtract the tracker-reported idle portion of this tick. Tracker
        # may report idle_seconds > tick when the clock skews; clamp.
        idle_in_tick = max(0, min(tick, int(a.idle_seconds or 0)))
        if is_active:
            active_seconds += tick - idle_in_tick
            idle_seconds  += idle_in_tick
        else:
            idle_seconds  += tick

        bucket = _normalise_app_category(categorise_app(a.app_name, app_overrides))
        # Productivity bucketing only credits time the user was actually
        # using the app (active and not internally idle).
        prod_secs = max(0, tick - idle_in_tick) if is_active else 0
        bucket_seconds[bucket] = bucket_seconds.get(bucket, 0) + prod_secs

        ts = _aware(a.timestamp)
        if ts is not None:
            hr = ts.astimezone(timezone.utc).hour
            if is_active:
                hourly[hr][bucket] = hourly[hr].get(bucket, 0) + prod_secs
                if idle_in_tick > 0:
                    hourly[hr]["idle"] = hourly[hr].get("idle", 0) + idle_in_tick
            else:
                hourly[hr]["idle"] = hourly[hr].get("idle", 0) + tick

        # Per-app rollup
        key = (a.app_name or "Unknown")
        slot = app_counter.setdefault(
            key,
            {"app_name": key, "category": bucket, "seconds": 0, "screenshots": 0},
        )
        slot["seconds"] += prod_secs
        if a.file_path or a.thumbnail:
            slot["screenshots"] += 1

    # ── browser rollup ──
    bq = db.query(models.BrowserActivity).filter(
        models.BrowserActivity.organization_id == org.id,
        models.BrowserActivity.started_at >= start_dt,
        models.BrowserActivity.started_at <= end_dt,
    )
    if effective_user is not None:
        bq = bq.filter(models.BrowserActivity.user_id == effective_user)
    browsers = bq.all()

    domain_counter: Dict[str, Dict[str, Any]] = {}
    for b in browsers:
        bucket = _normalise_domain_category(
            categorise_domain(b.domain, domain_overrides)
        )
        active = int(b.active_sec or 0)
        # Browser active seconds also count towards the productivity buckets
        # so the pie chart reflects the user's full behaviour, not just their
        # desktop apps.
        bucket_seconds[bucket] = bucket_seconds.get(bucket, 0) + active

        ts = _aware(b.started_at)
        if ts is not None and active > 0:
            hr = ts.astimezone(timezone.utc).hour
            hourly[hr][bucket] = hourly[hr].get(bucket, 0) + active

        key = (b.domain or "(unknown)")
        slot = domain_counter.setdefault(
            key,
            {"domain": key, "category": bucket, "active_seconds": 0, "visits": 0},
        )
        slot["active_seconds"] += active
        slot["visits"] += 1

    # ── session / break counts ──
    # Tenant isolation: scope counts to the caller's org, matching the
    # activities/browser queries above. Legacy rows predating the
    # clock_sessions / break_sessions organization_id backfill carry NULL —
    # keep those visible (historical single-tenant data) rather than dropping
    # the count to zero, while still excluding any *other* org's rows.
    sq = db.query(models.ClockSession).filter(
        models.ClockSession.clock_in_time >= start_dt,
        models.ClockSession.clock_in_time <= end_dt,
        or_(
            models.ClockSession.organization_id == org.id,
            models.ClockSession.organization_id.is_(None),
        ),
    )
    if effective_user is not None:
        sq = sq.filter(models.ClockSession.user_id == effective_user)
    session_count = sq.count()

    brq = db.query(models.BreakSession).filter(
        models.BreakSession.break_start >= start_dt,
        models.BreakSession.break_start <= end_dt,
        or_(
            models.BreakSession.organization_id == org.id,
            models.BreakSession.organization_id.is_(None),
        ),
    )
    if effective_user is not None:
        brq = brq.filter(models.BreakSession.user_id == effective_user)
    break_count = brq.count()

    # ── T3: Active Time Engine as the single source of truth ──
    # Active Time, Idle Time, Productivity and the Hourly chart now come from the
    # engine (ActivityDailyScore for active/idle, ActivityWindow credit for
    # productivity/hourly) so the Activity Log matches the employee Dashboard.
    # The per-tick aggregation computed above is retained ONLY as the legacy
    # fallback for windows the engine doesn't cover (pre-engine history, or the
    # engine disabled). Counters (mouse_clicks/key_presses/screenshots),
    # sessions, breaks, browser_visits and the top apps/domains lists are
    # UNCHANGED — still derived from the per-tick loops above.
    _engine = _engine_summary(db, org.id, effective_user, start_dt, end_dt, app_overrides)
    if _engine is not None:
        active_seconds = _engine["active_seconds"]
        idle_seconds   = _engine["idle_seconds"]
        # Productivity + hourly stay on the engine ONLY when it actually has
        # window data. When the engine has no windows for this user/range it
        # returns None for these two (see _engine_summary) and we KEEP the legacy
        # raw-Activity aggregation computed above — this prevents a fake 0%
        # productivity / empty hourly chart while raw Activity rows exist.
        if _engine.get("bucket_seconds") is not None:
            bucket_seconds = _engine["bucket_seconds"]
        if _engine.get("hourly") is not None:
            hourly = _engine["hourly"]

    # Top 10 lists, sorted by seconds desc.
    top_apps = sorted(
        app_counter.values(), key=lambda r: r["seconds"], reverse=True
    )[:10]
    top_domains = sorted(
        domain_counter.values(), key=lambda r: r["active_seconds"], reverse=True
    )[:10]

    hourly_list = [
        {"hour": h, **hourly[h]}
        for h in range(24)
    ]

    return SummaryResponse(
        range={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        totals={
            "active_seconds": active_seconds,
            "idle_seconds": idle_seconds,
            "mouse_clicks": mouse_clicks,
            "key_presses": key_presses,
            "screenshots": screenshot_count,
            "sessions": session_count,
            "breaks": break_count,
            "browser_visits": len(browsers),
        },
        productivity={
            P_PRODUCTIVE:  int(bucket_seconds.get(P_PRODUCTIVE, 0)),
            P_NEUTRAL:     int(bucket_seconds.get(P_NEUTRAL, 0)),
            P_DISTRACTING: int(bucket_seconds.get(P_DISTRACTING, 0)),
        },
        hourly=hourly_list,
        top_apps=top_apps,
        top_domains=top_domains,
    )
