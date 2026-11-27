"""
Temveris – Schedule Router
Shift templates, assignments, employee availability, and productivity insights.

Multitenancy isolation
----------------------
All Shift, ShiftAssignment, and EmployeeAvailability reads and writes are
scoped to the caller's organization_id. Org A can never see or modify org B's
shift templates, assignments, or availability records.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text, func, desc, cast, Integer
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date, timezone, timedelta
import json

from database.database import get_db
from database import models
from app import _tz
from app.auth.service import get_current_user

router = APIRouter(prefix="/schedule", tags=["Schedule"])

# Display timezone — clock_in_time / clock_out_time / activity timestamps are
# stored as UTC but the schedule grid must render on the ORGANIZATION's wall
# clock. Without converting before strftime("%H:%M") the grid shows UTC hours
# and buckets sessions onto the wrong calendar day near midnight; with a fixed
# +05:30 it did the same thing for every tenant outside India.
def _org_zone(db, org_id):
    from app import _tz as _tzmod
    return _tzmod.org_timezone(db, org_id)


def _to_local(dt, zone):
    """Coerce any datetime (naive or tz-aware) to the ORGANIZATION's zone.

    ``zone`` is required. It used to default to None and resolve to
    Asia/Kolkata, which meant every caller that forgot to pass one silently
    bucketed a tenant's sessions onto an Indian calendar day.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(zone)


def _local_hm(dt, zone):
    """Format a UTC/aware datetime as 'HH:MM' on the organization's clock."""
    local = _to_local(dt, zone)
    return local.strftime("%H:%M") if local else None


def _local_date_str(dt, zone):
    """Calendar date (YYYY-MM-DD) on the organization's clock."""
    local = _to_local(dt, zone)
    return local.strftime("%Y-%m-%d") if local else None


def _require_admin(user: models.User) -> None:
    if user.role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin access required")


# ── SCHEMAS ───────────────────────────────────────────────────────────────────

class AssignIn(BaseModel):
    user_id: int
    shift_id: Optional[int] = None
    date: str                             # "YYYY-MM-DD"
    start_time: Optional[str] = None     # override shift defaults
    end_time: Optional[str] = None
    task_type: str = "working"
    notes: Optional[str] = None


class AvailabilityIn(BaseModel):
    date: str                             # "YYYY-MM-DD"
    is_available: bool = True
    reason: Optional[str] = None


# ── SHIFT TEMPLATES ───────────────────────────────────────────────────────────
#
# READ-ONLY. The standalone Shift Management screen that owned create / edit /
# deactivate was removed, so there is deliberately no POST/PUT/DELETE here.
# This GET stays because Organization Settings -> Working Schedule needs it to
# populate the "Assigned shift" (shift_ref) row, and the Schedule Resolver
# still resolves schedule.shift_id against these rows for attendance. Do not
# delete it as orphaned.

