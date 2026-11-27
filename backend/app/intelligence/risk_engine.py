"""
Temveris – Intelligence Engine
Risk User Computation with Confidence Score + Reason Codes + Explainability

This module lives in the Intelligence Layer and NEVER modifies raw Operational Truth data.
All output is computed, not stored as raw fact.

Multitenancy isolation
----------------------
`compute_risk_users` accepts an `organization_id` argument and scopes all
User, ClockSession, and Activity queries to that org. Callers must always
pass the authenticated user's organization_id.
"""

from sqlalchemy.orm import Session
from sqlalchemy import and_
from database import models
from datetime import datetime, timezone, timedelta, timezone
from typing import List, Dict, Any, Optional
from app._time_utils import iso_z

import logging as _logging_ri
_log_ri = _logging_ri.getLogger("zoiko.intelligence.risk")


# Last-resort value if the canonical resolver itself fails. NOT a policy: the
# policy is `schedule.expected_work_hours`, resolved per employee below. This
# exists only so a resolver outage degrades to a number instead of a crash, and
# it is logged when it is used.
_FALLBACK_EXPECTED_HOURS = 8.0


def _expected_hours_per_day(db, user) -> float:
    """Required hours per day for this employee, from the canonical resolver.

    Never raises — a resolver problem must not break the intelligence rollup.
    """
    try:
        from app import _tz as _tzmod
        from app.attendance_policy import resolve_expected_work_seconds
        org_id = getattr(user, "organization_id", None)
        secs = resolve_expected_work_seconds(
            db, user, _tzmod.local_date(db, org_id))
        if secs and secs > 0:
            return secs / 3600.0
    except Exception:
        _log_ri.warning("expected-hours resolve failed user=%s; using the "
                        "fallback of %sh", getattr(user, "id", None),
                        _FALLBACK_EXPECTED_HOURS, exc_info=True)
    return _FALLBACK_EXPECTED_HOURS


def _expected_seconds_over(db, user, start_date, end_date,
                           sched_by_date=None) -> int:
    """This employee's obligation across [start_date, end_date] INCLUSIVE.

    Sums the daily requirement over the days the resolver says they actually
    work, so a Mon-Fri employee is not measured against a seven-day week and a
    Mon-Sat one is not measured against a five-day week. Falls back to the
    per-day figure times the calendar span only if the resolver is unavailable,
    which is the previous behaviour and is never silently preferred.

    ``sched_by_date`` — ``{date: EffectiveSchedule}`` ALREADY resolved for this
    user by a caller that batched the whole cohort. Optional, and omitting it
    keeps the original behaviour exactly; this mirrors the optional ``ctx``
    argument ``resolve_work_schedule`` already takes.

    It exists because ``resolve_work_schedules_over(db, [user], days)`` builds
    its prefetch context per CALL, so a report looping over employees paid for a
    whole build_context each time: /reports/workload measured 692 statements and
    ~98 s for 57 employees. Passing a pre-resolved map collapses that to one
    prefetch for the report. The summing rule stays here, in one place — a
    caller that re-implemented it could silently disagree with the risk engine.
    """
    try:
        from datetime import timedelta as _td
        days = []
        d = start_date
        while d <= end_date:
            days.append(d)
            d += _td(days=1)
        if not days:
            return 0
        if sched_by_date is None:
            from app.schedule_policy.resolver import resolve_work_schedules_over
            by_date = resolve_work_schedules_over(db, [user], days)
            sched_by_date = {d: (by_date.get(d) or {}).get(user.id) for d in days}
        total = 0
        for d in days:
            sched = sched_by_date.get(d)
            if sched is not None and sched.is_working_day:
                total += int(sched.expected_work_seconds or 0)
        return total
    except Exception:
        _log_ri.warning("expected-seconds-over resolve failed user=%s",
                        getattr(user, "id", None), exc_info=True)
        span = max(1, (end_date - start_date).days + 1)
        return int(_expected_hours_per_day(db, user) * 3600 * span)


def _deficit_ratio(db, user) -> float:
    """"How far below expectation counts as at risk", from the cascade.

    This module hard-coded 0.75 while /employees/risk-users already resolved
    `attendance.risk_deficit_percent`, so the same employee could appear at
    risk on one screen and healthy on the other.
    """
    try:
        from app.attendance_policy import policy_float, SETTING_RISK_DEFICIT_PCT
        return max(0.0, policy_float(db, user, SETTING_RISK_DEFICIT_PCT) / 100.0)
    except Exception:
        return 0.75


def _high_ratio(db, user) -> float:
    """Below this share of expected hours the risk level is "high"."""
    try:
        from app.attendance_policy import policy_float, SETTING_RISK_HIGH_PCT
        return max(0.0, policy_float(db, user, SETTING_RISK_HIGH_PCT) / 100.0)
    except Exception:
        return 0.50


IDLE_THRESHOLD_RATIO   = 0.6    # idle > 60% of session = risk signal
LOW_ACTIVITY_THRESHOLD = 0.3    # productivity < 30% = risk signal
MIN_DAILY_SESSIONS     = 1


# ──────────────────────────────────────────────
# REASON CODES  (machine-readable, explainable)
# ──────────────────────────────────────────────
REASON_CODES = {
    "LOW_HOURS":           "Worked hours below expected threshold for the period",
    "HIGH_IDLE_RATIO":     "Session idle time exceeds acceptable threshold",
    "NO_KEYBOARD_MOUSE":   "No keyboard or mouse activity detected during tracked sessions",
    "ZERO_ACTIVITY":       "No clock-in recorded for one or more working days",
    "LOW_PRODUCTIVITY":    "Computed productivity score below benchmark",
    "MISSING_SCREENSHOTS": "No screenshot evidence recorded for recent sessions",
}


