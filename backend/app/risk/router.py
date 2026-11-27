"""
Temveris – Risk Analysis Router
==================================
Monitors employee work hours and triggers HR alerts when below threshold.

The threshold is NOT defined here. It is the employee's effective expected
daily hours, resolved through the Schedule Resolver cascade
(employee > team > org > system default) — the same number the Early Clock Out
gate enforces, so an employee can never be permitted to leave and then alerted
for the hours they were permitted to leave on.

Endpoints:
  POST /risk-alert          – manually trigger risk check for a user/date
  GET  /risk-alert/history  – list all risk alerts (admin/hr)
  POST /risk-alert/check-all – run threshold check for all users today (admin)

Multitenancy isolation
----------------------
All RiskAlert reads and writes are scoped to organization_id.
Admins can only trigger, view, and bulk-check alerts within their own org.
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta, date

from database.database import get_db
from database import models
from app._worked import session_worked_seconds
from app import _tz
from app.auth.service import get_current_user
from app.email_service import send_risk_alert_to_hr

router = APIRouter(prefix="/risk-alert", tags=["Risk Analysis"])

def _threshold_secs(db: Session, user) -> int:
    """This employee's required daily worked seconds.

    Delegates to the ONE canonical resolver (``resolve_expected_work_seconds``
    → the Schedule Resolver cascade) rather than reading a threshold of its own.
    Previously this read the org-tier ``work_hours_threshold`` row and fell back
    to a private ``DEFAULT_THRESHOLD_SECS = 31500`` (8h45m), while the Early
    Clock Out gate compared against ``expected_work_durations`` (8h00m) — so an
    employee could be *permitted* to clock out and then *alerted* for the same
    day's hours. One number now answers both questions.
    """
    from app.attendance_policy import resolve_expected_work_seconds
    return resolve_expected_work_seconds(db, user, _tz.local_date(db, getattr(user, "organization_id", None)))


def _risk_enabled(db: Session, user) -> bool:
    """Is the low-hours alert switched on for this employee?

    Read through the full employee > team > org > default cascade. The admin UI
    lists ``risk_alert_enabled`` under Notifications and ``PUT /settings/scoped``
    stores team and employee overrides for it, so reading it at the org tier
    only meant a saved override was silently inert.
    """
    from app.settings import scope as settings_scope
    try:
        merged = settings_scope.resolve_effective(db, user, {"risk_alert_enabled": "true"})
        return str(merged.get("risk_alert_enabled", "true")).strip().lower() != "false"
    except Exception:
        return True


def _fmt_duration(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    return f"{h}h {m:02d}m"


def _session_worked_secs(session: models.ClockSession) -> int:
    """Deprecated alias. The definition lives in ``app/_worked.py`` — one
    module owns it so the risk alert, the report and the payslip cannot
    disagree about how many hours somebody worked."""
    return session_worked_seconds(session)


def _run_risk_check(user: models.User, session: models.ClockSession, db: Session, background_tasks: BackgroundTasks = None) -> dict:
    """Core risk check logic. Returns alert info dict.

    Every input is this employee's EFFECTIVE policy:

      * required hours  → ``_threshold_secs`` → the canonical resolver
      * is today a day they owe hours at all → the resolver's ``is_working_day``

    There is no employment-type carve-out. "Part-time employees are skipped"
    was a Temveris convention hard-coded as a product rule; an organization that
    rosters part-timers to fixed shifts wants them measured, and one that does
    not simply resolves them to zero required hours. Contract and intern types
    are no longer silently measured against a full-timer's day either.
    """
    _org_id = getattr(user, "organization_id", None)

    # A non-working day owes nothing. Without this a Sunday clock-out fires a
    # low-hours alert, and a Mon–Sat organization's Saturday did too.
    try:
        from app.schedule_policy.resolver import resolve_work_schedule
        sched = resolve_work_schedule(db, user, _tz.local_date(db, _org_id))
        if not sched.is_working_day:
            return {"triggered": False, "skipped": True, "reason": "not_a_working_day"}
        threshold = sched.expected_work_seconds
    except Exception:
        threshold = _threshold_secs(db, user)

    # Zero required hours means "nothing owed today" — the policy-driven
    # replacement for the old hardcoded part-time skip.
    if threshold <= 0:
        return {"triggered": False, "skipped": True, "reason": "no_expected_hours"}

    worked    = _session_worked_secs(session)

    if worked >= threshold:
        return {"triggered": False, "worked_seconds": worked, "threshold_seconds": threshold}

    # Check if we already sent an alert for this session
    existing = db.query(models.RiskAlert).filter(
        and_(
            models.RiskAlert.user_id    == user.id,
            models.RiskAlert.session_id == session.id,
        )
    ).first()
    if existing:
        return {"triggered": False, "already_alerted": True, "worked_seconds": worked}

    # Create risk alert record — stamp organization_id for tenant isolation
    alert = models.RiskAlert(
        organization_id = user.organization_id,
        user_id         = user.id,
        session_id      = session.id,
        alert_date      = datetime.now(timezone.utc),
        total_seconds   = worked,
        threshold_secs  = threshold,
        alert_type      = "insufficient_hours",
        email_sent      = False,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    alert_date_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    worked_display    = _fmt_duration(worked)
    threshold_display = _fmt_duration(threshold)

    def _send():
        if not _risk_enabled(db, user):
            return
        # This organization's own audience — never a platform inbox.
        from app.notifications.recipients import recipients_for_user
        ok = send_risk_alert_to_hr(
            recipients      = recipients_for_user(db, user, purpose="risk.alert"),
            employee_name   = user.name,
            employee_email  = user.email,
            alert_date      = alert_date_str,
            worked_hours    = worked_display,
            threshold_hours = threshold_display,
        )
        alert.email_sent = ok
        db.commit()

    if background_tasks:
        background_tasks.add_task(_send)
    else:
        _send()

    # Broadcast to admin dashboard via WebSocket — org-scoped
    try:
        from websocket_manager import manager
        import asyncio, json as _json
        if user.organization_id:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(manager.broadcast_to_org(user.organization_id, _json.dumps({
                    "type":              "risk_alert",
                    "organization_id":   user.organization_id,
                    "alert_id":          alert.id,
                    "user_id":           user.id,
                    "employee_name":     user.name,
                    "alert_type":        "insufficient_hours",
                    "worked_display":    worked_display,
                    "threshold_display": threshold_display,
                    "alert_date":        alert_date_str,
                })))
    except Exception:
        pass

    return {
        "triggered":         True,
        "alert_id":          alert.id,
        "worked_seconds":    worked,
        "threshold_seconds": threshold,
        "worked_display":    worked_display,
        "threshold_display": threshold_display,
    }


# ── POST /risk-alert ──────────────────────────────────────────────────────────
class RiskCheckPayload(BaseModel):
    user_id:    Optional[int] = None   # admin can pass a user_id; employee checks self
    session_id: Optional[int] = None   # optional specific session

@router.post("")
def trigger_risk_check(
    body:             RiskCheckPayload,
    background_tasks: BackgroundTasks,
    db:               Session     = Depends(get_db),
    current_user:     models.User = Depends(get_current_user),
):
    # Resolve target user — admin may specify another user, but only within
    # their own organization (return 404 to avoid leaking cross-org existence)
    if body.user_id and current_user.role in ("admin", "hr"):
        user = db.query(models.User).filter(
            models.User.id == body.user_id,
            models.User.organization_id == current_user.organization_id,
        ).first()
        if not user:
            raise HTTPException(404, "User not found")
    else:
        user = current_user

    # Resolve session — always scoped to the resolved user
    if body.session_id:
        session = db.query(models.ClockSession).filter(
            and_(models.ClockSession.id == body.session_id,
                 models.ClockSession.user_id == user.id)
        ).first()
    else:
        session = db.query(models.ClockSession).filter(
            and_(models.ClockSession.user_id == user.id,
                 models.ClockSession.clock_out_time != None)
        ).order_by(models.ClockSession.clock_out_time.desc()).first()

    if not session:
        raise HTTPException(404, "No completed session found for this user")

    result = _run_risk_check(user, session, db, background_tasks)
    return {"success": True, "user": user.name, **result}


# ── POST /risk-alert/check-all ────────────────────────────────────────────────
@router.post("/check-all")
def check_all_users(
    background_tasks: BackgroundTasks,
    db:               Session     = Depends(get_db),
    admin:            models.User = Depends(get_current_user),
):
    """Run threshold check for all users who clocked out today. Admin/HR only."""
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Admin or HR only")

    # "Today" is this organization's calendar day, not UTC's. A UTC midnight
    # boundary silently excluded the first hours of the local morning for any
    # tenant east of Greenwich and included the previous evening for any tenant
    # west of it.
    today_start, _ = _tz.local_day_bounds(
        db, admin.organization_id, _tz.local_date(db, admin.organization_id))

    # Scope to the admin's organization — never iterate another org's sessions
    sessions = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.organization_id == admin.organization_id,
            models.ClockSession.clock_out_time >= today_start,
            models.ClockSession.clock_out_time != None,
        )
    ).all()

    results = []
    for session in sessions:
        user = db.query(models.User).filter(
            models.User.id == session.user_id,
            models.User.organization_id == admin.organization_id,
        ).first()
        if not user:
            continue
        r = _run_risk_check(user, session, db, background_tasks)
        results.append({"user_id": user.id, "name": user.name, **r})

    triggered = [r for r in results if r.get("triggered")]
    return {
        "checked":   len(results),
        "triggered": len(triggered),
        "alerts":    triggered,
    }


# ── GET /risk-alert/history ───────────────────────────────────────────────────
@router.get("/history")
def get_risk_history(
    user_id: Optional[int] = None,
    limit:   int = 50,
    db:      Session     = Depends(get_db),
    admin:   models.User = Depends(get_current_user),
):
    if admin.role not in ("admin", "hr"):
        raise HTTPException(403, "Admin or HR only")

    # Scope to org via user_id — risk_alerts table lacks organization_id in the DB.
    _risk_user_ids = [
        row.id for row in db.query(models.User.id)
        .filter(models.User.organization_id == admin.organization_id)
        .all()
    ]
    q = (
        db.query(models.RiskAlert)
        .filter(models.RiskAlert.user_id.in_(_risk_user_ids))
        .order_by(models.RiskAlert.created_at.desc())
    )
    if user_id:
        q = q.filter(models.RiskAlert.user_id == user_id)
    alerts = q.limit(limit).all()

    return [
        {
            "id":                a.id,
            "user_id":           a.user_id,
            "user_name":         db.query(models.User).filter(models.User.id == a.user_id).first().name if a.user_id else "—",
            "alert_date":        a.alert_date.isoformat(),
            "worked_display":    _fmt_duration(a.total_seconds),
            "threshold_display": _fmt_duration(a.threshold_secs),
            "total_seconds":     a.total_seconds,
            "email_sent":        a.email_sent,
            "created_at":        a.created_at.isoformat(),
        }
        for a in alerts
    ]
