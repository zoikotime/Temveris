"""
AI Daily Summary endpoint
Collects daily activity data and generates smart productivity summaries.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from database.database import get_db
from database.models import User, ClockSession, BreakSession, Activity
from app.auth.service import get_current_user
from datetime import datetime, timezone, timedelta, timezone

router = APIRouter(prefix="/ai", tags=["AI Summary"])


def _day_bounds(date_str: str | None = None):
    """Return (target_date, start_utc, end_utc) for an IST calendar date.

    Uses the SAME IST day-boundary convention as /employees/me,
    /employees/me/day-summary and the Active-Time engine (via the shared
    ``ist_day_bounds_utc`` helper) so /ai/summary's Top Apps read the SAME
    calendar day as the dashboard tiles. Previously this used UTC midnight,
    which desynced them for anyone active between 00:00–05:30 IST.
    Reuses the canonical helper — no duplicate timezone logic.
    """
    from app.active_time.service import ist_today as _ist_today, ist_day_bounds_utc as _ist_bounds
    if date_str:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            target = _ist_today()
    else:
        target = _ist_today()
    start, end = _ist_bounds(target)
    return target, start, end


def _classify_distraction(pct: float) -> str:
    """Classify distraction level based on idle percentage."""
    if pct < 15:
        return "Low"
    if pct < 35:
        return "Medium"
    return "High"


# Desktop-tool classifier is shared with the tracking heartbeat (timeline app
# events) so both stay in sync. See app/_app_classify.py.
from app._app_classify import is_desktop_tool as _is_desktop_tool


# Idle accounting model
# ----------------------
# Activity.idle_seconds is intentionally NOT summed across rows — it's an
# OS watchdog "consecutive idle seconds" counter that grows monotonically
# while the user is idle, so summing wildly over-counts.
#
# Instead we anchor on the user's clocked work time (clock_in → clock_out
# minus breaks) and split it active vs idle by the RATIO of activity
# samples flagged is_active=False. This keeps a 9-hour clocked workday
# accounting to ~9h regardless of the screenshot interval (30s vs 30min)
# or any dropped samples. Same model is used in /api/app-analytics and
# /api/productivity-insights for consistency.
def _idle_seconds_from_ratio(work_secs: int, activities) -> int:
    """Idle seconds = (work_secs) × (idle samples / total samples).

    Falls back to 0 when there are no activity samples — caller should
    treat that as 'tracker offline' rather than '100% productive'.
    """
    if not activities or work_secs <= 0:
        return 0
    total_n  = len(activities)
    idle_n   = sum(1 for a in activities if not a.is_active)
    if total_n == 0:
        return 0
    return int(round(work_secs * (idle_n / total_n)))


def _generate_summary(
    hours_worked: float,
    productivity_pct: float,
    idle_pct: float,
    distraction_level: str,
    break_minutes: int,
    top_apps: list[dict],
    user_name: str,
) -> str:
    """Generate a smart rule-based daily summary string."""
    parts = []

    # Opening line
    if hours_worked >= 8:
        parts.append(f"{user_name} completed a full workday of {hours_worked:.1f} hours.")
    elif hours_worked >= 6:
        parts.append(f"{user_name} worked {hours_worked:.1f} hours today — a solid session.")
    elif hours_worked >= 3:
        parts.append(f"{user_name} logged {hours_worked:.1f} hours of work today.")
    elif hours_worked > 0:
        parts.append(f"{user_name} had a short session of {hours_worked:.1f} hours today.")
    else:
        return f"No work activity recorded for {user_name} today."

    # Productivity insight
    if productivity_pct >= 85:
        parts.append(f"Productivity was excellent at {productivity_pct:.0f}%.")
    elif productivity_pct >= 65:
        parts.append(f"Productivity was good at {productivity_pct:.0f}%.")
    elif productivity_pct >= 40:
        parts.append(f"Productivity was moderate at {productivity_pct:.0f}%.")
    else:
        parts.append(f"Productivity was low at {productivity_pct:.0f}% — consider reviewing workflow.")

    # Distraction insight
    if distraction_level == "High":
        parts.append(f"Distraction level was high ({idle_pct:.0f}% idle time). Try to minimize interruptions.")
    elif distraction_level == "Medium":
        parts.append(f"Distraction level was moderate ({idle_pct:.0f}% idle time).")

    # Break insight
    if break_minutes > 0:
        parts.append(f"Took {break_minutes} minute{'s' if break_minutes != 1 else ''} of breaks.")

    # Top apps
    if top_apps:
        app_names = ", ".join(a["app_name"] for a in top_apps[:3])
        parts.append(f"Most used apps: {app_names}.")

    return " ".join(parts)


@router.get("/summary/{user_id}")
def get_daily_summary(
    user_id: int,
    date: str | None = Query(None, description="Date in YYYY-MM-DD format. Defaults to today."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    GET /ai/summary/{user_id}?date=YYYY-MM-DD
    Returns AI-generated daily productivity summary.
    """
    # Authorization: employees can only see their own summary
    if current_user.role == "employee" and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    user = db.query(User).filter(
        User.id == user_id,
        User.organization_id == current_user.organization_id,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    target_date, day_start, day_end = _day_bounds(date)

    # ── Clock sessions for the day ──
    sessions = db.query(ClockSession).filter(
        and_(
            ClockSession.user_id == user_id,
            ClockSession.organization_id == current_user.organization_id,
            ClockSession.clock_in_time >= day_start,
            ClockSession.clock_in_time < day_end,
        )
    ).all()

    total_work_secs = 0
    total_break_secs = 0

    from app._worked import session_worked_seconds
    for sess in sessions:
        # One definition of worked time (app/_worked.py). This re-derived it and
        # deducted breaks but not offline_seconds, so the AI summary quoted more
        # hours than the report and the payslip for the same day.
        total_break_secs += sess.break_seconds or 0
        total_work_secs += session_worked_seconds(sess)

    # ── Activities for the day ──
    activities = db.query(Activity).filter(
        and_(
            Activity.user_id == user_id,
            Activity.organization_id == current_user.organization_id,
            Activity.timestamp >= day_start,
            Activity.timestamp < day_end,
        )
    ).all()

    total_mouse = sum(a.mouse_clicks or 0 for a in activities)
    total_keys = sum(a.key_presses or 0 for a in activities)
    total_idle_secs = _idle_seconds_from_ratio(total_work_secs, activities)
    total_prod_score = sum(a.productivity_score or 0.0 for a in activities)
    activity_count = len(activities)

    # ── Compute metrics ──
    hours_worked = round(total_work_secs / 3600, 2)
    productive_secs = max(0, total_work_secs - total_idle_secs)

    if total_work_secs > 0:
        productivity_pct = round((productive_secs / total_work_secs) * 100, 1)
        idle_pct = round((total_idle_secs / total_work_secs) * 100, 1)
    else:
        productivity_pct = 0.0
        idle_pct = 0.0

    avg_prod_score = round(total_prod_score / activity_count, 4) if activity_count > 0 else 0.0

    distraction_level = _classify_distraction(idle_pct)

    # ── Top apps ──
    app_usage: dict[str, int] = {}
    for a in activities:
        name = a.app_name or "Unknown"
        app_usage[name] = app_usage.get(name, 0) + 1

    top_apps = sorted(
        [{"app_name": k, "count": v} for k, v in app_usage.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:5]

    # ── Top tools ──
    # Desktop applications the user actually works in (IDEs, Office, design
    # tools…), i.e. non-browser foreground apps. Surfaced separately because the
    # raw top_apps list is dominated by browser processes, which buries tools
    # like Eclipse below the cut. Websites are already covered by the browser /
    # domain views, so browsers are intentionally excluded here.
    top_tools = sorted(
        [
            {"app_name": k, "count": v}
            for k, v in app_usage.items()
            if _is_desktop_tool(k)
        ],
        key=lambda x: x["count"],
        reverse=True,
    )[:8]

    # ── Generate summary ──
    summary_text = _generate_summary(
        hours_worked=hours_worked,
        productivity_pct=productivity_pct,
        idle_pct=idle_pct,
        distraction_level=distraction_level,
        break_minutes=round(total_break_secs / 60),
        top_apps=top_apps,
        user_name=user.name or "Employee",
    )

    return {
        "user_id": user_id,
        "user_name": user.name,
        "date": str(target_date),
        "hours_worked": hours_worked,
        "productive_hours": round(productive_secs / 3600, 2),
        "idle_hours": round(total_idle_secs / 3600, 2),
        "break_minutes": round(total_break_secs / 60),
        "productivity_percentage": productivity_pct,
        "idle_percentage": idle_pct,
        "distraction_level": distraction_level,
        "avg_productivity_score": avg_prod_score,
        "total_mouse_clicks": total_mouse,
        "total_key_presses": total_keys,
        "activity_count": activity_count,
        "sessions_count": len(sessions),
        "top_apps": top_apps,
        "top_tools": top_tools,
        "summary": summary_text,
    }


@router.get("/summary-weekly/{user_id}")
def get_weekly_summary(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    GET /ai/summary-weekly/{user_id}
    Returns a 7-day summary trend for charts.
    """
    if current_user.role == "employee" and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    user = db.query(User).filter(
        User.id == user_id,
        User.organization_id == current_user.organization_id,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    today = datetime.now(timezone.utc).date()
    days = []

    for offset in range(6, -1, -1):
        d = today - timedelta(days=offset)
        day_start = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)

        sessions = db.query(ClockSession).filter(
            and_(
                ClockSession.user_id == user_id,
                ClockSession.organization_id == current_user.organization_id,
                ClockSession.clock_in_time >= day_start,
                ClockSession.clock_in_time < day_end,
            )
        ).all()

        work_secs = 0
        brk_secs = 0
        from app._worked import session_worked_seconds
        for sess in sessions:
            # Same canonical definition as everywhere else — see above.
            brk_secs += sess.break_seconds or 0
            work_secs += session_worked_seconds(sess)

        activities = db.query(Activity).filter(
            and_(
                Activity.user_id == user_id,
                Activity.organization_id == current_user.organization_id,
                Activity.timestamp >= day_start,
                Activity.timestamp < day_end,
            )
        ).all()

        idle_secs = _idle_seconds_from_ratio(work_secs, activities)
        productive_secs = max(0, work_secs - idle_secs)
        prod_pct = round((productive_secs / work_secs) * 100, 1) if work_secs > 0 else 0.0

        days.append({
            "date": str(d),
            "day": d.strftime("%a"),
            "hours_worked": round(work_secs / 3600, 2),
            "productivity_percentage": prod_pct,
            "break_minutes": round(brk_secs / 60),
        })

    return {
        "user_id": user_id,
        "user_name": user.name,
        "period": f"{today - timedelta(days=6)} to {today}",
        "days": days,
    }
