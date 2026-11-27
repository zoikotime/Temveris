"""
Temveris – Browser Activity Router
===================================
Endpoints consumed by the Temveris browser extension (Chrome/Edge/Firefox).
The extension reports per-tab focus intervals with URL, domain, favicon,
and interaction counters so the admin dashboard can show exactly which
website each employee is working on — not just "Chrome".

Endpoints
---------
POST /browser/heartbeat   Extension → current active tab state (upserts LiveStatus).
POST /browser/event       Extension → completed tab session (tab switch / close / idle).
GET  /browser/live        Admin   → current tab state for every user in the org.
GET  /browser/history     Admin   → per-user tab timeline with date filtering.
GET  /browser/summary     Admin   → per-user per-domain roll-up for a date range.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, desc
from sqlalchemy.orm import Session

from database.database import get_db
from app.commercial.enforcement import shadow_capability as _shadow
from database import models
from app.auth.service import get_current_user, get_current_organization
from app.browser.productivity import (
    categorise, load_overrides, VALID_CATEGORIES,
    CATEGORY_WORK, CATEGORY_NEUTRAL, CATEGORY_DISTRACTION,
    DEFAULT_CATEGORIES,
)

router = APIRouter(prefix="/browser", tags=["Browser Activity"])


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        return host.lower().lstrip("www.") or "unknown"
    except Exception:
        return "unknown"


def _active_session_id(db: Session, user_id: int) -> Optional[int]:
    """Return the id of the user's currently-open clock session, if any."""
    row = (
        db.query(models.ClockSession.id)
        .filter(models.ClockSession.user_id == user_id,
                models.ClockSession.clock_out_time.is_(None))
        .order_by(desc(models.ClockSession.clock_in_time))
        .first()
    )
    return row.id if row else None


def _broadcast(payload: dict) -> None:
    """Fire-and-forget WebSocket broadcast, org-scoped — never raises."""
    try:
        from websocket_manager import manager
        org_id = payload.get("organization_id")
        if org_id:
            manager.broadcast_to_org_sync(org_id, json.dumps(payload, default=str))
    except Exception:
        pass


def _assert_user_in_org(db: Session, target_user_id: int, viewer: models.User) -> None:
    """Raise 404 when target_user_id is not in the viewer's organization."""
    if target_user_id == viewer.id:
        return
    ok = db.query(models.User).filter(
        models.User.id == target_user_id,
        models.User.organization_id == viewer.organization_id,
    ).first()
    if not ok:
        raise HTTPException(404, "User not found")


# ──────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────
class HeartbeatIn(BaseModel):
    url: str
    tab_title: Optional[str] = None
    favicon_url: Optional[str] = None
    browser: Optional[str] = None           # "chrome" | "edge" | "firefox"
    is_idle: bool = False
    tab_since: Optional[datetime] = None    # when this tab became active
    # Optional counters since last heartbeat (extension resets locally after send)
    mouse_clicks: int = 0
    key_presses: int = 0
    scrolls: int = 0


class EventIn(BaseModel):
    url: str
    tab_title: Optional[str] = None
    favicon_url: Optional[str] = None
    browser: Optional[str] = None
    started_at: datetime
    ended_at: datetime
    active_sec: int = 0
    idle_sec: int = 0
    mouse_clicks: int = 0
    key_presses: int = 0
    scrolls: int = 0


