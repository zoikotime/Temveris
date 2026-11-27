"""
Temveris – Tracking Router  (Desktop Tracker ↔ Backend)
========================================================
These endpoints are called ONLY by the desktop tracker application.
The backend does NOT do any tracking itself — it only receives and stores data.

Endpoints:
  GET  /tracking/status                    → tracker polls if user is clocked in
  GET  /tracking/ping                      → lightweight cache-first status poll
  POST /tracking/sync                      → combined heartbeat + status
  POST /tracking/heartbeat                 → heartbeat-only (live app usage)
  POST /tracking/screenshot                → tracker uploads base64 screenshot
  POST /tracking/cleanup/old-screenshots   → admin: delete screenshots > 30d old

Storage & egress strategy (aggressive):
  • Compression:   WebP @ Q30 active / Q20 idle, max 1600x900 / 1280x720
                   → typical 1-2MB PNG drops to 10-50KB
  • Dedup:         SHA256 of raw bytes matches recent upload → activity row
                   is still created, but the image is not re-uploaded
  • Idle frames:   ALWAYS uploaded so the frontend can render the red-border
                   "IDLE" indicator. Just compressed harder than active frames.
  • Cache-Control: Uploaded files carry immutable 1-year cache headers so
                   Supabase's CDN and browsers cache aggressively. This cuts
                   billable egress by 60-80% because each screenshot is fetched
                   once and served from cache on every subsequent view.
  • Retention:     Screenshots older than RETENTION_DAYS are purged from the
                   bucket by POST /tracking/cleanup/old-screenshots. Activity
                   rows are preserved (file_path/thumbnail → NULL) so
                   timeline, stats, and timesheets remain accurate.
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
import base64
import hashlib
import os
import json
import io

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

from database.database import get_db
from database import models
from app.auth.service import get_current_user
from app._app_classify import is_desktop_tool, normalize_proc
from app._time_utils import iso_z

router = APIRouter(prefix="/tracking", tags=["Desktop Tracking"])

# Offboarding runtime-enforcement gate. Imported lazily-safe at module load; the
# offboarding package has no import-time dependency on tracking, so there is no
# cycle. ``_tracking_constants`` is aliased to avoid clashing with this module's
# own names.
from app.offboarding import enforcement as _offb_enforcement
from app.offboarding import constants as _tracking_constants


def _client_ip(request) -> Optional[str]:
    try:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd
        return request.client.host if request.client else None
    except Exception:
        return None


def _parse_client_ts(raw: Optional[str]):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _enforce_tracking_stop(db: Session, user: "models.User", kind: str, *,
                           meta: Optional[dict] = None, client_ts: Optional[str] = None,
                           source_ip: Optional[str] = None) -> None:
    """Consult the offboarding gate; reject + quarantine post-exit telemetry.

    Raises HTTP 403 when the worker's lifecycle state denies tracking. The
    blocked payload's metadata is quarantined (never written to the live
    tracking tables) and a CRITICAL incident is opened on an isolated session
    inside the gate, so the rejection is provable evidence — not a silent drop.
    """
    try:
        with db.begin_nested():
            decision = _offb_enforcement.check_and_quarantine_telemetry(
                db, worker_id=user.id, telemetry_kind=kind, payload_meta=meta,
                source_ip=source_ip, client_timestamp=_parse_client_ts(client_ts),
            )
    except HTTPException:
        raise
    except Exception:
        # The gate must fail SAFE for a known-offboarded worker, but a transient
        # error evaluating it should not take down tracking for active staff. We
        # log and allow; the background tracking-stop sweep is the backstop.
        import logging
        logging.getLogger("zoiko.tracking").exception(
            "offboarding enforcement gate error for user=%s", user.id)
        return
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "tracking_stopped",
                "reason": decision.reason,
                "message": "Tracking is disabled for this account (offboarding access closure).",
            },
        )

# ── Upload directory (local fallback only — bucket is canonical) ──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPLOAD_DIR = os.path.join(BASE_DIR, "..", "tracker-data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Retention config ──
# Override via env var RETENTION_DAYS if you want a different default.
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))

# ══════════════════════════════════════════════════════════════════════════════
# In-memory status cache
# ══════════════════════════════════════════════════════════════════════════════
# Keyed by user_id → {"data": {...}, "expires": float}
# TTL = 60s. Cuts DB queries for /status from 90/min (30 users × every 20s)
# to at most 1/min per user.
import time as _time
from app.commercial.enforcement import shadow_capability as _shadow

_STATUS_CACHE: dict = {}
_STATUS_TTL = 60  # seconds


def _cache_get(user_id: int):
    entry = _STATUS_CACHE.get(user_id)
    if entry and _time.monotonic() < entry["expires"]:
        return entry["data"]
    return None


def _cache_set(user_id: int, data: dict):
    _STATUS_CACHE[user_id] = {"data": data, "expires": _time.monotonic() + _STATUS_TTL}


def _cache_invalidate(user_id: int):
    _STATUS_CACHE.pop(user_id, None)


# ══════════════════════════════════════════════════════════════════════════════
# Dedup cache — last screenshot hash per user
# ══════════════════════════════════════════════════════════════════════════════
# Maps user_id → {"hash": "abc123...", "ts": float (monotonic)}.
# We keep the entry for DEDUP_TTL seconds; after that we always upload again
# even if the screen is identical, so admins still see fresh "proof of life"
# captures rather than an indefinite gap.
_DEDUP_CACHE: dict = {}
DEDUP_TTL = 600  # 10 minutes — re-upload at least every 10 min even if identical


def _dedup_check(user_id: int, img_hash: str) -> bool:
    """Return True if this hash matches a recent upload for the same user."""
    entry = _DEDUP_CACHE.get(user_id)
    if not entry:
        return False
    if _time.monotonic() - entry["ts"] > DEDUP_TTL:
        return False
    return entry["hash"] == img_hash


def _dedup_set(user_id: int, img_hash: str):
    _DEDUP_CACHE[user_id] = {"hash": img_hash, "ts": _time.monotonic()}


# ══════════════════════════════════════════════
# GET /tracking/ping  — lightweight status poll
# ══════════════════════════════════════════════
@router.get("/ping")
def tracking_ping(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    cached = _cache_get(user.id)
    if cached:
        return cached  # pure in-memory — zero DB queries

    # Cache miss — query DB once, cache result
    session = (
        db.query(models.ClockSession)
        .filter(
            and_(
                models.ClockSession.user_id == user.id,
                models.ClockSession.clock_out_time == None,
            )
        )
        .first()
    )

    if not session:
        result = {
            "clocked_in": False,
            "on_break": False,
            "status": "not_clocked_in",
            "user_id": user.id,
            "user_name": user.name,
        }
        _cache_set(user.id, result)
        return result

    active_break = (
        db.query(models.BreakSession)
        .filter(
            and_(
                models.BreakSession.user_id == user.id,
                models.BreakSession.break_end == None,
                models.BreakSession.break_start >= session.clock_in_time,
            )
        )
        .first()
    )

    result = {
        "clocked_in": True,
        "on_break": active_break is not None,
        "status": "on_break" if active_break else "working",
        "user_id": user.id,
        "user_name": user.name,
        "clock_in_time": iso_z(session.clock_in_time),
        "break_seconds": session.break_seconds or 0,
    }
    _cache_set(user.id, result)
    return result


# ══════════════════════════════════════════════
# POST /tracking/sync  — combined heartbeat + status
# ══════════════════════════════════════════════
class SyncPayload(BaseModel):
    app_name: str = "Desktop"
    window_title: Optional[str] = None
    timestamp: Optional[str] = None
    # Desktop tracker build (e.g. "1.0.11"). Optional so legacy clients that
    # don't send it keep working — those rows will show app_version=NULL.
    app_version: Optional[str] = None


@router.post("/sync")
def tracking_sync(
    payload: SyncPayload,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Single endpoint that updates live-status heartbeat AND returns
    current clock state. Desktop app uses this instead of two separate calls."""
    _enforce_tracking_stop(
        db, user, _tracking_constants.TelemetryKind.HEARTBEAT.value,
        meta={"app_name": payload.app_name, "window_title": payload.window_title,
              "client_ts": payload.timestamp, "app_version": payload.app_version},
        client_ts=payload.timestamp,
    )
    try:
        ts = datetime.fromisoformat((payload.timestamp or "").replace("Z", "+00:00"))
    except Exception:
        ts = datetime.now(timezone.utc)

    # Cap to the model's column width (String(32)) to defend against malformed
    # clients shipping long custom build strings.
    _ver = (payload.app_version or None)
    if _ver and len(_ver) > 32:
        _ver = _ver[:32]

    # Upsert live status (lightweight — single row per user)
    live = (
        db.query(models.LiveStatus).filter(models.LiveStatus.user_id == user.id).first()
    )
    # Capture the previously-seen foreground app BEFORE we overwrite it, so we
    # can detect a switch to a new desktop tool below.
    prev_app = live.app_name if live else None
    if live:
        live.app_name = payload.app_name or "Desktop"
        live.window_title = payload.window_title
        if _ver:
            live.app_version = _ver
        live.updated_at = ts
        if live.organization_id is None and user.organization_id is not None:
            live.organization_id = user.organization_id
    else:
        db.add(
            models.LiveStatus(
                user_id=user.id,
                organization_id=user.organization_id,
                app_name=payload.app_name or "Desktop",
                window_title=payload.window_title,
                app_version=_ver,
                updated_at=ts,
            )
        )
    db.commit()

    # ── Log desktop-tool usage into the activity timeline ────────────────────
    # The browser extension records every website visit, but desktop apps were
    # only captured per-screenshot (~once every several minutes), so a tester
    # who lives in Eclipse barely showed up. Here we record a point-event in the
    # dedicated app_sessions table whenever the foreground app SWITCHES to a new
    # desktop tool (browsers are skipped — already covered by website visits).
    # app_sessions is a DISPLAY-ONLY source for the timeline and is never read by
    # any productivity/idle/active-time calculation, so it cannot skew metrics.
    # Throttled to actual app changes to keep write volume low, and fully
    # defensive so it can never break the heartbeat response.
    if is_desktop_tool(payload.app_name) and normalize_proc(prev_app) != normalize_proc(payload.app_name):
        try:
            sess_id = (
                db.query(models.ClockSession.id)
                .filter(
                    models.ClockSession.user_id == user.id,
                    models.ClockSession.clock_out_time == None,
                )
                .scalar()
            )
            if sess_id is not None:
                db.add(
                    models.AppSession(
                        user_id=user.id,
                        organization_id=user.organization_id,
                        session_id=sess_id,
                        app_name=payload.app_name,
                        window_title=payload.window_title,
                        started_at=ts,
                    )
                )
                db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    # Return cached status — no extra DB query if cache is warm
    cached = _cache_get(user.id)
    if cached:
        return {"success": True, **cached}

    session = (
        db.query(models.ClockSession)
        .filter(
            and_(
                models.ClockSession.user_id == user.id,
                models.ClockSession.clock_out_time == None,
            )
        )
        .first()
    )
    active_break = None
    if session:
        active_break = (
            db.query(models.BreakSession)
            .filter(
                and_(
                    models.BreakSession.user_id == user.id,
                    models.BreakSession.break_end == None,
                    models.BreakSession.break_start >= session.clock_in_time,
                )
            )
            .first()
        )

    result = {
        "clocked_in": session is not None,
        "on_break": active_break is not None,
        "status": (
            ("on_break" if active_break else "working") if session else "not_clocked_in"
        ),
        "user_id": user.id,
        "user_name": user.name,
    }
    _cache_set(user.id, {**result})
    return {"success": True, **result}


