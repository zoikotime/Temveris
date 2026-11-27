"""
Temveris – Break Management Router
=====================================
Handles employee break start/resume with reason capture.

Endpoints:
  POST /break/start    – start a break (records break_start time)
  POST /break/resume   – end a break + save reason (shows popup on frontend)
  GET  /break/today    – all breaks for the user today
  GET  /break/active   – check if user is currently on break
  GET  /break/sessions – all clock sessions + breaks for an employee (admin)
  GET  /break/thresholds – workspace-configured overtime thresholds (workers + admins)
  PUT  /break/thresholds – admin updates the overtime thresholds
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timezone, timedelta

from database.database import get_db
from database import models
from app.auth.service import get_current_user
from app.breaks import governance as break_governance
from app.attendance_desktop_policy import enforce_desktop_attendance

router = APIRouter(prefix="/break", tags=["Break Management"])


def _device_info(request) -> dict:
    if request is None:
        return {}
    try:
        return {
            "deviceId":   request.headers.get("x-device-id") or request.headers.get("x-device"),
            "ip_address": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }
    except Exception:
        return {}

# ── Threshold config (workspace-level, stored in AppSettings) ─────────────────
# Both values are in MINUTES. Defaults match the product spec:
#   • short break ("break"): warn at 15 minutes
#   • lunch break ("lunch"): warn at 60 minutes
# Admins can tune these via PUT /break/thresholds; workers read them via
# GET /break/thresholds so the popup fires at the workspace-configured limit
# rather than a hard-coded one.
_BREAK_THRESHOLD_KEY = "break.short_minutes"
_LUNCH_THRESHOLD_KEY = "break.lunch_minutes"
_DEFAULT_BREAK_MIN = 15
_DEFAULT_LUNCH_MIN = 60


def _get_threshold(db: Session, key: str, default: int, org_id=None) -> int:
    """Read an integer threshold from `app_settings`, seeding the default if
    the row does not exist yet. Wrapped in try/except so a missing table on
    fresh deploys does not break the break endpoints — we fall back to the
    spec defaults instead."""
    try:
        row = db.query(models.AppSettings).filter(
            models.AppSettings.organization_id == org_id,
            models.AppSettings.key == key,
            models.AppSettings.team_key.is_(None),   # org level only
            models.AppSettings.user_id.is_(None),
        ).first()
        if row and row.value is not None:
            try:
                return max(1, int(str(row.value).strip()))
            except (TypeError, ValueError):
                return default
        # Seed default so the admin UI has something to edit.
        row = models.AppSettings(organization_id=org_id, key=key, value=str(default))
        db.add(row)
        db.commit()
        return default
    except Exception:
        try: db.rollback()
        except Exception: pass
        return default


def _set_threshold(db: Session, key: str, minutes: int, org_id=None) -> int:
    # Org tier only — without this the upsert could overwrite a team/employee
    # override row with the org-level value instead of creating the org row.
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == key,
        models.AppSettings.team_key.is_(None),
        models.AppSettings.user_id.is_(None),
    ).first()
    if row:
        row.value = str(minutes)
    else:
        row = models.AppSettings(organization_id=org_id, key=key, value=str(minutes))
        db.add(row)
    db.commit()
    return minutes


def _assert_user_in_org(db: Session, target_user_id: int, viewer: models.User) -> None:
    if target_user_id == viewer.id:
        return
    ok = db.query(models.User).filter(
        models.User.id == target_user_id,
        models.User.organization_id == viewer.organization_id,
    ).first()
    if not ok:
        raise HTTPException(404, "User not found")


def _active_session(user_id: int, db: Session) -> Optional[models.ClockSession]:
    return db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user_id,
            models.ClockSession.clock_out_time == None,
        )
    ).first()


def _active_break(user_id: int, db: Session) -> Optional[models.BreakSession]:
    return db.query(models.BreakSession).filter(
        and_(
            models.BreakSession.user_id   == user_id,
            models.BreakSession.break_end == None,
            models.BreakSession.is_active == True,
        )
    ).first()


# ── POST /break/start ────────────────────────────────────────────────────────
class StartBreakPayload(BaseModel):
    # "break" or "lunch". Worker dashboard sends this so the overtime
    # threshold can differ per break type (short break 15m vs lunch 60m).
    break_type: Literal["break", "lunch"] = "break"


@router.post("/start")
def start_break(
    body: Optional[StartBreakPayload] = None,
    request: Request = None,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Attendance actions are desktop-app only — see WEB_ATTENDANCE_DISABLED.
    # /break/* is a second, parallel break API; without this guard the rule on
    # /employees/start-break would be trivially bypassable from a browser.
    enforce_desktop_attendance(user, request, db)

    btype = (body.break_type if body else "break") or "break"
    if btype not in ("break", "lunch"):
        btype = "break"

    device_info = _device_info(request)
    session      = _active_session(user.id, db)
    active_break = _active_break(user.id, db)
    worker_type  = break_governance.normalize_worker_type(user)
    allowed_min  = break_governance.allowance_minutes(db, btype, user.organization_id)

    # Provenance: record the request before evaluating it.
    break_governance.record_break_event(
        db, user=user, event_type="BREAK_START_REQUESTED",
        session_id=session.id if session else None,
        worker_type=worker_type, break_type=btype, allowed_minutes=allowed_min,
        device_info=device_info, commit=True,
    )

    # Server-side entitlement enforcement (BR-01 / BR-02 / BR-03 + workday limits).
    elig = break_governance.evaluate_eligibility(
        db, user, btype, active_session=session, active_break=active_break, _looked_up=True,
    )
    if not elig.allowed:
        break_governance.record_break_event(
            db, user=user, event_type="BREAK_DENIED",
            session_id=session.id if session else None,
            worker_type=worker_type, break_type=btype, allowed_minutes=allowed_min,
            eligibility_result="denied", denial_reason=elig.code,
            device_info=device_info, extra={"message": elig.message}, commit=True,
        )
        return {
            "success":     False,
            "allowed":     False,
            "code":        elig.code,
            "message":     elig.message,
            "break_type":  btype,
            "worker_type": worker_type,
        }

    now = datetime.now(timezone.utc)
    brk = models.BreakSession(
        user_id         = user.id,
        session_id      = session.id,
        organization_id = user.organization_id,
        break_start     = now,
        is_active       = True,
        break_type      = btype,
        overtime_notified = False,
        worker_type        = worker_type,
        allowed_minutes    = allowed_min,
        status             = "active",
        overrun_flag       = False,
        overrun_notification_sent = False,
        alert_count        = 0,
    )
    db.add(brk)
    db.commit()
    db.refresh(brk)

    break_governance.record_break_event(
        db, user=user, event_type="BREAK_STARTED",
        session_id=session.id, break_id=brk.id,
        worker_type=worker_type, break_type=btype, allowed_minutes=allowed_min,
        eligibility_result="allowed", device_info=device_info, commit=True,
    )

    # Invalidate tracking status cache so next /tracking/ping gets on_break=True immediately
    try:
        from app.tracking.router import _cache_invalidate
        _cache_invalidate(user.id)
    except Exception:
        pass

    # Invalidate team cache so the admin dashboard reflects "On Break" on the next poll
    try:
        from app.employees.router import _team_cache_invalidate_all
        _team_cache_invalidate_all()
    except Exception:
        pass

    try:
        from websocket_manager import manager
        import json
        if user.organization_id:
            manager.broadcast_to_org_sync(user.organization_id, json.dumps({
                "type":            "break_started",
                "organization_id": user.organization_id,
                "user_id":         user.id,
                "break_id":        brk.id,
                "break_type":      btype,
                "timestamp":       now.isoformat(),
            }))
    except Exception:
        pass

    threshold_min = _get_threshold(
        db,
        _LUNCH_THRESHOLD_KEY if btype == "lunch" else _BREAK_THRESHOLD_KEY,
        _DEFAULT_LUNCH_MIN if btype == "lunch" else _DEFAULT_BREAK_MIN,
        org_id=user.organization_id,
    )
    return {
        "success":    True,
        "allowed":    True,
        "break_id":   brk.id,
        "break_start": now.isoformat(),
        "break_type": btype,
        "worker_type": worker_type,
        "allowed_minutes": allowed_min,
        "threshold_minutes": threshold_min,
        "message":    "Break started",
    }


# ── POST /break/resume ───────────────────────────────────────────────────────
class ResumePayload(BaseModel):
    reason: str                       # mandatory – shown in popup
    description: Optional[str] = ""  # extra notes

@router.post("/resume")
def resume_from_break(
    body: ResumePayload,
    request: Request = None,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Attendance actions are desktop-app only — see WEB_ATTENDANCE_DISABLED.
    enforce_desktop_attendance(user, request, db)

    if not body.reason or not body.reason.strip():
        raise HTTPException(400, "Break reason is required")

    brk = _active_break(user.id, db)
    if not brk:
        raise HTTPException(400, "No active break found")

    now           = datetime.now(timezone.utc)
    break_secs    = int((now - brk.break_start.replace(tzinfo=timezone.utc)).total_seconds())

    brk.break_end     = now
    brk.break_seconds = break_secs
    brk.is_active     = False
    brk.status        = "ended"
    brk.reason        = body.reason.strip()

    # Finalize overrun provenance (the break may have crossed its allowance).
    allowed_min     = brk.allowed_minutes or break_governance.allowance_minutes(db, brk.break_type or "break", user.organization_id)
    overrun_seconds = max(0, break_secs - allowed_min * 60)
    if overrun_seconds > 0:
        brk.overrun_flag = True
        if brk.allowance_reached_at is None:
            brk.allowance_reached_at = brk.break_start.replace(tzinfo=timezone.utc) + timedelta(minutes=allowed_min)

    # Add break seconds to the parent clock session
    session = db.query(models.ClockSession).filter(
        models.ClockSession.id == brk.session_id
    ).first()
    if session:
        session.break_seconds = (session.break_seconds or 0) + break_secs

    db.commit()

    break_governance.record_break_event(
        db, user=user, event_type="BREAK_ENDED",
        session_id=brk.session_id, break_id=brk.id,
        worker_type=brk.worker_type, break_type=brk.break_type,
        allowed_minutes=allowed_min, device_info=_device_info(request),
        extra={
            "actualDurationSeconds": break_secs,
            "actualDurationMinutes": round(break_secs / 60, 2),
            "overrunMinutes":        round(overrun_seconds / 60, 2),
            "overrunFlag":           bool(overrun_seconds > 0),
            "reason":                body.reason.strip(),
        },
        commit=True,
    )

    # Invalidate tracking status cache so next /tracking/ping gets on_break=False immediately
    try:
        from app.tracking.router import _cache_invalidate
        _cache_invalidate(user.id)
    except Exception:
        pass

    # Invalidate team cache so the admin dashboard reflects "Working" on the next poll
    try:
        from app.employees.router import _team_cache_invalidate_all
        _team_cache_invalidate_all()
    except Exception:
        pass

    try:
        from websocket_manager import manager
        import json
        if user.organization_id:
            manager.broadcast_to_org_sync(user.organization_id, json.dumps({
                "type":            "break_ended",
                "organization_id": user.organization_id,
                "user_id":         user.id,
                "break_id":        brk.id,
                "break_seconds":   break_secs,
                "reason":          body.reason.strip(),
                "timestamp":       now.isoformat(),
            }))
    except Exception:
        pass

    mins = break_secs // 60
    secs = break_secs % 60
    return {
        "success":      True,
        "break_id":     brk.id,
        "break_seconds": break_secs,
        "duration":     f"{mins}m {secs}s",
        "reason":       body.reason.strip(),
        "message":      f"Resumed after {mins}m {secs}s break",
    }


# ── GET /break/active ────────────────────────────────────────────────────────
@router.get("/active")
def get_active_break(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    brk = _active_break(user.id, db)
    if not brk:
        return {"on_break": False}

    btype = getattr(brk, "break_type", None) or "break"
    threshold_min = _get_threshold(
        db,
        _LUNCH_THRESHOLD_KEY if btype == "lunch" else _BREAK_THRESHOLD_KEY,
        _DEFAULT_LUNCH_MIN if btype == "lunch" else _DEFAULT_BREAK_MIN,
        org_id=user.organization_id,
    )
    grace_min = break_governance.grace_minutes(db, btype, user.organization_id)
    elapsed = int((datetime.now(timezone.utc) - brk.break_start.replace(tzinfo=timezone.utc)).total_seconds())
    is_overtime = elapsed >= threshold_min * 60
    notification_due = elapsed >= (threshold_min + grace_min) * 60

    # First time we observe overtime for this break, mark it and broadcast a
    # websocket alert so admins see the indicator without waiting for the
    # next /employees/team poll cycle.
    just_crossed = False
    if notification_due and not getattr(brk, "overtime_notified", False):
        brk.overtime_notified = True
        try:
            db.commit()
            just_crossed = True
        except Exception:
            db.rollback()
        if just_crossed:
            try:
                from websocket_manager import manager
                import json
                if user.organization_id:
                    manager.broadcast_to_org_sync(user.organization_id, json.dumps({
                        "type":              "break_overtime",
                        "organization_id":   user.organization_id,
                        "user_id":           user.id,
                        "user_name":         user.name,
                        "break_id":          brk.id,
                        "break_type":        btype,
                        "elapsed_seconds":   elapsed,
                        "threshold_minutes": threshold_min,
                        "overtime_seconds":  max(0, elapsed - threshold_min * 60),
                        "notification_delay_minutes": grace_min,
                        "timestamp":         datetime.now(timezone.utc).isoformat(),
                    }))
            except Exception:
                pass

    return {
        "on_break":         True,
        "break_id":         brk.id,
        "break_type":       btype,
        "break_start":      brk.break_start.isoformat(),
        "elapsed_seconds":  elapsed,
        "threshold_minutes": threshold_min,
        "threshold_seconds": threshold_min * 60,
        "allowed_minutes":  threshold_min,
        "allowed_seconds":  threshold_min * 60,
        "is_overtime":      is_overtime,
        "exceeded":         is_overtime,
        "exceeded_seconds": max(0, elapsed - threshold_min * 60),
        "overtime_seconds": max(0, elapsed - threshold_min * 60),
        "notification_due": notification_due,
        "notification_delay_minutes": grace_min,
        "notification_due_seconds": (threshold_min + grace_min) * 60,
        "notification_pending_seconds": max(0, (threshold_min + grace_min) * 60 - elapsed),
        "overtime_just_crossed": just_crossed,
        # Spec UI strings + dashboard indicator, returned so every surface
        # renders identical copy ("On Short Break (Exceeded)" etc.).
        "status":           "overdue" if is_overtime else "active",
        "status_chip":      break_governance.status_chip(btype, is_overtime),
        "banner":           break_governance.overrun_banner(btype) if is_overtime else None,
        "indicator":        break_governance.governance_state(elapsed, threshold_min),
        "worker_type":      break_governance.normalize_worker_type(user),
    }


# ── GET /break/thresholds ────────────────────────────────────────────────────
# Workers + admins both read this so the popup fires at the workspace-
# configured limit. No auth role gate; the values are non-sensitive config.
@router.get("/thresholds")
def get_thresholds(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    return {
        "short_minutes": _get_threshold(db, _BREAK_THRESHOLD_KEY, _DEFAULT_BREAK_MIN, org_id=user.organization_id),
        "lunch_minutes": _get_threshold(db, _LUNCH_THRESHOLD_KEY, _DEFAULT_LUNCH_MIN, org_id=user.organization_id),
        # Grace = minutes past the allowance before the FIRST HR overrun alert
        # fires (effective_limit = allowance + grace). Defaults 5 each.
        "short_break_grace_minutes": break_governance.grace_minutes(db, break_governance.SHORT_BREAK_TYPE, user.organization_id),
        "lunch_break_grace_minutes": break_governance.grace_minutes(db, break_governance.LUNCH_BREAK_TYPE, user.organization_id),
        # Continuous work before the "take a break" nudge. Was hard-coded in
        # the browser as 4 hours for every tenant.
        "deep_work_minutes": _get_threshold(
            db, break_governance.DEEP_WORK_KEY,
            break_governance.DEFAULT_DEEP_WORK_MINUTES,
            org_id=user.organization_id),
    }


# ── PUT /break/thresholds ────────────────────────────────────────────────────
class ThresholdsPayload(BaseModel):
    short_minutes: Optional[int] = Field(default=None, ge=1, le=240)
    lunch_minutes: Optional[int] = Field(default=None, ge=1, le=480)
    short_break_grace_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    lunch_break_grace_minutes: Optional[int] = Field(default=None, ge=0, le=240)


@router.put("/thresholds")
def update_thresholds(
    body: ThresholdsPayload,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Admin/HR-only: thresholds are workspace policy, not per-worker.
    if (user.role or "").lower() not in ("admin", "hr", "owner"):
        raise HTTPException(403, "Admin/HR only")
    org_id = user.organization_id
    if body.short_minutes is not None:
        _set_threshold(db, _BREAK_THRESHOLD_KEY, int(body.short_minutes), org_id=org_id)
    if body.lunch_minutes is not None:
        _set_threshold(db, _LUNCH_THRESHOLD_KEY, int(body.lunch_minutes), org_id=org_id)
    if body.short_break_grace_minutes is not None:
        _set_threshold(db, break_governance.SHORT_GRACE_KEY, int(body.short_break_grace_minutes))
    if body.lunch_break_grace_minutes is not None:
        _set_threshold(db, break_governance.LUNCH_GRACE_KEY, int(body.lunch_break_grace_minutes))
    return {
        "short_minutes": _get_threshold(db, _BREAK_THRESHOLD_KEY, _DEFAULT_BREAK_MIN, org_id=org_id),
        "lunch_minutes": _get_threshold(db, _LUNCH_THRESHOLD_KEY, _DEFAULT_LUNCH_MIN, org_id=org_id),
        "short_break_grace_minutes": break_governance.grace_minutes(db, break_governance.SHORT_BREAK_TYPE, user.organization_id),
        "lunch_break_grace_minutes": break_governance.grace_minutes(db, break_governance.LUNCH_BREAK_TYPE, user.organization_id),
    }


# ── GET /break/today ─────────────────────────────────────────────────────────
@router.get("/today")
def get_today_breaks(
    user_id: Optional[int] = None,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Admins may pass ?user_id=X to view another employee's breaks.
    target_id = user_id if (user_id and user.role in ("admin", "manager")) else user.id
    _assert_user_in_org(db, target_id, user)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    breaks = db.query(models.BreakSession).filter(
        and_(
            models.BreakSession.user_id    == target_id,
            models.BreakSession.break_start >= today_start,
        )
    ).order_by(models.BreakSession.break_start.asc()).all()

    total_secs = sum(b.break_seconds or 0 for b in breaks if b.break_end)
    return {
        "breaks": [
            {
                "id":            b.id,
                "break_start":   b.break_start.isoformat(),
                "break_end":     b.break_end.isoformat() if b.break_end else None,
                "break_seconds": b.break_seconds,
                "is_active":     b.is_active,
                "reason":        b.reason,
            }
            for b in breaks
        ],
        "total_break_seconds": total_secs,
        "total_break_display": f"{total_secs // 3600}h {(total_secs % 3600) // 60}m",
    }


# ── GET /break/sessions ───────────────────────────────────────────────────────
# Returns every ClockSession for the target employee (most recent first, up to
# `days` calendar days back) together with all BreakSessions for each session.
# Admins and managers can pass ?user_id=X; employees see only their own data.
@router.get("/sessions")
def get_all_sessions(
    user_id: Optional[int] = None,
    days: int = 90,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    target_id = user_id if (user_id and user.role in ("admin", "manager")) else user.id
    _assert_user_in_org(db, target_id, user)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id         == target_id,
            models.ClockSession.organization_id == user.organization_id,
            models.ClockSession.clock_in_time   >= cutoff,
        )
        .order_by(models.ClockSession.clock_in_time.desc())
        .all()
    )

    # Fetch all breaks for these sessions in one query
    session_ids = [s.id for s in sessions]
    all_breaks = []
    if session_ids:
        all_breaks = (
            db.query(models.BreakSession)
            .filter(models.BreakSession.session_id.in_(session_ids))
            .order_by(models.BreakSession.break_start.asc())
            .all()
        )

    # Also fetch orphan breaks (session_id is null) within the cutoff window
    orphan_breaks = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.user_id     == target_id,
            models.BreakSession.session_id  == None,
            models.BreakSession.break_start >= cutoff,
        )
        .order_by(models.BreakSession.break_start.asc())
        .all()
    )
    all_breaks_combined = all_breaks + orphan_breaks

    # Index breaks by session_id (None → orphan bucket keyed by "orphan")
    from collections import defaultdict
    breaks_by_session = defaultdict(list)
    for b in all_breaks_combined:
        breaks_by_session[b.session_id or "orphan"].append(b)

    def fmt_break(b):
        allowed_min = b.allowed_minutes or break_governance.allowance_minutes(db, b.break_type or "break", user.organization_id)
        actual_secs = b.break_seconds or 0
        overrun_secs = max(0, actual_secs - allowed_min * 60)
        return {
            "id":            b.id,
            "break_start":   b.break_start.isoformat(),
            "break_end":     b.break_end.isoformat() if b.break_end else None,
            "break_seconds": b.break_seconds,
            "reason":        b.reason,
            "is_active":     b.is_active,
            # ── Governance / reporting fields ──
            "break_type":                b.break_type or "break",
            "worker_type":               b.worker_type,
            "status":                    b.status or ("active" if b.is_active else "ended"),
            "expected_duration_minutes": allowed_min,
            "actual_duration_minutes":   round(actual_secs / 60, 2),
            "overrun_minutes":           round(overrun_secs / 60, 2),
            "overrun_flag":              bool(b.overrun_flag or overrun_secs > 0),
            "overrun_notification_sent": bool(b.overrun_notification_sent),
        }

    # Connectivity gaps deducted from these sessions. Returned alongside breaks
    # so the Clock Summary can render them as their own rows — otherwise a
    # session whose time was reduced by an offline window shows one unbroken
    # "Worked for <full span>" row and the deduction is invisible.
    # Degrades to "no offline rows" rather than 500ing the whole Clock Summary if
    # the table is absent (fresh deploy before the startup ensure has run). The
    # panel is a read-only view — losing the offline annotation is far better
    # than losing the admin's entire attendance history.
    offline_by_session = defaultdict(list)
    if session_ids:
        try:
            for oi in (
                db.query(models.OfflineInterval)
                .filter(models.OfflineInterval.session_id.in_(session_ids))
                .order_by(models.OfflineInterval.offline_start.asc())
                .all()
            ):
                offline_by_session[oi.session_id].append(oi)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    def fmt_offline(oi):
        return {
            "id":              oi.id,
            "offline_start":   oi.offline_start.isoformat(),
            "offline_end":     oi.offline_end.isoformat(),
            "applied_seconds": oi.applied_seconds or 0,
            "claimed_seconds": oi.claimed_seconds or 0,
            "capped":          bool(oi.capped),
            "cap_reason":      oi.cap_reason,
        }

    # Deduplicate sessions that share the same clock_in_time (caused by
    # frontend network-retry races creating multiple identical DB rows).
    # Keep the session with the most breaks — it has the most complete data.
    seen_clock_in: dict = {}  # clock_in_minute_key → best session so far
    for s in sessions:
        # Round to the nearest minute so near-duplicate retries (off by <1s) merge.
        key = s.clock_in_time.strftime("%Y%m%d%H%M")
        existing = seen_clock_in.get(key)
        if existing is None:
            seen_clock_in[key] = s
        else:
            # Keep whichever has more breaks; break tie by larger total_seconds.
            existing_breaks = len(breaks_by_session[existing.id])
            candidate_breaks = len(breaks_by_session[s.id])
            if candidate_breaks > existing_breaks or (
                candidate_breaks == existing_breaks
                and (s.total_seconds or 0) > (existing.total_seconds or 0)
            ):
                seen_clock_in[key] = s

    deduped = sorted(seen_clock_in.values(), key=lambda s: s.clock_in_time, reverse=True)

    result = []
    for s in deduped:
        result.append({
            "session_id":     s.id,
            "clock_in_time":  s.clock_in_time.isoformat(),
            "clock_out_time": s.clock_out_time.isoformat() if s.clock_out_time else None,
            "total_seconds":  s.total_seconds or 0,
            "break_seconds":  s.break_seconds or 0,
            "offline_seconds": s.offline_seconds or 0,
            "is_active":      s.is_active,
            "breaks":         [fmt_break(b) for b in breaks_by_session[s.id]],
            "offline_intervals": [fmt_offline(o) for o in offline_by_session[s.id]],
        })

    return {"sessions": result}


# ── GET /break/entitlements ───────────────────────────────────────────────────
# What break types this worker may still take today. The worker dashboard uses
# this to hide the Lunch option for part-time / contractor workers (UI side of
# Test 5) and to disable an already-used break.
@router.get("/entitlements")
def get_entitlements(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    wt = break_governance.normalize_worker_type(user)
    out = {"worker_type": wt, "types": {}}
    for btype in (break_governance.SHORT_BREAK_TYPE, break_governance.LUNCH_BREAK_TYPE):
        entitlement = break_governance.entitlement_for(db, wt, btype, user.organization_id)
        used = break_governance.breaks_used_today(
            db, user.id, btype, org_id=user.organization_id)
        out["types"][btype] = {
            "allowed":         entitlement > 0,
            "entitlement":     entitlement,
            "used_today":      used,
            "remaining":       max(0, entitlement - used),
            "allowed_minutes": break_governance.allowance_minutes(db, btype, user.organization_id),
        }
    out["lunch_allowed"] = out["types"][break_governance.LUNCH_BREAK_TYPE]["allowed"]
    out["short_allowed"] = out["types"][break_governance.SHORT_BREAK_TYPE]["allowed"]
    return out


# ── GET /break/overruns ───────────────────────────────────────────────────────
# Supervisor view: every worker CURRENTLY exceeding (or near) their break
# allowance, so a reviewer sees live overruns at a glance. Admin/HR/manager only.
@router.get("/overruns")
def get_active_overruns(
    near_only: bool = False,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if (user.role or "").lower() not in ("admin", "hr", "owner", "manager"):
        raise HTTPException(403, "Admin/HR/Manager only")

    now = datetime.now(timezone.utc)
    active_breaks = (
        db.query(models.BreakSession)
        .filter(models.BreakSession.break_end == None)  # noqa: E711
        .all()
    )
    if not active_breaks:
        return {"count": 0, "overruns": []}

    user_ids = list({b.user_id for b in active_breaks})
    users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()}

    rows = []
    for b in active_breaks:
        u = users.get(b.user_id)
        if u is None:
            continue
        btype = b.break_type or "break"
        allowed_min = b.allowed_minutes or break_governance.allowance_minutes(db, btype, user.organization_id)
        grace_min = break_governance.grace_minutes(db, btype, user.organization_id)
        start = b.break_start if b.break_start.tzinfo else b.break_start.replace(tzinfo=timezone.utc)
        elapsed = max(0, int((now - start).total_seconds()))
        indicator = break_governance.governance_state(elapsed, allowed_min)
        exceeded = elapsed >= allowed_min * 60
        notification_due_seconds = (allowed_min + grace_min) * 60
        notification_due = elapsed >= notification_due_seconds
        if near_only:
            if indicator == "green":
                continue
        elif not exceeded:
            continue
        rows.append({
            "break_id":         b.id,
            "user_id":          u.id,
            "name":             u.name,
            "email":            u.email,
            "department":       u.department,
            "team":             u.team,
            "worker_type":      b.worker_type or break_governance.normalize_worker_type(u),
            "break_type":       btype,
            "status_chip":      break_governance.status_chip(btype, exceeded),
            "indicator":        indicator,
            "allowed_minutes":  allowed_min,
            "elapsed_seconds":  elapsed,
            "exceeded_seconds": max(0, elapsed - allowed_min * 60),
            "exceeded_minutes": round(max(0, elapsed - allowed_min * 60) / 60, 1),
            "notification_due": notification_due,
            "notification_delay_minutes": grace_min,
            "notification_due_seconds": notification_due_seconds,
            "notification_pending_seconds": max(0, notification_due_seconds - elapsed),
            "overrun_notification_sent": bool(b.overrun_notification_sent),
            "alert_count":      b.alert_count or 0,
            "break_start":      start.isoformat(),
        })

    rows.sort(key=lambda r: r["exceeded_seconds"], reverse=True)
    return {"count": len(rows), "overruns": rows}