def _productivity_score(mouse_clicks: int, key_presses: int, session_seconds: int) -> float:
    """Weighted activity score: keyboard 40%, mouse 60%. Returns 0.0–1.0."""
    if session_seconds <= 0:
        return 0.0
    activity_seconds = min(session_seconds, (mouse_clicks * 2) + (key_presses * 1))
    return round(min(1.0, activity_seconds / max(session_seconds, 1)), 4)


def _confidence_score(reasons: List[str], worked_ratio: float) -> float:
    """
    Confidence that this person is truly at risk (0.0–1.0).
    More signals + lower hours = higher confidence.
    """
    base = 1.0 - min(1.0, worked_ratio)           # 0 = fully compliant, 1 = no hours
    signal_boost = min(0.4, len(reasons) * 0.12)  # each extra signal adds weight
    return round(min(0.99, base + signal_boost), 4)


def _risk_level(confidence: float) -> str:
    if confidence >= 0.75:
        return "critical"
    if confidence >= 0.50:
        return "high"
    if confidence >= 0.25:
        return "medium"
    return "low"


def compute_risk_users(
    db: Session,
    period_days: int = 7,
    organization_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Compute risk assessment for employees over the given period.

    organization_id scopes the result to a single tenant — employees from
    other organizations are never included. Callers must always supply the
    authenticated user's organization_id; passing None returns an empty list
    rather than leaking cross-org data.
    """
    if organization_id is None:
        return []

    today        = datetime.now(timezone.utc).date()
    period_start = datetime.combine(
        today - timedelta(days=period_days - 1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )

    users = db.query(models.User).filter(
        models.User.organization_id == organization_id,
        models.User.role == "employee",
        models.User.is_active == True,
        models.User.is_password_set == True,
    ).all()

    results = []

    for u in users:
        sessions = db.query(models.ClockSession).filter(
            and_(
                models.ClockSession.user_id == u.id,
                models.ClockSession.clock_in_time >= period_start,
            )
        ).all()

        activities = db.query(models.Activity).filter(
            and_(
                models.Activity.user_id == u.id,
                models.Activity.timestamp >= period_start,
            )
        ).all()

        # ── Compute metrics ──────────────────────
        worked_seconds = sum(s.total_seconds or 0 for s in sessions)
        break_seconds  = sum(s.break_seconds  or 0 for s in sessions)
        worked_hours   = round(worked_seconds / 3600, 2)

        # Expected hours = this employee's daily requirement summed over the
        # days they were actually SCHEDULED to work. `days_elapsed` used to be
        # `period_days` calendar days by construction, so a Mon-Fri employee
        # was measured against 7 x 8h and a fully-compliant week read as 71%.
        expected_hours = _expected_seconds_over(
            db, u, period_start.date(), today) / 3600.0
        worked_ratio   = worked_hours / expected_hours if expected_hours > 0 else 0

        total_mouse = sum(a.mouse_clicks or 0 for a in activities)
        total_keys  = sum(a.key_presses  or 0 for a in activities)
        active_acts = [a for a in activities if a.is_active]
        idle_acts   = [a for a in activities if not a.is_active]

        idle_ratio = len(idle_acts) / max(len(activities), 1) if activities else 1.0
        prod_score = _productivity_score(total_mouse, total_keys, worked_seconds)

        screenshot_count = len([a for a in activities if a.file_path])

        # ── Reason codes ──────────────────────────
        reasons: List[str] = []

        if worked_ratio < _deficit_ratio(db, u):
            reasons.append("LOW_HOURS")
        if idle_ratio > IDLE_THRESHOLD_RATIO and len(activities) > 5:
            reasons.append("HIGH_IDLE_RATIO")
        if total_mouse == 0 and total_keys == 0 and worked_seconds > 3600:
            reasons.append("NO_KEYBOARD_MOUSE")
        if not sessions:
            reasons.append("ZERO_ACTIVITY")
        if prod_score < LOW_ACTIVITY_THRESHOLD and worked_seconds > 1800:
            reasons.append("LOW_PRODUCTIVITY")
        if screenshot_count == 0 and worked_seconds > 7200:
            reasons.append("MISSING_SCREENSHOTS")

        # Only surface if there is at least one risk signal
        if not reasons:
            continue

        confidence = _confidence_score(reasons, worked_ratio)

        results.append({
            "user_id":      u.id,
            "name":         u.name,
            "email":        u.email,
            "department":   u.department,
            "designation":  u.designation,

            # Core metrics
            "worked_hours":   worked_hours,
            "expected_hours": round(expected_hours, 2),
            "deficit_hours":  round(max(0, expected_hours - worked_hours), 2),
            "worked_ratio":   round(worked_ratio, 4),

            # Activity signals
            "total_mouse_clicks": total_mouse,
            "total_key_presses":  total_keys,
            "screenshot_count":   screenshot_count,
            "idle_ratio":         round(idle_ratio, 4),
            "productivity_score": prod_score,

            # Intelligence output
            "risk_level":       _risk_level(confidence),
            "confidence_score": confidence,
            "reason_codes":     reasons,

            # Explainability payload
            "explainability": {r: REASON_CODES[r] for r in reasons},

            "period_days": period_days,
            "computed_at": iso_z(datetime.now(timezone.utc)),
        })

    # Sort: most critical first, then by deficit hours
    results.sort(key=lambda x: (-x["confidence_score"], -x["deficit_hours"]))
    return results