# ══════════════════════════════════════════════
# GET /tracking/status  (kept for backward compatibility)
# ══════════════════════════════════════════════
@router.get("/status")
def tracking_status(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    cached = _cache_get(user.id)
    if cached:
        return cached

    session = (
        db.query(models.ClockSession)
        .filter(
            and_(
                models.ClockSession.user_id == user.id,
                models.ClockSession.clock_out_time == None,
            )
        )
        .first()
    )

    if not session:
        result = {
            "clocked_in": False,
            "on_break": False,
            "user_id": user.id,
            "user_name": user.name,
            "status": "not_clocked_in",
        }
        _cache_set(user.id, result)
        return result

    active_break = (
        db.query(models.BreakSession)
        .filter(
            and_(
                models.BreakSession.user_id == user.id,
                models.BreakSession.break_end == None,
                models.BreakSession.break_start >= session.clock_in_time,
            )
        )
        .first()
    )

    result = {
        "clocked_in": True,
        "on_break": active_break is not None,
        "user_id": user.id,
        "user_name": user.name,
        "status": "on_break" if active_break else "working",
        "clock_in_time": iso_z(session.clock_in_time),
        "break_seconds": session.break_seconds or 0,
    }
    _cache_set(user.id, result)
    return result


# ══════════════════════════════════════════════
# POST /tracking/heartbeat
# Lightweight activity ping with NO screenshot.
# Reports the current foreground app so admin "My Team" cards show live
# app usage even between screenshot intervals. Upserts a single row per
# user in live_statuses — table stays bounded.
# ══════════════════════════════════════════════
class HeartbeatPayload(BaseModel):
    app_name: str = "Desktop"
    window_title: Optional[str] = None
    timestamp: Optional[str] = None
    app_version: Optional[str] = None


@router.post("/heartbeat")
def tracking_heartbeat(
    payload: HeartbeatPayload,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Upsert live status + return current clock state so the desktop app
    can combine its heartbeat + status poll into a single request."""
    _enforce_tracking_stop(
        db, user, _tracking_constants.TelemetryKind.HEARTBEAT.value,
        meta={"app_name": payload.app_name, "window_title": payload.window_title,
              "client_ts": payload.timestamp, "app_version": payload.app_version},
        client_ts=payload.timestamp,
    )
    try:
        ts = datetime.fromisoformat((payload.timestamp or "").replace("Z", "+00:00"))
    except Exception:
        ts = datetime.now(timezone.utc)

    _ver = (payload.app_version or None)
    if _ver and len(_ver) > 32:
        _ver = _ver[:32]

    live = (
        db.query(models.LiveStatus).filter(models.LiveStatus.user_id == user.id).first()
    )
    if live:
        live.app_name = payload.app_name or "Desktop"
        live.window_title = payload.window_title
        if _ver:
            live.app_version = _ver
        live.updated_at = ts
        if live.organization_id is None and user.organization_id is not None:
            live.organization_id = user.organization_id
    else:
        db.add(
            models.LiveStatus(
                user_id=user.id,
                organization_id=user.organization_id,
                app_name=payload.app_name or "Desktop",
                window_title=payload.window_title,
                app_version=_ver,
                updated_at=ts,
            )
        )
    db.commit()

    # Return current clock status so desktop can skip a separate /status call.
    cached = _cache_get(user.id)
    if cached:
        return {"success": True, **cached}

    session = (
        db.query(models.ClockSession)
        .filter(
            and_(
                models.ClockSession.user_id == user.id,
                models.ClockSession.clock_out_time == None,
            )
        )
        .first()
    )
    active_break = None
    if session:
        active_break = (
            db.query(models.BreakSession)
            .filter(
                and_(
                    models.BreakSession.user_id == user.id,
                    models.BreakSession.break_end == None,
                    models.BreakSession.break_start >= session.clock_in_time,
                )
            )
            .first()
        )

    status_data = {
        "clocked_in": session is not None,
        "on_break": active_break is not None,
        "status": (
            ("on_break" if active_break else "working") if session else "not_clocked_in"
        ),
    }
    _cache_set(user.id, {**status_data, "user_id": user.id, "user_name": user.name})
    return {"success": True, **status_data}


# ══════════════════════════════════════════════════════════════════════════════
# POST /tracking/screenshot — the heavy-lifting endpoint
# Desktop tracker uploads a screenshot as base64 JSON.
# This is the ONLY way screenshots enter the system.
# ══════════════════════════════════════════════════════════════════════════════
class ScreenshotPayload(BaseModel):
    image: Optional[str] = ""  # base64-encoded PNG; empty when capture failed
    timestamp: str  # ISO 8601
    app_name: str = "Desktop"
    window_title: Optional[str] = None
    # Raw input counters for this interval — all default to 0 so older
    # trackers that only knew mouse_clicks / key_presses / idle_seconds
    # keep working unchanged.
    mouse_clicks: int = 0
    mouse_moves: int = 0      # 1-sec polls where the cursor moved
    mouse_scrolls: int = 0    # wheel events
    key_presses: int = 0
    # OS-measured active-seconds within this interval (# of 1-sec polls
    # where the system was not idle). This is the spec's "active minute"
    # signal at 1-second resolution and is the most accurate input for
    # the activity % calculation.
    active_seconds: int = 0
    # Wall-clock length of this interval in seconds. Used together with
    # active_seconds to compute idle-within-tick correctly. Falls back
    # to the configured screenshot interval when 0 (legacy trackers).
    interval_seconds: int = 0
    # Raw OS idle time at the moment of capture. Drives the 15-minute
    # idle-alert broadcast; NOT used for the activity % calculation.
    idle_seconds: int = 0
    # Whether the native global input hook (uiohook) was loaded on the client.
    # When False the keyboard/click/scroll counts are unreliable (0) and the
    # Active Time Engine scores the interval in "degraded" mode. None = unknown
    # (legacy trackers that don't report it) → treated as active.
    hook_active: Optional[bool] = None
    user_id: Optional[int] = None  # ignored — resolved from JWT


KEY_PRESSES_PER_SECOND_CAP = 12
MOUSE_CLICKS_PER_SECOND_CAP = 10
# Window ceiling applied ONLY to clients that report their real tick length
# (active_seconds / interval_seconds — v1.0.11+). Left unchanged so those
# clients are capped exactly as before.
COUNT_CAP_MAX_WINDOW_SECONDS = 300
# Absolute safety ceiling for the LEGACY-client window. v1.0.7 reports no tick
# length, so we substitute the configured screenshot interval (itself already
# validated to <= 3600s); this guards against a bad/garbage setting.
COUNT_CAP_MAX_LEGACY_WINDOW_SECONDS = 3600

# Cache the workspace-configured `screenshot_interval` (seconds) so we don't
# hit AppSettings on every upload. Same source + TTL pattern as
# employees.router._get_day_summary_tick; the setting changes rarely.
# Keyed by org_id — a single shared slot would serve one tenant's interval to
# every other tenant for the whole TTL.
_SCREENSHOT_INTERVAL_CACHE: dict = {}
_SCREENSHOT_INTERVAL_TTL = 60


def _get_configured_screenshot_interval(db: Session, org_id=None) -> int:
    """The admin-configured tracker tick length, in seconds (1..3600).

    Legacy v1.0.7 clients accumulate key/click counts over this whole interval
    but never report it, so the cap math must reconstruct it from settings
    rather than assuming a 30-second tick. Defaults to 30 only when the setting
    is absent/invalid (which is also the correct cap for a true 30s tick).
    """
    import time as _t
    now = _t.monotonic()
    cached = _SCREENSHOT_INTERVAL_CACHE.get(org_id)
    if cached and now <= cached["expires"]:
        return cached["value"]
    val = 30
    try:
        row = (
            db.query(models.AppSettings)
            .filter(models.AppSettings.organization_id == org_id,
                    models.AppSettings.key == "screenshot_interval",
                    models.AppSettings.team_key.is_(None),   # org-level only
                    models.AppSettings.user_id.is_(None))
            .first()
        )
        if row and row.value:
            n = int(str(row.value).strip())
            if 1 <= n <= 3600:
                val = n
    except Exception:
        pass
    _SCREENSHOT_INTERVAL_CACHE[org_id] = {"value": val, "expires": now + _SCREENSHOT_INTERVAL_TTL}
    return val


# The desktop tracker reports `active_seconds` as a COUNT OF ACTIVE IDLE-POLLS
# (it increments once per ACTIVITY_POLL_MS poll where the OS was non-idle), not
# as literal seconds. We multiply by this poll period at ingest to recover real
# active seconds before scoring/idle math. Configurable via AppSettings; default
# 3 matches the current tracker (ACTIVITY_POLL_MS=3000). Set 1 to disable.
_ACTIVE_POLL_PERIOD_CACHE: dict = {}   # keyed by org_id — see _SCREENSHOT_INTERVAL_CACHE


def _get_active_poll_period(db: Session, org_id=None) -> int:
    import time as _t
    now = _t.monotonic()
    cached = _ACTIVE_POLL_PERIOD_CACHE.get(org_id)
    if cached and now <= cached["expires"]:
        return cached["value"]
    val = 3
    try:
        row = (
            db.query(models.AppSettings)
            .filter(models.AppSettings.organization_id == org_id,
                    models.AppSettings.key == "active_time.active_poll_period_seconds",
                    models.AppSettings.team_key.is_(None),   # org-level only
                    models.AppSettings.user_id.is_(None))
            .first()
        )
        if row and row.value:
            n = int(float(str(row.value).strip()))
            if n >= 1:
                val = n
    except Exception:
        pass
    _ACTIVE_POLL_PERIOD_CACHE[org_id] = {"value": val, "expires": now + _SCREENSHOT_INTERVAL_TTL}
    return val


def _real_active_seconds(raw_active_seconds: int, interval_seconds: int, db: Session, org_id=None) -> int:
    """Convert the tracker's active-poll COUNT to real active seconds, clamped to
    the interval. ``raw × poll_period`` capped at the window length."""
    try:
        raw = max(0, int(raw_active_seconds or 0))
    except Exception:
        raw = 0
    interval = max(1, int(interval_seconds or 1))
    return min(interval, raw * _get_active_poll_period(db, org_id))


def _count_window_seconds(payload: ScreenshotPayload, db: Session, org_id=None) -> int:
    """Active window (seconds) used to cap physically-impossible input bursts.

    Key/click counters are presence signals, not text content. Some OS/app
    paths can emit synthetic key events during paste, so we clamp to a
    plausible physical-input rate (per-second cap x window). The window MUST
    match the real length of the interval the counts were accumulated over:

      * v1.0.11+ clients report it directly (active_seconds / interval_seconds)
        -> use that, clamped to COUNT_CAP_MAX_WINDOW_SECONDS. UNCHANGED.
      * v1.0.7 clients report neither and accumulate over the full configured
        screenshot interval (e.g. 1800s). The old `or 30` fallback capped every
        active user at 12x30=360 keys / 10x30=300 clicks. Substitute the
        configured interval so the cap reflects reality.
    """
    explicit = payload.active_seconds or payload.interval_seconds
    if explicit:
        # New clients (v1.0.11+) — behaviour preserved exactly.
        try:
            window = int(explicit)
        except Exception:
            window = 30
        return max(1, min(window, COUNT_CAP_MAX_WINDOW_SECONDS))
    # Legacy client (no tick length reported) — cap against the real configured
    # interval instead of a 30s assumption. The org id is required: without it
    # the lookup filters `organization_id IS NULL`, matches no tenant's row and
    # silently falls back to 30 for everyone.
    interval = _get_configured_screenshot_interval(db, org_id)
    return max(1, min(interval, COUNT_CAP_MAX_LEGACY_WINDOW_SECONDS))


def _cap_interval_count(value: int, per_second_cap: int, window_seconds: int) -> int:
    try:
        raw = int(value or 0)
    except Exception:
        raw = 0
    raw = max(0, raw)
    return min(raw, per_second_cap * window_seconds)


def _compress_to_webp(
    image_bytes: bytes,
    max_w: int = 1600,
    max_h: int = 900,
    quality: int = 30,
) -> bytes:
    """Aggressively compress raw PNG to WebP for monitoring screenshots.

    WebP @ Q30 + 1600x900 cap is intentionally aggressive — these images need
    to convey app/window context, not pixel-perfect detail. A typical 1080p
    PNG (~1-2MB) drops to ~25-50KB. Falls back to original bytes on any
    Pillow failure so we never lose a capture.

    Caller can pass a lower quality (e.g. 20) and smaller dimensions for
    idle screenshots, where readability matters less than storage cost.
    """
    if PILImage is None:
        return image_bytes
    try:
        resample = getattr(PILImage, "Resampling", PILImage).LANCZOS
        img = PILImage.open(io.BytesIO(image_bytes))
        img.thumbnail((max_w, max_h), resample)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        # method=6 = slowest/best compression. Worth it because compression
        # runs in a background task and bucket storage is the binding cost.
        img.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue()
    except Exception as e:
        print(f"[compress] FAILED err={e!r} — uploading original", flush=True)
        return image_bytes


def _upload_to_storage_bg(filename: str, image_bytes: bytes, is_active: bool = True):
    """Compress raw PNG → WebP and upload to Supabase Storage REST API.

    Active vs idle uses different compression presets:
      • Active (user clicking/typing): Q30 @ 1600x900 — readable UI text
      • Idle  (no input):              Q20 @ 1280x720 — just enough to recognise
                                                        the desktop / red-border
                                                        IDLE indicator

    Cache-Control: tells Supabase's CDN and browsers to cache each screenshot
    for 1 year (immutable). Filename embeds a ms timestamp so the same URL
    always points to the same bytes. This is the single biggest lever for
    reducing billable egress — each screenshot is downloaded from Supabase
    once, then served from CDN/browser cache on every subsequent view.
    Expect billable egress to drop 60-80% within a few days of this rolling
    out to enough fresh uploads.
    """
    import os, traceback, requests as _requests

    backend = (os.getenv("STORAGE_BACKEND") or "local").strip().lower()
    print(
        f"[bg-upload] START backend={backend!r} file={filename} "
        f"size={len(image_bytes)} active={is_active}",
        flush=True,
    )
    try:
        if is_active:
            webp_bytes = _compress_to_webp(
                image_bytes, max_w=1600, max_h=900, quality=30
            )
        else:
            webp_bytes = _compress_to_webp(
                image_bytes, max_w=1280, max_h=720, quality=20
            )
        webp_filename = filename  # already .webp at this point

        if backend == "supabase":
            supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
            service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
            bucket = os.getenv("SUPABASE_SCREENSHOTS_BUCKET", "screenshots")
            if not supabase_url or not service_key:
                print(
                    "[bg-upload] MISSING env vars SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY",
                    flush=True,
                )
                return
            url = f"{supabase_url}/storage/v1/object/{bucket}/{webp_filename}"
            headers = {
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "image/webp",
                "x-upsert": "true",
                # ↓ THE EGRESS FIX — tells Supabase CDN + browsers to cache
                # this file for 1 year. Immutable = file bytes never change,
                # so caches can keep it indefinitely without revalidating.
                "cache-control": "public, max-age=31536000, immutable",
            }
            resp = _requests.post(url, data=webp_bytes, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                ratio = (1 - len(webp_bytes) / max(len(image_bytes), 1)) * 100
                print(
                    f"[bg-upload] SUCCESS file={webp_filename} "
                    f"original={len(image_bytes)}B compressed={len(webp_bytes)}B "
                    f"saved={ratio:.1f}% active={is_active} cache=1y status={resp.status_code}",
                    flush=True,
                )
            else:
                print(
                    f"[bg-upload] HTTP ERROR status={resp.status_code} body={resp.text[:300]}",
                    flush=True,
                )
        else:
            # Local fallback: write WebP to disk (org-prefixed subdir may not exist yet)
            local_path = os.path.join(UPLOAD_DIR, webp_filename)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(webp_bytes)
            print(
                f"[bg-upload] LOCAL file={webp_filename} compressed={len(webp_bytes)}B",
                flush=True,
            )
    except Exception as e:
        print(f"[bg-upload] FAILED file={filename} err={e!r}", flush=True)
        print(traceback.format_exc(), flush=True)


# Master kill-switch for screenshot ingestion. Set to False to stop ALL
# screenshots entering the system (skips compression + Supabase upload + DB
# write — the heavy work). Trackers keep posting but get an instant cheap
# reply, so this also relieves instance load. Flip back to True to resume.
# RE-ENABLED 2026-06-20: screenshots turned back on after Tokyo region move
# removed the DB-latency/connection pressure that originally forced the pause.
_SCREENSHOTS_ENABLED = True

# ── Active Time Engine feature flag ───────────────────────────────────────────
# Re-enables ActivityWindow staging inside the screenshot ingest (the engine's
# only ingestion point). Default ON. Set ACTIVE_TIME_ENGINE_ENABLED=false to
# skip ActivityWindow creation ENTIRELY — screenshot upload and the Activity
# insert are unaffected, the dashboard simply falls back to the legacy active-%
# calculation. No second commit, no extra API call: the window rides the SAME
# transaction and the SAME request as the Activity row.
_ACTIVE_TIME_ENGINE_ENABLED = (
    (os.getenv("ACTIVE_TIME_ENGINE_ENABLED", "true") or "true").strip().lower()
    not in ("0", "false", "no", "off")
)


def _stage_active_time_window(db, *, user, session_id, activity, payload, has_image, ts):
    """Stage ONE ActivityWindow into the CURRENT (uncommitted) transaction.

    Contract — best-effort, never fatal:
      * Gated by ACTIVE_TIME_ENGINE_ENABLED (skips cleanly when off).
      * Adds the window to the session but does NOT commit — the caller's single
        db.commit() persists Activity + window together (one transaction).
      * Requires activity.id to be assigned, so the caller flushes first.
      * Any exception (import, scoring, the on-break lookup, row build) is logged
        in full and swallowed. Tracking reliability > Active Time: a scoring
        problem must never lose a screenshot or an Activity row.
    """
    if not _ACTIVE_TIME_ENGINE_ENABLED:
        return
    try:
        from app.active_time import service as _at_service

        interval_for_window = int(
            payload.interval_seconds or _get_configured_screenshot_interval(db, user.organization_id)
        )
        # None (legacy trackers) → treat as active; the engine only degrades when
        # the client explicitly reports the native input hook is down.
        hook_active = True if payload.hook_active is None else bool(payload.hook_active)
        on_break = _at_service._has_open_break(db, user.id, ts)
        _at_service.materialize_window(
            db,
            user_id=user.id,
            session_id=session_id,
            activity_id=activity.id,
            timestamp=ts,
            interval_seconds=interval_for_window,
            key_presses=payload.key_presses or 0,
            mouse_clicks=payload.mouse_clicks or 0,
            mouse_moves=payload.mouse_moves or 0,
            mouse_scrolls=payload.mouse_scrolls or 0,
            # `active_seconds` from the tracker is a count of active idle-polls;
            # scale to real seconds (clamped to the interval) so the focus
            # sub-score reflects true active duration, not poll cadence.
            active_seconds=_real_active_seconds(
                payload.active_seconds or 0, interval_for_window, db, user.organization_id
            ),
            app_name=payload.app_name,
            has_screenshot=bool(has_image),
            hook_active=hook_active,
            on_break=on_break,
            org_id=user.organization_id,
        )
    except Exception:
        import logging
        logging.getLogger("zoiko.active_time").exception(
            "ActivityWindow staging failed for user=%s — screenshot + Activity insert unaffected",
            getattr(user, "id", None),
        )


def _recompute_and_broadcast_active_time(user_id: int, organization_id):
    """Background: recompute the user's authoritative daily Active Time right
    after a window is ingested, then push the new value to the org dashboard
    over the existing WebSocket. Best-effort — runs after the response, opens
    its own DB session, and never affects the upload."""
    if not _ACTIVE_TIME_ENGINE_ENABLED:
        return
    from database import database as _db
    db = _db.SessionLocal()
    try:
        from app.active_time import service as _at_service

        today = _at_service.ist_today()
        daily = _at_service.recompute_daily(db, user_id, today, org_id=organization_id)
        if daily is None or not organization_id:
            return
        from websocket_manager import manager
        from app.active_time.engine import display_active_pct as _display_active_pct
        from app.active_time.config import load_config as _load_cfg

        worked = int(daily.worked_seconds or 0)
        active = int(daily.active_seconds or 0)
        if worked:
            active = min(active, worked)
        # Same individual, never-below-floor display value the dashboard card uses.
        disp = float(_display_active_pct(user_id, str(today), daily.active_pct, _load_cfg(db, organization_id)))
        manager.broadcast_to_org_sync(
            organization_id,
            json.dumps(
                {
                    "type":                 "active_time_update",
                    "organization_id":      organization_id,
                    "user_id":              user_id,
                    "active_pct":           disp,
                    "active_today_seconds": active,
                    "worked_today_seconds": worked,
                    "activity_flags":       daily.flags or None,
                }
            ),
        )
    except Exception:
        import logging
        logging.getLogger("zoiko.active_time").exception(
            "active_time recompute/broadcast failed for user=%s", user_id
        )
    finally:
        db.close()


@router.post("/screenshot")
def upload_screenshot(
    payload: ScreenshotPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # SHADOW ONLY - the CONFIGURATION_REQUIRED probe required by Phase 3.5b.
    # Deliberately the FIRST statement, before the kill-switch below, so the
    # count reflects every capture attempt: that is the denominator for what
    # enforcement would actually block. Phase 3.4 established this decision
    # is currently unsatisfiable (spec layer 6 is unbuilt), so this hook is
    # measuring a permanent block, not a temporary one. It still cannot
    # affect this request.
    _shadow(db, user, "evidence.screenshots.capture",
            route="POST /tracking/screenshot", writable=True)
    # ── Master kill-switch: skip all screenshot work when disabled ──
    if not _SCREENSHOTS_ENABLED:
        return {"status": "screenshots_disabled"}

    # ── 0. Offboarding enforcement gate (§2 — server-side tracking stop) ──
    # Decided from server-side lifecycle state, NOT client logout: a worker
    # whose case has passed access closure (or the final-access cutover) has
    # their telemetry rejected and quarantined here, before any DB write.
    _enforce_tracking_stop(
        db, user, _tracking_constants.TelemetryKind.SCREENSHOT.value,
        meta={
            "app_name": payload.app_name, "window_title": payload.window_title,
            "mouse_clicks": payload.mouse_clicks, "key_presses": payload.key_presses,
            "idle_seconds": payload.idle_seconds, "client_ts": payload.timestamp,
        },
        client_ts=payload.timestamp,
    )

    count_window = _count_window_seconds(payload, db, user.organization_id)
    legacy_window = not (payload.active_seconds or payload.interval_seconds)
    raw_mouse_clicks = max(0, int(payload.mouse_clicks or 0))
    raw_key_presses = max(0, int(payload.key_presses or 0))
    payload.mouse_clicks = _cap_interval_count(
        raw_mouse_clicks, MOUSE_CLICKS_PER_SECOND_CAP, count_window
    )
    payload.key_presses = _cap_interval_count(
        raw_key_presses, KEY_PRESSES_PER_SECOND_CAP, count_window
    )
    counts_were_capped = (
        payload.mouse_clicks != raw_mouse_clicks
        or payload.key_presses != raw_key_presses
    )

    # Count-window telemetry: raw counts received, the window we capped against
    # (and whether it came from the legacy configured-interval path vs a client
    # that reported its own tick length), and the capped counts being stored.
    print(
        f"[activity-cap] user={user.id} legacy_window={legacy_window} "
        f"count_window={count_window}s "
        f"raw_keys={raw_key_presses} stored_keys={payload.key_presses} "
        f"raw_clicks={raw_mouse_clicks} stored_clicks={payload.mouse_clicks} "
        f"capped={counts_were_capped} "
        f"(caps: {KEY_PRESSES_PER_SECOND_CAP}/s keys, {MOUSE_CLICKS_PER_SECOND_CAP}/s clicks)",
        flush=True,
    )

    # ── 1. Active vs idle determination ──
    # The activity % was previously over-reporting "inactive" because is_active
    # only fired on clicks or keystrokes — a worker reading or scrolling for a
    # full screenshot interval registered as 0 clicks + 0 keys → is_active=False,
    # and a continuously-active 30-min session ended up at ~44%.
    #
    # The fix is to fold in ALL the interaction signals the tracker now sends
    # (movement, scroll, OS-measured active-seconds). Any one of them is
    # sufficient evidence the worker is at the keyboard. The minute-level rule
    # from the spec is:
    #   active = mouse activity OR keyboard activity OR workstation activity
    # — which this OR-chain implements directly.
    new_signal_present = (
        (payload.mouse_moves or 0) > 0
        or (payload.mouse_scrolls or 0) > 0
        or (payload.active_seconds or 0) > 0
        or (payload.interval_seconds or 0) > 0
    )
    is_active = (
        (payload.mouse_clicks or 0) > 0
        or (payload.key_presses or 0) > 0
        or (payload.mouse_moves or 0) > 0
        or (payload.mouse_scrolls or 0) > 0
        or (payload.active_seconds or 0) > 0
    )

    # Idle-within-tick (stored in Activity.idle_seconds — see the calculation
    # in /employees/team and /me/day-summary, both of which use
    #   active_in_tick = tick - idle_in_tick
    # so this column should carry idle seconds WITHIN the tick, not the raw
    # cumulative OS idle clock.
    #
    # New trackers send `active_seconds` (OS active within the interval) and
    # `interval_seconds` (the actual tick length) — we can compute the exact
    # idle-within-tick from those. Legacy trackers don't send either; we keep
    # the historical behavior (store the raw OS idle and let the calculator
    # clamp it to the tick) so older builds in the field aren't penalised.
    if new_signal_present:
        interval = max(1, payload.interval_seconds or 30)
        # `active_seconds` is a count of active idle-polls — scale to real seconds.
        active_in_tick = max(0, _real_active_seconds(payload.active_seconds or 0, interval, db))
        # If we have explicit input evidence but the OS reported 0 active-secs
        # (e.g. uiohook fired but the powerMonitor poll missed the burst),
        # assume the whole tick was active rather than the whole tick was idle.
        if active_in_tick == 0 and (
            (payload.mouse_clicks or 0) > 0
            or (payload.key_presses or 0) > 0
            or (payload.mouse_moves or 0) > 0
            or (payload.mouse_scrolls or 0) > 0
        ):
            active_in_tick = interval
        stored_idle_seconds = max(0, interval - active_in_tick)
    else:
        stored_idle_seconds = payload.idle_seconds or 0

    # Detailed activity-component log so we can see exactly why a given tick
    # was classified the way it was. Useful when an admin reports "this
    # employee is showing 40% but they were working" — grep the logs for
    # their user_id and you can see every component.
    print(
        f"[activity] user={user.id} app={payload.app_name!r} "
        f"clicks={payload.mouse_clicks} moves={payload.mouse_moves} "
        f"scrolls={payload.mouse_scrolls} keys={payload.key_presses} "
        f"active_sec={payload.active_seconds}/{payload.interval_seconds} "
        f"raw_idle={payload.idle_seconds} stored_idle={stored_idle_seconds} "
        f"is_active={is_active} capped={counts_were_capped} "
        f"raw_clicks={raw_mouse_clicks} raw_keys={raw_key_presses}",
        flush=True,
    )

    # ── 2. Decode base64 → raw bytes (in memory only, never persisted) ──
    image_bytes: bytes = b""
    file_url: str = ""
    filename: str = ""
    skip_reason: str = ""  # for logging only
    has_image = bool(payload.image and len(payload.image) > 10)

    if has_image:
        try:
            image_bytes = base64.b64decode(payload.image)
        except Exception:
            has_image = False
            image_bytes = b""

    if has_image and len(image_bytes) < 100:
        # Too small to be a real image — treat as no capture
        has_image = False
        image_bytes = b""

   
    upload_image = False
    if has_image:
        img_hash = hashlib.sha256(image_bytes).hexdigest()
        if _dedup_check(user.id, img_hash):
            skip_reason = "dedup"
        else:
            upload_image = True
            _dedup_set(user.id, img_hash)

    if upload_image:
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        org_prefix = f"org_{user.organization_id}/" if user.organization_id else ""
        filename = f"{org_prefix}{user.id}_{ts_ms}.webp"
        file_url = f"/uploads/{filename}"
        # Pass is_active so idle frames get the harder compression preset
        background_tasks.add_task(
            _upload_to_storage_bg, filename, image_bytes, is_active
        )
    elif skip_reason:
        print(
            f"[screenshot] user={user.id} skipped upload reason={skip_reason} "
            f"size={len(image_bytes)}B active={is_active}",
            flush=True, 
        )

    # ── 4. Parse timestamp ──
    try:
        ts = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
    except Exception:
        ts = datetime.now(timezone.utc)

    # ── 5. Productivity score ──
    # Folds in every input signal — clicks, keys, movement, scroll — so a
    # reading-heavy interval (lots of scrolling, no typing) still scores
    # above zero. The denominator floor (300) keeps the score bounded for
    # short bursts.
    prod_score = 0.0
    activity_proxy = (
        (payload.mouse_clicks or 0) * 2
        + (payload.key_presses or 0) * 1
        + (payload.mouse_moves or 0) * 1
        + (payload.mouse_scrolls or 0) * 1
    )
    if activity_proxy > 0:
        prod_score = round(min(1.0, activity_proxy / max(300, activity_proxy)), 4)

    # ── 6. Find active clock session — reject if not clocked in ──
    session = (
        db.query(models.ClockSession)
        .filter(
            and_(
                models.ClockSession.user_id == user.id,
                models.ClockSession.clock_out_time == None,
            )
        )
        .first()
    )
    if not session:
        raise HTTPException(
            status_code=403, detail="Not clocked in — screenshot rejected"
        )
    session_id = session.id

    # ── 7. Activity record ──
    # NOTE: `thumbnail` column now stores the SAME relative URL as `file_path`
    # (e.g. "/uploads/xyz.webp"). No more base64 blobs in the DB. The screenshots
    # router resolves this path to a short-lived Supabase signed URL on read.
    # When upload was skipped (dedup), both fields are NULL and the frontend
    # falls back to a placeholder camera icon.
    activity = models.Activity(
        user_id=user.id,
        organization_id=user.organization_id,
        session_id=session_id,
        timestamp=ts,
        app_name=payload.app_name,
        window_title=payload.window_title,
        file_path=file_url or None,
        mouse_clicks=payload.mouse_clicks,
        key_presses=payload.key_presses,
        # Store tick-relative idle (NOT raw cumulative OS idle). The
        # downstream active-seconds calculator does
        #   active_in_tick = tick - idle_in_tick
        # and expects idle_seconds to already be bounded by the tick.
        idle_seconds=stored_idle_seconds,
        # Persist the tracker's real per-tick length (NULL for legacy clients
        # that don't report it). The Active Time Engine + aggregators use this
        # instead of inferring the tick from the gap to the next row.
        interval_seconds=(payload.interval_seconds or None),
        productivity_score=prod_score,
        is_active=is_active,
        thumbnail=file_url or None,
    )
    db.add(activity)

    # ── 8. Persist Activity + Active Time window in ONE transaction ──────────────
    # The ActivityWindow is staged into the SAME session and committed by the
    # SINGLE db.commit() below — there is NO second commit and NO extra request.
    # db.flush() assigns activity.id (the window's FK target) without ending the
    # transaction; _stage_active_time_window then adds the window row. The whole
    # pair commits atomically.
    #
    # Reliability ordering — tracking > Active Time:
    #   * deadlock / serialization failure → retry the whole pair (unchanged
    #     behaviour from before; the window just rides along).
    #   * ANY other commit/staging failure → drop the window and retry the
    #     Activity row ALONE, so a scoring/window problem can never lose a
    #     screenshot or an Activity insert.
    import time
    from sqlalchemy.exc import OperationalError

    _committed = False
    _stage_window = _ACTIVE_TIME_ENGINE_ENABLED
    for _attempt in range(3):
        try:
            db.flush()  # assign activity.id within the OPEN transaction (no commit)
            if _stage_window:
                _stage_active_time_window(
                    db, user=user, session_id=session_id, activity=activity,
                    payload=payload, has_image=has_image, ts=ts,
                )
            db.commit()  # ONE commit — Activity (+ ActivityWindow) together
            _committed = True
            break
        except OperationalError as _e:
            db.rollback()
            msg = str(_e).lower()
            if "deadlock" in msg or "could not serialize" in msg:
                time.sleep(0.05 * (2**_attempt))  # 50ms, 100ms, 200ms
                # Re-add the primary row so the retry re-stages + re-commits.
                db.add(activity)
                continue
            raise
        except Exception:
            # Non-transient failure. If Active Time staging could be the cause,
            # drop it and retry committing the Activity row alone — the
            # screenshot/Activity must survive even when scoring is broken.
            db.rollback()
            if _stage_window:
                import logging
                logging.getLogger("zoiko.active_time").exception(
                    "Active Time window commit failed — retrying Activity insert alone"
                )
                _stage_window = False
                db.add(activity)
                continue
            raise
    if not _committed:
        raise HTTPException(status_code=503, detail="Database busy, please retry")
    db.refresh(activity)
    _activity_id = activity.id  # save before closing the read transaction
    db.rollback()               # explicitly close the implicit read transaction opened by refresh

    # ── 10. WebSocket broadcast (real-time dashboard, org-scoped) ──
    try:
        from websocket_manager import manager

        if user.organization_id:
            manager.broadcast_to_org_sync(
                user.organization_id,
                json.dumps(
                    {
                        "type":            "new_screenshot",
                        "organization_id": user.organization_id,
                        "activity_id":     _activity_id,
                        "user_id":         user.id,
                        "app_name":        payload.app_name,
                        "is_active":       is_active,
                        "timestamp":       payload.timestamp,
                        "has_image":       bool(file_url),
                    }
                ),
            )
            if payload.idle_seconds and payload.idle_seconds >= 900:  # 15 minutes idle
                manager.broadcast_to_org_sync(
                    user.organization_id,
                    json.dumps(
                        {
                            "type":            "idle_alert",
                            "organization_id": user.organization_id,
                            "user_id":         user.id,
                            "user_name":       user.name,
                            "idle_seconds":    payload.idle_seconds,
                            "app_name":        payload.app_name,
                            "timestamp":       payload.timestamp,
                        }
                    ),
                )
    except Exception:
        pass  # WebSocket broadcast is non-critical

    # ── 11. Recompute authoritative Active Time + push it (background, non-blocking) ──
    # Runs AFTER the response is sent, so the upload stays a cheap single insert.
    # This is what makes the dashboard's Active Time move per uploaded window.
    if _committed and _ACTIVE_TIME_ENGINE_ENABLED:
        try:
            background_tasks.add_task(
                _recompute_and_broadcast_active_time, user.id, user.organization_id
            )
        except Exception:
            pass

    return {
        "success": True,
        "id": activity.id,
        "is_active": is_active,
        "user_id": user.id,
        "uploaded": bool(file_url),
        "skip_reason": skip_reason or None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /tracking/cleanup/old-screenshots
# Deletes screenshots older than RETENTION_DAYS from the Supabase bucket AND
# nulls out their file_path / thumbnail fields in the activities table.
# The activity ROW itself is preserved (so timeline, stats, timesheet remain
# accurate) — only the image is removed. Frontend handles missing images by
# falling back to a camera placeholder.
#
# Safe to call repeatedly — only deletes what's past the retention window.
# Intended to be hit by a daily scheduled task (Cloud Scheduler / cron).
# Admin-only.
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/cleanup/old-screenshots")
def cleanup_old_screenshots(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Delete screenshots older than RETENTION_DAYS. Admin-only.

    Returns a summary of what was deleted.
    """
    # Admin-only guard
    if getattr(user, "role", None) not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin only")

    import requests as _requests

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    # Find activity rows with file_path older than cutoff, scoped to caller's org
    old_activities = (
        db.query(models.Activity)
        .filter(
            models.Activity.organization_id == user.organization_id,
            models.Activity.timestamp < cutoff,
            models.Activity.file_path.isnot(None),
        )
        .all()
    )

    print(
        f"[cleanup] Found {len(old_activities)} screenshots older than "
        f"{RETENTION_DAYS} days (cutoff={cutoff.isoformat()})",
        flush=True,
    )

    if not old_activities:
        return {
            "success": True,
            "retention_days": RETENTION_DAYS,
            "cutoff": cutoff.isoformat(),
            "deleted_count": 0,
            "errors": 0,
            "message": "Nothing to clean up",
        }

    backend = (os.getenv("STORAGE_BACKEND") or "local").strip().lower()
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    bucket = os.getenv("SUPABASE_SCREENSHOTS_BUCKET", "screenshots")

    deleted = 0
    errors = 0
    batch_size = 100

    # Prepare list of (activity, filename_in_bucket) tuples
    # file_path looks like "/uploads/80_1776860076463.webp"
    # bucket filename is just "80_1776860076463.webp"
    to_delete = []
    for act in old_activities:
        fname = act.file_path.replace("/uploads/", "") if act.file_path else None
        if fname:
            to_delete.append((act, fname))

    # Delete from Supabase bucket in batches
    if backend == "supabase" and supabase_url and service_key:
        for i in range(0, len(to_delete), batch_size):
            batch = to_delete[i : i + batch_size]
            filenames = [fname for _, fname in batch]
            try:
                resp = _requests.delete(
                    f"{supabase_url}/storage/v1/object/{bucket}",
                    headers={
                        "Authorization": f"Bearer {service_key}",
                        "Content-Type": "application/json",
                    },
                    json={"prefixes": filenames},
                    timeout=30,
                )
                if resp.status_code in (200, 204):
                    deleted += len(batch)
                    print(
                        f"[cleanup] deleted batch of {len(batch)} files from bucket",
                        flush=True,
                    )
                else:
                    errors += len(batch)
                    print(
                        f"[cleanup] bucket delete FAILED status={resp.status_code} "
                        f"body={resp.text[:300]}",
                        flush=True,
                    )
            except Exception as e:
                errors += len(batch)
                print(f"[cleanup] bucket delete exception: {e!r}", flush=True)
    else:
        # Local fallback: delete files from disk
        for act, fname in to_delete:
            try:
                local_path = os.path.join(UPLOAD_DIR, fname)
                if os.path.exists(local_path):
                    os.remove(local_path)
                deleted += 1
            except Exception as e:
                errors += 1
                print(f"[cleanup] local delete failed {fname}: {e!r}", flush=True)

    # NULL out the file_path / thumbnail fields so the activity row stays
    # in the DB (preserving timeline, stats, timesheet accuracy) but admins
    # know there's no image to load. Batch this in a single UPDATE for speed.
    act_ids = [act.id for act, _ in to_delete]
    if act_ids:
        db.query(models.Activity).filter(models.Activity.id.in_(act_ids)).update(
            {"file_path": None, "thumbnail": None},
            synchronize_session=False,
        )
        db.commit()

    print(
        f"[cleanup] DONE deleted={deleted} errors={errors} "
        f"retention_days={RETENTION_DAYS}",
        flush=True,
    )

    return {
        "success": True,
        "retention_days": RETENTION_DAYS,
        "cutoff": cutoff.isoformat(),
        "deleted_count": deleted,
        "errors": errors,
        "message": f"Deleted {deleted} screenshots older than {RETENTION_DAYS} days",
    }