# ──────────────────────────────────────────────────────────────────────
# Ingest (called by the browser extension with user's JWT)
# ──────────────────────────────────────────────────────────────────────
@router.post("/heartbeat")
def heartbeat(
    payload: HeartbeatIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    domain = _domain_of(payload.url)
    now = datetime.now(timezone.utc)

    live = db.query(models.LiveStatus).filter_by(user_id=user.id).first()
    if not live:
        live = models.LiveStatus(
            user_id=user.id,
            organization_id=user.organization_id,
        )
        db.add(live)
    elif live.organization_id is None and user.organization_id is not None:
        # Backfill org on legacy rows that pre-date the migration.
        live.organization_id = user.organization_id

    # If the active app isn't a browser, surface the browser as app_name so
    # existing "current app" widgets still work.
    if payload.browser:
        live.app_name = payload.browser
    live.window_title = payload.tab_title or live.window_title
    live.url         = payload.url
    live.domain      = domain
    live.tab_title   = payload.tab_title
    live.favicon_url = payload.favicon_url
    live.browser     = payload.browser
    live.is_idle     = payload.is_idle
    live.tab_since   = payload.tab_since or live.tab_since or now
    live.updated_at  = now
    db.commit()

    _broadcast({
        "type":            "browser_activity",
        "event":           "heartbeat",
        "organization_id": user.organization_id,
        "user_id":         user.id,
        "user_name":       user.name,
        "url":             payload.url,
        "domain":          domain,
        "tab_title":       payload.tab_title,
        "favicon_url":     payload.favicon_url,
        "browser":         payload.browser,
        "is_idle":         payload.is_idle,
        "tab_since":       (payload.tab_since or live.tab_since or now).isoformat(),
        "timestamp":       now.isoformat(),
    })
    return {"success": True}


@router.post("/event")
def ingest_event(
    payload: EventIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if payload.ended_at <= payload.started_at:
        raise HTTPException(400, "ended_at must be after started_at")

    duration = int((payload.ended_at - payload.started_at).total_seconds())
    if duration < 1:
        return {"success": True, "skipped": True}

    domain = _domain_of(payload.url)
    session_id = _active_session_id(db, user.id)

    row = models.BrowserActivity(
        user_id         = user.id,
        organization_id = user.organization_id,
        session_id      = session_id,
        url             = payload.url,
        domain          = domain,
        tab_title       = payload.tab_title,
        favicon_url     = payload.favicon_url,
        browser         = payload.browser,
        started_at      = payload.started_at,
        ended_at        = payload.ended_at,
        duration_sec    = duration,
        active_sec      = max(0, min(payload.active_sec, duration)),
        idle_sec        = max(0, min(payload.idle_sec, duration)),
        mouse_clicks    = payload.mouse_clicks,
        key_presses     = payload.key_presses,
        scrolls         = payload.scrolls,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    _broadcast({
        "type":            "browser_activity",
        "event":           "tab_closed",
        "organization_id": user.organization_id,
        "user_id":         user.id,
        "user_name":       user.name,
        "activity_id":     row.id,
        "url":             payload.url,
        "domain":          domain,
        "tab_title":       payload.tab_title,
        "duration":        duration,
        "started_at":      payload.started_at.isoformat(),
        "ended_at":        payload.ended_at.isoformat(),
    })
    return {"success": True, "id": row.id}


# ──────────────────────────────────────────────────────────────────────
# Admin queries
# ──────────────────────────────────────────────────────────────────────
@router.get("/live")
def live_team(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Current browser tab for every user in the caller's org.
    Filters via the User join so legacy LiveStatus rows (pre-migration,
    without organization_id stamped) are still isolated by org."""
    # SHADOW ONLY - the live team board is the closest shipped analogue of
    # the dashboard overview surface.
    _shadow(db, user, "dashboard.overview.view", route="GET /browser/live")
    rows = (
        db.query(models.LiveStatus, models.User.name)
        .join(models.User, models.User.id == models.LiveStatus.user_id)
        .filter(
            models.LiveStatus.url.isnot(None),
            models.User.organization_id == user.organization_id,
        )
        .all()
    )
    overrides = load_overrides(db, user.organization_id)
    now = datetime.now(timezone.utc)
    out = []
    for live, name in rows:
        tab_since = live.tab_since or live.updated_at
        on_tab_sec = int((now - tab_since).total_seconds()) if tab_since else 0
        out.append({
            "user_id":     live.user_id,
            "user_name":   name,
            "url":         live.url,
            "domain":      live.domain,
            "tab_title":   live.tab_title,
            "favicon_url": live.favicon_url,
            "browser":     live.browser,
            "is_idle":     bool(live.is_idle),
            "tab_since":   tab_since.isoformat() if tab_since else None,
            "on_tab_sec":  max(0, on_tab_sec),
            "updated_at":  live.updated_at.isoformat() if live.updated_at else None,
            "category":    categorise(live.domain, overrides),
        })
    return out


def _parse_range(from_: Optional[str], to: Optional[str]):
    today = datetime.now(timezone.utc).date()
    try:
        d_from = datetime.fromisoformat(from_) if from_ else datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, "Invalid 'from' date")
    try:
        d_to = datetime.fromisoformat(to) if to else d_from + timedelta(days=1)
    except ValueError:
        raise HTTPException(400, "Invalid 'to' date")
    if d_to <= d_from:
        raise HTTPException(400, "'to' must be after 'from'")
    return d_from, d_to


@router.get("/history")
def history(
    user_id: int = Query(..., description="Employee to load"),
    from_:   Optional[str] = Query(None, alias="from"),
    to:      Optional[str] = Query(None),
    limit:   int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Flat tab-session timeline for one user inside a date range.
    Validates that the requested user_id belongs to the viewer's org."""
    # SHADOW ONLY — records what entitlement enforcement would decide and
    # returns None whatever happens. It cannot raise and cannot block; the
    # handler below is unchanged. Apps History and Website Categories are denied
    # to every plan, so this is the highest-value place to observe first.
    _shadow(db, viewer, "productivity.website_categories",
            route="GET /browser/history")
    _assert_user_in_org(db, user_id, viewer)
    d_from, d_to = _parse_range(from_, to)
    rows = (
        db.query(models.BrowserActivity)
        .filter(
            models.BrowserActivity.user_id == user_id,
            models.BrowserActivity.organization_id == viewer.organization_id,
            models.BrowserActivity.started_at >= d_from,
            models.BrowserActivity.started_at < d_to,
        )
        .order_by(desc(models.BrowserActivity.started_at))
        .limit(limit)
        .all()
    )
    overrides = load_overrides(db, viewer.organization_id)
    return [{
        "id":           r.id,
        "url":          r.url,
        "domain":       r.domain,
        "tab_title":    r.tab_title,
        "favicon_url":  r.favicon_url,
        "browser":      r.browser,
        "started_at":   r.started_at.isoformat(),
        "ended_at":     r.ended_at.isoformat() if r.ended_at else None,
        "duration_sec": r.duration_sec,
        "active_sec":   r.active_sec,
        "idle_sec":     r.idle_sec,
        "mouse_clicks": r.mouse_clicks,
        "key_presses":  r.key_presses,
        "scrolls":      r.scrolls,
        "category":     categorise(r.domain, overrides),
    } for r in rows]


@router.get("/summary")
def summary(
    user_id: int = Query(...),
    from_:   Optional[str] = Query(None, alias="from"),
    to:      Optional[str] = Query(None),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Per-domain totals + overall stats for pie/bar charts on the detail view."""
    # SHADOW ONLY - observes and returns None; cannot raise, cannot block.
    _shadow(db, viewer, "productivity.apps_history",
            route="GET /browser/summary")
    _assert_user_in_org(db, user_id, viewer)
    d_from, d_to = _parse_range(from_, to)

    per_domain = (
        db.query(
            models.BrowserActivity.domain,
            func.min(models.BrowserActivity.favicon_url).label("favicon_url"),
            func.sum(models.BrowserActivity.duration_sec).label("duration"),
            func.sum(models.BrowserActivity.active_sec).label("active"),
            func.sum(models.BrowserActivity.idle_sec).label("idle"),
            func.count(models.BrowserActivity.id).label("visits"),
        )
        .filter(
            models.BrowserActivity.user_id == user_id,
            models.BrowserActivity.organization_id == viewer.organization_id,
            models.BrowserActivity.started_at >= d_from,
            models.BrowserActivity.started_at < d_to,
        )
        .group_by(models.BrowserActivity.domain)
        .order_by(desc("duration"))
        .all()
    )
    overrides = load_overrides(db, viewer.organization_id)
    total = sum(int(r.duration or 0) for r in per_domain) or 1
    sites = [{
        "domain":      r.domain,
        "favicon_url": r.favicon_url,
        "duration":    int(r.duration or 0),
        "active":      int(r.active or 0),
        "idle":        int(r.idle or 0),
        "visits":      int(r.visits or 0),
        "percentage":  round((int(r.duration or 0) / total) * 100, 1),
        "category":    categorise(r.domain, overrides),
    } for r in per_domain]

    by_cat = {CATEGORY_WORK: 0, CATEGORY_NEUTRAL: 0, CATEGORY_DISTRACTION: 0}
    for s in sites:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + s["duration"]

    total_active = sum(s["active"] for s in sites)
    total_idle   = sum(s["idle"]   for s in sites)
    total_dur    = sum(s["duration"] for s in sites)

    weighted = by_cat[CATEGORY_WORK] + 0.5 * by_cat[CATEGORY_NEUTRAL]
    productivity_score = round((weighted / total_dur) * 100, 1) if total_dur else 0.0

    return {
        "user_id":           user_id,
        "from":              d_from.isoformat(),
        "to":                d_to.isoformat(),
        "total_sec":         total_dur,
        "active_sec":        total_active,
        "idle_sec":          total_idle,
        "active_pct":        round((total_active / total_dur) * 100, 1) if total_dur else 0.0,
        "categories": {
            "work":        by_cat[CATEGORY_WORK],
            "neutral":     by_cat[CATEGORY_NEUTRAL],
            "distraction": by_cat[CATEGORY_DISTRACTION],
        },
        "productivity_score": productivity_score,
        "sites":             sites,
    }


@router.get("/trend")
def trend(
    user_id: int = Query(...),
    days:    int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    """Daily productivity-score series for the last N days."""
    _assert_user_in_org(db, user_id, viewer)
    now = datetime.now(timezone.utc)
    start = datetime.combine((now - timedelta(days=days - 1)).date(), datetime.min.time(), tzinfo=timezone.utc)

    rows = (
        db.query(
            func.date(models.BrowserActivity.started_at).label("day"),
            models.BrowserActivity.domain,
            func.sum(models.BrowserActivity.duration_sec).label("duration"),
        )
        .filter(
            models.BrowserActivity.user_id == user_id,
            models.BrowserActivity.organization_id == viewer.organization_id,
            models.BrowserActivity.started_at >= start,
        )
        .group_by("day", models.BrowserActivity.domain)
        .all()
    )
    overrides = load_overrides(db, viewer.organization_id)

    by_day: dict = {}
    for r in rows:
        day_key = str(r.day)
        bucket = by_day.setdefault(day_key, {"work": 0, "neutral": 0, "distraction": 0})
        cat = categorise(r.domain, overrides)
        bucket[cat] = bucket.get(cat, 0) + int(r.duration or 0)

    series = []
    for i in range(days):
        d = (start + timedelta(days=i)).date()
        key = str(d)
        b = by_day.get(key)
        if not b:
            series.append({"date": key, "score": None, "total_sec": 0})
            continue
        total = b["work"] + b["neutral"] + b["distraction"]
        score = round(((b["work"] + 0.5 * b["neutral"]) / total) * 100, 1) if total else 0.0
        series.append({
            "date": key,
            "score": score,
            "total_sec": total,
            "work": b["work"],
            "neutral": b["neutral"],
            "distraction": b["distraction"],
        })
    return {"user_id": user_id, "days": days, "series": series}


# ──────────────────────────────────────────────────────────────────────
# Admin: list / upsert / delete domain category overrides
# ──────────────────────────────────────────────────────────────────────
class CategoryIn(BaseModel):
    domain: str
    category: str = Field(..., description="work | neutral | distraction")


def _require_admin(user: models.User):
    if getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Admin only")


@router.get("/categories/defaults")
def list_category_defaults(
    user: models.User = Depends(get_current_user),
):
    """Built-in curated defaults shipped with the product. Read-only."""
    _require_admin(user)
    return [
        {"domain": d, "category": c}
        for d, c in sorted(DEFAULT_CATEGORIES.items())
    ]


@router.get("/categories")
def list_categories(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Return all admin overrides for the caller's org."""
    _require_admin(user)
    rows = (
        db.query(models.DomainCategory)
        .filter(models.DomainCategory.organization_id == user.organization_id)
        .order_by(models.DomainCategory.domain)
        .all()
    )
    return [{
        "domain":     r.domain,
        "category":   r.category,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]


@router.post("/categories")
def upsert_category(
    payload: CategoryIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    if payload.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(VALID_CATEGORIES)}")
    domain = payload.domain.strip().lower().lstrip(".")
    if not domain:
        raise HTTPException(400, "domain is required")

    row = db.query(models.DomainCategory).filter(
        models.DomainCategory.domain == domain,
        models.DomainCategory.organization_id == user.organization_id,
    ).first()
    if row:
        row.category      = payload.category
        row.updated_by_id = user.id
    else:
        row = models.DomainCategory(
            organization_id=user.organization_id,
            domain=domain,
            category=payload.category,
            updated_by_id=user.id,
        )
        db.add(row)
    db.commit()
    return {"success": True, "domain": domain, "category": payload.category}


@router.delete("/categories/{domain}")
def delete_category(
    domain: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _require_admin(user)
    domain = domain.strip().lower().lstrip(".")
    row = db.query(models.DomainCategory).filter(
        models.DomainCategory.domain == domain,
        models.DomainCategory.organization_id == user.organization_id,
    ).first()
    if row:
        db.delete(row)
        db.commit()
    return {"success": True}