@router.get("/shifts")
def list_shifts(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    rows = (
        db.query(models.Shift)
        .filter(
            models.Shift.organization_id == current_user.organization_id,
            models.Shift.is_active == True,
        )
        .order_by(models.Shift.name.asc())
        .all()
    )
    result = []
    for r in rows:
        dow = r.days_of_week
        if isinstance(dow, str):
            try:
                dow = json.loads(dow)
            except Exception:
                dow = [0,1,2,3,4]
        result.append({
            "id":          r.id,
            "name":        r.name,
            "shift_type":  r.shift_type,
            "start_time":  str(r.start_time)[:5] if r.start_time else None,
            "end_time":    str(r.end_time)[:5]   if r.end_time   else None,
            "days_of_week": dow,
            "color":       r.color,
        })
    return result


# ── ASSIGNMENTS ───────────────────────────────────────────────────────────────

@router.post("/assign")
def assign_shift(
    data: AssignIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    try:
        datetime.strptime(data.date, "%Y-%m-%d")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # Verify target user belongs to the same organization (404 avoids leaking
    # cross-org user existence)
    target_user = db.query(models.User).filter(
        models.User.id == data.user_id,
        models.User.organization_id == current_user.organization_id,
    ).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    start_t, end_t = data.start_time, data.end_time
    if data.shift_id:
        # Ownership is validated whenever a shift_id is supplied. This used to be
        # gated on `not start_t or not end_t`, so a caller who passed BOTH times
        # skipped the lookup entirely and could persist another org's shift_id
        # onto the assignment. 404 (not 403) to avoid leaking cross-org existence.
        shift = db.query(models.Shift).filter(
            models.Shift.id == data.shift_id,
            models.Shift.organization_id == current_user.organization_id,
        ).first()
        if not shift:
            raise HTTPException(status_code=404, detail="Shift not found")
        start_t = start_t or (str(shift.start_time)[:5] if shift.start_time else None)
        end_t   = end_t   or (str(shift.end_time)[:5]   if shift.end_time   else None)

    new_assignment = models.ShiftAssignment(
        organization_id = current_user.organization_id,
        user_id         = data.user_id,
        shift_id        = data.shift_id,
        date            = data.date,
        start_time      = start_t,
        end_time        = end_t,
        task_type       = data.task_type,
        notes           = data.notes,
        created_by      = current_user.id,
    )
    db.add(new_assignment)
    db.commit()
    db.refresh(new_assignment)

    # ZT-ATT-01 schedule notification (Operational; fail-open). Best-effort:
    # a mail failure must never block the schedule operation.
    try:
        from app.email_service import send_schedule_notification
        send_schedule_notification(
            to_email=target_user.email, name=target_user.name or "",
            variant="published", date=data.date, start_time=start_t, end_time=end_t,
            task_type=data.task_type, reply_to=current_user.email,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("ZT-ATT-01 schedule email failed (operation preserved)", exc_info=True)

    return {"id": new_assignment.id, "message": "Shift assigned"}


@router.put("/assign/{aid}")
def update_assignment(
    aid: int,
    data: AssignIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    assignment = db.query(models.ShiftAssignment).filter(
        models.ShiftAssignment.id == aid,
        models.ShiftAssignment.organization_id == current_user.organization_id,
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    assignment.start_time = data.start_time
    assignment.end_time   = data.end_time
    assignment.task_type  = data.task_type
    assignment.notes      = data.notes
    db.commit()

    # ZT-ATT-01 schedule notification (Operational; fail-open).
    try:
        from app.email_service import send_schedule_notification
        _worker = db.query(models.User).filter(models.User.id == assignment.user_id).first()
        if _worker:
            send_schedule_notification(
                to_email=_worker.email, name=_worker.name or "",
                variant="changed", date=assignment.date,
                start_time=assignment.start_time, end_time=assignment.end_time,
                task_type=assignment.task_type, reply_to=current_user.email,
            )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("ZT-ATT-01 schedule email failed (operation preserved)", exc_info=True)

    return {"message": "Assignment updated"}


@router.delete("/assign/{aid}")
def cancel_assignment(
    aid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    assignment = db.query(models.ShiftAssignment).filter(
        models.ShiftAssignment.id == aid,
        models.ShiftAssignment.organization_id == current_user.organization_id,
    ).first()
    if assignment:
        assignment.status = 'cancelled'
        db.commit()

        # ZT-ATT-01 schedule notification (Operational; fail-open).
        try:
            from app.email_service import send_schedule_notification
            _worker = db.query(models.User).filter(models.User.id == assignment.user_id).first()
            if _worker:
                send_schedule_notification(
                    to_email=_worker.email, name=_worker.name or "",
                    variant="cancelled", date=assignment.date,
                    start_time=assignment.start_time, end_time=assignment.end_time,
                    task_type=assignment.task_type, reply_to=current_user.email,
                )
        except Exception:
            import logging
            logging.getLogger(__name__).warning("ZT-ATT-01 schedule email failed (operation preserved)", exc_info=True)

    return {"message": "Assignment cancelled"}


# ── MY SCHEDULE ───────────────────────────────────────────────────────────────

@router.get("/my-schedule")
def my_schedule(
    start: str     = Query(...),   # YYYY-MM-DD
    end:   str     = Query(...),
    db:    Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    user_id = current_user.id
    org_id  = current_user.organization_id
    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d   = datetime.strptime(end,   "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # ShiftAssignment.date is a varchar(10) "YYYY-MM-DD" column, so the bound
    # parameter must be a string — Postgres won't auto-cast varchar >= date.
    start_str = start_d.isoformat()
    end_str   = end_d.isoformat()

    # Formal shift assignments — org-scoped
    asgn_rows = (
        db.query(
            models.ShiftAssignment.id,
            models.ShiftAssignment.date,
            models.ShiftAssignment.start_time,
            models.ShiftAssignment.end_time,
            models.ShiftAssignment.task_type,
            models.ShiftAssignment.status,
            models.ShiftAssignment.notes,
            models.Shift.name.label("shift_name"),
            models.Shift.color.label("color"),
            models.Shift.shift_type,
        )
        .join(models.Shift, models.ShiftAssignment.shift_id == models.Shift.id, isouter=True)
        .filter(
            models.ShiftAssignment.organization_id == org_id,
            models.ShiftAssignment.user_id == user_id,
            models.ShiftAssignment.date >= start_str,
            models.ShiftAssignment.date <= end_str,
            models.ShiftAssignment.status != "cancelled",
        )
        .order_by(models.ShiftAssignment.date, models.ShiftAssignment.start_time)
        .all()
    )

    # SQL-side day bucketing runs in the ORGANIZATION's zone. It was a literal
    # 'Asia/Kolkata', which put every non-Indian tenant's activity on the wrong
    # calendar day (and the wrong hour bucket) in this grid alone.
    tz_name = _tz.org_timezone_name(db, org_id)
    zone = _org_zone(db, org_id)

    # Day-boundary range used for session/activity filtering. Pulling a day or
    # two of slack ensures we don't miss rows whose UTC date differs from their
    # local date — final bucketing happens in Python below.
    sess_filter_start = start_d - timedelta(days=1)
    sess_filter_end   = end_d   + timedelta(days=1)

    # Actual clock sessions — user-scoped (implicitly org-safe)
    sess_rows = (
        db.query(
            models.ClockSession.id,
            models.ClockSession.clock_in_time,
            models.ClockSession.clock_out_time,
            models.ClockSession.total_seconds,
            models.ClockSession.break_seconds,
        )
        .filter(
            models.ClockSession.user_id == user_id,
            models.ClockSession.clock_in_time >= sess_filter_start,
            models.ClockSession.clock_in_time <  sess_filter_end,
        )
        .order_by(models.ClockSession.clock_in_time)
        .all()
    )

    # Activity aggregate per IST calendar day
    act_rows = (
        db.query(
            func.date(func.timezone(tz_name, func.timezone('UTC', models.Activity.timestamp))).label("day"),
            func.count(models.Activity.id).label("screenshot_count"),
            func.coalesce(func.sum(models.Activity.idle_seconds), 0).label("total_idle"),
            func.count(models.Activity.id).filter(models.Activity.is_active == True).label("active_ticks"),
            func.coalesce(func.avg(models.Activity.productivity_score), 0).label("avg_score"),
        )
        .filter(
            models.Activity.user_id == user_id,
            func.date(func.timezone(tz_name, func.timezone('UTC', models.Activity.timestamp))) >= start_d,
            func.date(func.timezone(tz_name, func.timezone('UTC', models.Activity.timestamp))) <= end_d,
        )
        .group_by("day")
        .all()
    )

    # Approved / pending leaves — user-scoped
    leave_rows = (
        db.query(
            models.LeaveRequest.id,
            models.LeaveRequest.leave_type,
            models.LeaveRequest.from_date,
            models.LeaveRequest.to_date,
            models.LeaveRequest.status,
        )
        .filter(
            models.LeaveRequest.user_id == user_id,
            models.LeaveRequest.status.in_(['submitted', 'under_review', 'approved']),
            models.LeaveRequest.from_date <= end_d,
            models.LeaveRequest.to_date   >= start_d,
        )
        .order_by(models.LeaveRequest.from_date)
        .all()
    )

    # Holidays in range — org-scoped, and country-scoped through the shared
    # calendar so an employee's schedule shows the calendar their organization
    # actually runs on rather than every country the workspace maintains.
    from app.holidays import org_calendar as _holiday_calendar
    _applicable_dates = _holiday_calendar.holiday_dates_or_empty(
        db, org_id, start=start_d, end=end_d
    )
    hol_rows = [
        row for row in (
            db.query(models.Holiday.id, models.Holiday.name, models.Holiday.date,
                     models.Holiday.holiday_type)
            .filter(
                models.Holiday.organization_id == org_id,
                models.Holiday.date >= start_d,
                models.Holiday.date <= end_d,
            )
            .order_by(models.Holiday.date)
            .all()
        )
        if row.date in _applicable_dates
    ]

    # User-marked availability — user-scoped
    avail_rows = (
        db.query(
            models.EmployeeAvailability.id,
            models.EmployeeAvailability.date,
            models.EmployeeAvailability.is_available,
            models.EmployeeAvailability.reason,
        )
        .filter(
            models.EmployeeAvailability.organization_id == org_id,
            models.EmployeeAvailability.user_id == user_id,
            models.EmployeeAvailability.date >= start_str,
            models.EmployeeAvailability.date <= end_str,
        )
        .order_by(models.EmployeeAvailability.date)
        .all()
    )

    # Build per-day lookups. Bucket by IST date (not UTC) so a session that
    # starts at 23:30 IST (18:00 UTC same day) ends up under the IST day.
    sess_by_date: dict = {}
    for s in sess_rows:
        d = _local_date_str(s.clock_in_time, zone)
        if d is None or not (start_d.strftime("%Y-%m-%d") <= d <= end_d.strftime("%Y-%m-%d")):
            continue
        sess_by_date.setdefault(d, []).append(s)

    act_by_date = {
        a.day.strftime("%Y-%m-%d"): {
            "screenshot_count": int(a.screenshot_count),
            "total_idle":       int(a.total_idle),
            "active_ticks":     int(a.active_ticks),
            "avg_score":        round(float(a.avg_score), 2),
        }
        for a in act_rows
    }

    # Compose assignments with actuals. "today" is the organization's today —
    # in_progress / upcoming / absent are decided on the clock its employees
    # work to, not the server's and not India's.
    today = _tz.local_date(db, org_id)
    assignments = []
    for a in asgn_rows:
        date_str    = a.date.strftime("%Y-%m-%d") if hasattr(a.date, "strftime") else str(a.date)[:10]
        sched_start = str(a.start_time)[:5] if a.start_time else None
        sched_end   = str(a.end_time)[:5]   if a.end_time   else None

        day_sess   = sess_by_date.get(date_str, [])
        actual_in  = None
        actual_out = None
        total_secs = 0
        break_secs = 0

        if day_sess:
            first      = min(day_sess, key=lambda x: x.clock_in_time)
            last       = max(day_sess, key=lambda x: x.clock_in_time)
            actual_in  = _local_hm(first.clock_in_time, zone)
            actual_out = _local_hm(last.clock_out_time, zone)
            total_secs = sum(x.total_seconds or 0 for x in day_sess)
            break_secs = sum(x.break_seconds or 0 for x in day_sess)

        act = act_by_date.get(date_str, {})

        late_min  = 0
        early_min = 0
        if sched_start and actual_in:
            try:
                sh, sm = map(int, sched_start.split(":"))
                ah, am = map(int, actual_in.split(":"))
                diff = (ah * 60 + am) - (sh * 60 + sm)
                if diff > 0:
                    late_min = diff
            except Exception:
                pass
        if sched_end and actual_out:
            try:
                eh, em = map(int, sched_end.split(":"))
                oh, om = map(int, actual_out.split(":"))
                diff = (eh * 60 + em) - (oh * 60 + om)
                if diff > 0:
                    early_min = diff
            except Exception:
                pass

        # Auto-resolve status
        auto_status = a.status
        if auto_status == "scheduled":
            adate = datetime.strptime(date_str, "%Y-%m-%d").date()
            if adate < today:
                auto_status = "completed" if actual_out else ("incomplete" if day_sess else "absent")
            elif adate == today:
                auto_status = "in_progress" if day_sess else "upcoming"

        assignments.append({
            "id":              a.id,
            "date":            date_str,
            "shift_name":      a.shift_name or "Shift",
            "task_type":       a.task_type,
            "color":           a.color or "#3B82F6",
            "scheduled_start": sched_start,
            "scheduled_end":   sched_end,
            "status":          auto_status,
            "actual": {
                "clock_in":         actual_in,
                "clock_out":        actual_out,
                "total_seconds":    total_secs,
                "break_seconds":    break_secs,
                "active_ticks":     act.get("active_ticks", 0),
                "idle_seconds":     act.get("total_idle",  0),
                "screenshot_count": act.get("screenshot_count", 0),
                "avg_score":        act.get("avg_score",   0),
                "late_minutes":     late_min,
                "early_minutes":    early_min,
            },
        })

    # Also include raw clock sessions that have no assignment (so calendar is
    # useful even before admin has created any shift templates)
    assigned_dates = {a["date"] for a in assignments}
    raw_sessions = []
    for d_str, sessions in sess_by_date.items():
        if d_str in assigned_dates:
            continue
        first = min(sessions, key=lambda x: x.clock_in_time)
        last  = max(sessions, key=lambda x: x.clock_in_time)
        act   = act_by_date.get(d_str, {})
        raw_sessions.append({
            "date":             d_str,
            "clock_in":         _local_hm(first.clock_in_time, zone),
            "clock_out":        _local_hm(last.clock_out_time, zone),
            "total_seconds":    sum(x.total_seconds or 0 for x in sessions),
            "break_seconds":    sum(x.break_seconds or 0 for x in sessions),
            "screenshot_count": act.get("screenshot_count", 0),
            "idle_seconds":     act.get("total_idle",  0),
            "active_ticks":     act.get("active_ticks", 0),
        })

    # The caller's own working days, so the calendar grid can shade non-working
    # days from POLICY instead of assuming Saturday and Sunday.
    _working_days = []
    try:
        from app.schedule_policy.resolver import resolve_work_schedule
        _working_days = list(
            resolve_work_schedule(db, current_user, today).working_days or [])
    except Exception:
        _working_days = []

    return {
        "assignments":  assignments,
        "raw_sessions": raw_sessions,
        "working_days": _working_days,
        "timezone":     tz_name,
        "leaves": [
            {
                "id":         l.id,
                "leave_type": l.leave_type,
                "from_date":  l.from_date.strftime("%Y-%m-%d"),
                "to_date":    l.to_date.strftime("%Y-%m-%d"),
                "status":     l.status,
            }
            for l in leave_rows
        ],
        "holidays": [
            {
                "id":           h.id,
                "name":         h.name,
                "date":         h.date.strftime("%Y-%m-%d"),
                "holiday_type": h.holiday_type,
            }
            for h in hol_rows
        ],
        "availabilities": [
            {
                "id":           v.id,
                "date":         v.date,
                "is_available": bool(v.is_available),
                "reason":       v.reason or "",
            }
            for v in avail_rows
        ],
    }


# ── DAY INSIGHTS ──────────────────────────────────────────────────────────────

@router.get("/day-insights")
def day_insights(
    date:  str     = Query(...),    # YYYY-MM-DD
    db:    Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    user_id = current_user.id
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format")

    # Day filtering uses the ORGANIZATION's calendar day. clock_in_time /
    # timestamp are stored in UTC, so we shift them into that zone before
    # truncating to ::date.
    tz_name = _tz.org_timezone_name(db, current_user.organization_id)
    zone = _org_zone(db, current_user.organization_id)
    sess = (
        db.query(
            models.ClockSession.clock_in_time,
            models.ClockSession.clock_out_time,
            models.ClockSession.total_seconds,
            models.ClockSession.break_seconds,
        )
        .filter(
            models.ClockSession.user_id == user_id,
            func.date(func.timezone(tz_name, func.timezone('UTC', models.ClockSession.clock_in_time))) == d,
        )
        .order_by(models.ClockSession.clock_in_time)
        .first()
    )

    acts = (
        db.query(
            func.count(models.Activity.id).label("screenshot_count"),
            func.coalesce(func.sum(models.Activity.idle_seconds), 0).label("total_idle"),
            func.count(models.Activity.id).filter(models.Activity.is_active == True).label("active_ticks"),
            func.coalesce(func.avg(models.Activity.productivity_score), 0).label("avg_score"),
        )
        .filter(
            models.Activity.user_id == user_id,
            func.date(func.timezone(tz_name, func.timezone('UTC', models.Activity.timestamp))) == d,
        )
        .first()
    )

    top_apps = (
        db.query(models.Activity.app_name, func.count(models.Activity.id).label("cnt"))
        .filter(
            models.Activity.user_id == user_id,
            func.date(func.timezone(tz_name, func.timezone('UTC', models.Activity.timestamp))) == d,
            models.Activity.app_name.isnot(None),
        )
        .group_by(models.Activity.app_name)
        .order_by(desc("cnt"))
        .limit(6)
        .all()
    )

    # Hour bucket is the organization's local hour, so the bar chart matches
    # the clock its employees actually work to.
    hourly = (
        db.query(
            cast(func.extract('hour', func.timezone(tz_name, func.timezone('UTC', models.Activity.timestamp))), Integer).label("hr"),
            func.count(models.Activity.id).filter(models.Activity.is_active == True).label("active_ticks"),
            func.coalesce(func.sum(models.Activity.idle_seconds), 0).label("idle_secs"),
        )
        .filter(
            models.Activity.user_id == user_id,
            func.date(func.timezone(tz_name, func.timezone('UTC', models.Activity.timestamp))) == d,
        )
        .group_by("hr")
        .order_by("hr")
        .all()
    )

    return {
        "date": date,
        "session": {
            "clock_in":      _local_hm(sess.clock_in_time, zone)  if sess and sess.clock_in_time  else None,
            "clock_out":     _local_hm(sess.clock_out_time, zone) if sess and sess.clock_out_time else None,
            "total_seconds": sess.total_seconds or 0      if sess else 0,
            "break_seconds": sess.break_seconds or 0      if sess else 0,
        } if sess else None,
        "activity": {
            "screenshot_count":   int(acts.screenshot_count or 0),
            "total_idle_seconds": int(acts.total_idle       or 0),
            "active_ticks":       int(acts.active_ticks     or 0),
            "avg_productivity":   round(float(acts.avg_score or 0), 2),
        },
        "top_apps": [{"app": r.app_name, "count": int(r.cnt)} for r in top_apps],
        "hourly":   [{"hour": r.hr, "active": int(r.active_ticks), "idle": int(r.idle_secs)} for r in hourly],
    }


# ── AVAILABILITY ──────────────────────────────────────────────────────────────

@router.get("/availability")
def get_availability(
    month: str     = Query(...),    # YYYY-MM
    db:    Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    rows = (
        db.query(models.EmployeeAvailability)
        .filter(
            models.EmployeeAvailability.organization_id == current_user.organization_id,
            models.EmployeeAvailability.user_id == current_user.id,
            models.EmployeeAvailability.date.startswith(month),
        )
        .order_by(models.EmployeeAvailability.date.asc())
        .all()
    )
    return [
        {
            "date":         r.date,
            "is_available": r.is_available,
            "reason":       r.reason or "",
        }
        for r in rows
    ]


@router.post("/availability")
def set_availability(
    data: AvailabilityIn,
    db:   Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    try:
        target = datetime.strptime(data.date, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format")

    today_local = _tz.local_date(db, current_user.organization_id)
    if target < today_local:
        raise HTTPException(
            status_code=400,
            detail="Cannot mark availability for past dates",
        )

    avail = db.query(models.EmployeeAvailability).filter(
        models.EmployeeAvailability.organization_id == current_user.organization_id,
        models.EmployeeAvailability.user_id == current_user.id,
        models.EmployeeAvailability.date == data.date,
    ).first()

    if avail:
        avail.is_available = data.is_available
        avail.reason       = data.reason
    else:
        db.add(models.EmployeeAvailability(
            organization_id = current_user.organization_id,
            user_id         = current_user.id,
            date            = data.date,
            is_available    = data.is_available,
            reason          = data.reason,
        ))
    db.commit()
    return {"message": "Availability updated"}


# ── TEAM SCHEDULE (admin / HR) ────────────────────────────────────────────────

@router.get("/team")
def team_schedule(
    start: str     = Query(...),
    end:   str     = Query(...),
    db:    Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d   = datetime.strptime(end,   "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format")

    org_id         = current_user.organization_id
    zone           = _org_zone(db, org_id)
    asgn_start_str = start_d.isoformat()
    asgn_end_str   = end_d.isoformat()

    # Team assignments — org-scoped via both ShiftAssignment.organization_id
    # and User.organization_id (defense in depth)
    assignments = (
        db.query(
            models.ShiftAssignment.id,
            models.ShiftAssignment.date,
            models.ShiftAssignment.start_time,
            models.ShiftAssignment.end_time,
            models.ShiftAssignment.task_type,
            models.ShiftAssignment.status,
            models.User.id.label("user_id"),
            models.User.name.label("user_name"),
            models.User.department,
            models.Shift.name.label("shift_name"),
            models.Shift.color,
        )
        .join(models.User, models.ShiftAssignment.user_id == models.User.id)
        .join(models.Shift, models.ShiftAssignment.shift_id == models.Shift.id, isouter=True)
        .filter(
            models.ShiftAssignment.organization_id == org_id,
            models.User.organization_id == org_id,
            models.ShiftAssignment.date >= asgn_start_str,
            models.ShiftAssignment.date <= asgn_end_str,
            models.ShiftAssignment.status != 'cancelled',
            models.User.is_active == True,
        )
        .order_by(models.ShiftAssignment.date, models.User.name)
        .all()
    )

    # Actual sessions — org-scoped. Pad ±1 day on the UTC filter so
    # IST-day-edge sessions aren't dropped; final bucketing uses IST date.
    sessions = (
        db.query(
            models.ClockSession.user_id,
            models.ClockSession.clock_in_time,
            models.ClockSession.clock_out_time,
            models.ClockSession.total_seconds,
        )
        .filter(
            models.ClockSession.organization_id == org_id,
            models.ClockSession.clock_in_time >= start_d - timedelta(days=1),
            models.ClockSession.clock_in_time <= end_d   + timedelta(days=1),
        )
        .all()
    )

    start_str = start_d.strftime("%Y-%m-%d")
    end_str   = end_d.strftime("%Y-%m-%d")

    sess_map: dict = {}
    for s in sessions:
        d = _local_date_str(s.clock_in_time, zone)
        if d is None or not (start_str <= d <= end_str):
            continue
        k     = f"{s.user_id}:{d}"
        entry = sess_map.setdefault(k, {"clock_in": None, "clock_out": None, "total_seconds": 0})
        ci = _local_hm(s.clock_in_time, zone)
        co = _local_hm(s.clock_out_time, zone)
        if not entry["clock_in"] or ci < entry["clock_in"]:
            entry["clock_in"] = ci
        if co and (not entry["clock_out"] or co > entry["clock_out"]):
            entry["clock_out"] = co
        entry["total_seconds"] += s.total_seconds or 0

    result = []
    for a in assignments:
        date_str = a.date.strftime("%Y-%m-%d") if hasattr(a.date, "strftime") else str(a.date)[:10]
        sess     = sess_map.get(f"{a.user_id}:{date_str}", {})
        result.append({
            "assignment_id":   a.id,
            "date":            date_str,
            "user_id":         a.user_id,
            "user_name":       a.user_name,
            "department":      a.department,
            "shift_name":      a.shift_name or "Shift",
            "color":           a.color or "#3B82F6",
            "task_type":       a.task_type,
            "scheduled_start": str(a.start_time)[:5] if a.start_time else None,
            "scheduled_end":   str(a.end_time)[:5]   if a.end_time   else None,
            "actual_clock_in":  sess.get("clock_in"),
            "actual_clock_out": sess.get("clock_out"),
            "total_seconds":    sess.get("total_seconds", 0),
            "status":           a.status,
        })
    return {"team_schedule": result}
