"""
Temveris — Reports Router
Powers the Admin → Reports section of the dashboard.

All endpoints are read-only and admin-facing. They aggregate data already
captured by the rest of the platform (clock sessions, break sessions,
activities, leave requests, audit logs) and shape it for the report views
shown in the UI screenshots:

  • Daily Attendance       → /reports/daily-attendance
  • Monthly Attendance     → /reports/monthly-attendance
  • Monthly In/Out         → /reports/monthly-inout
  • Late Clock-In          → /reports/late-clockin
  • Overtime Report        → /reports/overtime
  • Custom Report          → /reports/custom
  • Apps & Sites Usage     → /reports/apps-sites
  • Work Activity Log      → /reports/work-activity
  • Work Load Analysis     → /reports/workload
  • Shift Time Report      → /reports/shift-time
  • Summary Report         → /reports/summary
  • Input Activity Report  → /reports/input-activity
  • Login IP Report        → /reports/login-ip
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from datetime import datetime, timezone, timedelta, timezone, date, time, timezone
from collections import defaultdict
from typing import Optional
import requests as _requests

from database.database import get_db
from database import models
from app.auth.service import get_current_user, get_current_organization
from app._system_admin import is_system_admin, exclude_system_admin
from app._time_adjustments import adjustment_seconds_for_day_by_user
from app._worked import session_worked_seconds

router = APIRouter(prefix="/reports", tags=["Reports"])


# ──────────────────────────────────────────────────────────────────────
# In-process TTL cache for read-only aggregate reports.
# These endpoints are pure functions of (organization, query params). Caching
# collapses repeated/concurrent identical requests (e.g. several admins opening
# the same date, or quick re-renders) into one aggregation. Time-based expiry
# only — at most _RPT_TTL_SECONDS of staleness for today's still-changing data,
# which matches the dashboard's existing 30 s team cache. Past dates are
# immutable. No invalidation hooks, so report calculations are untouched.
#
# The key MUST start with the organization. This block previously read "this is
# a single-tenant deployment ... so a global key is safe", and the keys were
# built from date and user_id alone. That assumption was wrong — the cache is
# consulted BEFORE any org filtering, so org A's admin opening a report seeded
# an entry that org B's admin then received for the rest of the TTL. `user_id`
# defaults to None, so the colliding key was the DEFAULT request, not an edge
# case. Passing org_id as a required first argument rather than leaving it to
# each call site is deliberate: a forgotten tenant is a silent data leak, and
# this way omitting it is a TypeError instead.
# ──────────────────────────────────────────────────────────────────────
import logging as _rpt_logging
_rpt_log = _rpt_logging.getLogger("zoiko.reports")

_RPT_CACHE: dict = {}
_RPT_TTL_SECONDS = 30


def _rpt_key(org_id, key: str) -> str:
    """Tenant-qualified cache key. One builder so the get and set paths cannot
    drift apart — two separately-written format strings is how a key loses a
    field without anyone noticing."""
    return f"org:{org_id}|{key}"


def _rpt_cache_get(org_id, key: str):
    entry = _RPT_CACHE.get(_rpt_key(org_id, key))
    if entry and datetime.now(timezone.utc) < entry["expires_at"]:
        return entry["data"]
    return None


def _rpt_cache_set(org_id, key: str, data, ttl_seconds: int = _RPT_TTL_SECONDS):
    _RPT_CACHE[_rpt_key(org_id, key)] = {
        "data": data,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    }
    # Opportunistic prune so the dict can't grow unbounded across many dates.
    if len(_RPT_CACHE) > 256:
        now = datetime.now(timezone.utc)
        for k in [k for k, v in _RPT_CACHE.items() if v["expires_at"] <= now]:
            _RPT_CACHE.pop(k, None)
    return data


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _parse_date(s: Optional[str], db=None, org_id=None) -> date:
    """Parse a YYYY-MM-DD query param, defaulting to the organization's today.

    Callers that pass db/org_id get the tenant's calendar date; the UTC date is
    only used when a caller has no tenant context, and it is wrong by up to a
    day for anyone far from UTC.
    """
    if not s:
        if db is not None and org_id is not None:
            from app import _tz as _tzmod
            return _tzmod.local_date(db, org_id)
        return datetime.now(timezone.utc).date()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")


def _parse_month(s: Optional[str], db=None, org_id=None) -> tuple[date, date]:
    """Parse 'YYYY-MM' into (first_day, last_day), defaulting to the
    organization's current month. On the 1st, a tenant ahead of UTC used to
    get the previous month as its default payroll period."""
    if db is not None and org_id is not None:
        from app import _tz as _tzmod
        today = _tzmod.local_date(db, org_id)
    else:
        today = datetime.now(timezone.utc).date()
    if not s:
        first = today.replace(day=1)
    else:
        try:
            first = datetime.strptime(s + "-01", "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid month. Use YYYY-MM")
    if first.month == 12:
        last = first.replace(year=first.year + 1, month=1) - timedelta(days=1)
    else:
        last = first.replace(month=first.month + 1) - timedelta(days=1)
    return first, last


def _day_window(db, org_id, d: date) -> tuple[datetime, datetime]:
    """UTC [start, end) of the ORGANIZATION-local calendar day ``d``.

    This used to be a UTC midnight window, which meant a daily report and the
    admin dashboard disagreed about which day a session belonged to for every
    tenant not on UTC — an Indian worker clocking in at 09:00 local landed in
    the right bucket, but one clocking in at 01:00 local landed on the previous
    day's report while the dashboard showed them on today.
    """
    from app import _tz as _tzmod
    return _tzmod.local_day_bounds(db, org_id, d)


def _fmt_duration(seconds: int) -> str:
    if not seconds or seconds <= 0:
        return "00h 00m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}h {m:02d}m"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    s = dt.isoformat()
    return s if s.endswith("Z") or "+" in s[-6:] else s + "Z"


# Reports define NO business thresholds of their own.
#
# What used to live here:
#   LATE_THRESHOLD_HOUR/MINUTE = 9:30  — compared against clock_in_dt.hour,
#       which is a UTC hour, so "late" actually meant 15:00 for an IST tenant
#       while the dashboard used shift-start + grace. Two answers to one
#       question, and the report's was wrong for every tenant.
#   EXPECTED_HOURS_PER_DAY = 8         — a fourth expected-hours value.
#   OVERTIME_THRESHOLD_SEC = 28800     — derived from it.
#
# All three now come from each employee's effective schedule via `_sched_map`
# (one batched prefetch per report, five queries regardless of size).


def _user_dict(u: models.User) -> dict:
    return {
        "id":          u.id,
        "name":        u.name,
        "email":       u.email,
        "employee_id": u.employee_id,
        "team":        u.team,
        "department":  u.department,
        "designation": u.designation,
        # Surfaced so attendance/history UIs can size progress against the
        # employee's OWN required hours (part-time 4h vs full-time 8h45m).
        "employment_type": u.employment_type or "full_time",
    }



# Payroll-grade leave statuses. Matches app.payroll.service.get_lwp_days so
# the Monthly Attendance report and the Payroll engine never disagree on
# what "approved leave" means. Pending / submitted / under_review do NOT
# count — they aren't finalized yet and would distort salary math.
PAYROLL_LEAVE_STATUSES = ("approved", "locked_for_payroll")


def _session_seconds(s, now: datetime) -> int:
    """Worked seconds for a clock session, including in-progress ones.

    Delegates to ``app/_worked.py``. This used to be its own implementation
    that omitted ``offline_seconds`` on the recompute path, so a session with
    a connectivity gap reported more hours here than the risk check saw.
    """
    return session_worked_seconds(s, now)


def _is_on_leave(u_id: int, target: date, leaves: list) -> Optional[dict]:
    for l in leaves:
        if l.user_id != u_id:
            continue
        if l.status not in PAYROLL_LEAVE_STATUSES:
            continue
        f = l.from_date.date() if l.from_date else None
        t = l.to_date.date()   if l.to_date   else f
        if f and t and f <= target <= t:
            return {
                "leave_type": l.leave_type,
                "from_date":  f.isoformat(),
                "to_date":    t.isoformat(),
                "description": l.description or "",
            }
    return None


def _is_late(clock_in_dt: datetime, sched=None) -> bool:
    """Was this arrival late?

    Delegates to ``classify_arrival`` — the SAME decision table the dashboard
    and the attendance monitor use — evaluated in the schedule's own timezone.
    This function performs no threshold arithmetic of its own.

    No resolved schedule, or no scheduled start, means there is nothing to be
    late against, so the answer is False. That is deliberate: inventing a
    default start time here is exactly how the report came to disagree with
    every other surface.
    """
    if not clock_in_dt or sched is None:
        return False
    if getattr(sched, "start_local", None) is None:
        return False
    try:
        from app.schedule_policy.classify import ArrivalStatus, classify_arrival
        ci = clock_in_dt
        if ci.tzinfo is None:
            ci = ci.replace(tzinfo=timezone.utc)
        ci = ci.astimezone(_zone_of(sched))
        verdict = classify_arrival(ci, sched, ci)
        return verdict.status in (ArrivalStatus.LATE, ArrivalStatus.VERY_LATE)
    except Exception:
        return False


def _zone_of(sched, db=None, org_id=None):
    """The tzinfo a resolved schedule is expressed in.

    A schedule always carries its organization's zone. When one somehow does
    not, ask _tz for the organization's zone rather than dropping to the fixed
    +05:30 guard — that guard exists for a runtime with no tz database, and
    using it here silently classified a US tenant's arrivals on India time.
    """
    from app import _tz as _tzmod
    zone = _tzmod._load_zone(getattr(sched, "timezone_name", None))
    if zone is not None:
        return zone
    if db is not None and org_id is not None:
        return _tzmod.org_timezone(db, org_id)
    return _tzmod._load_zone(_tzmod.FALLBACK_TZ_NAME) or _tzmod._IST_FIXED


def _tz_local_date(db, org_id) -> date:
    """Today, on this organization's clock."""
    from app import _tz as _tzmod
    return _tzmod.local_date(db, org_id)


def _org_date_of(db, org_id, dt) -> date:
    """The organization-local calendar date an instant falls on."""
    from app import _tz as _tzmod
    return _tzmod.to_local(db, org_id, dt).date()


def _sched_map(db, org_id, users, dates) -> dict:
    """``{(user_id, date): EffectiveSchedule}`` for every user and date.

    One prefetch (five queries, see ``schedule_policy.resolver.build_context``),
    so a report never pays O(users x days) resolver cost. Returns ``{}`` only on
    a structural failure, which is logged — callers then treat the schedule as
    unknown rather than substituting a default.
    """
    try:
        from app.schedule_policy.resolver import resolve_work_schedules_over
        by_day = resolve_work_schedules_over(db, list(users), list(dates))
        return {(uid, d): sc for d, m in by_day.items() for uid, sc in m.items()}
    except Exception:
        _rpt_log.warning("schedule prefetch failed org=%s; reports will treat "
                         "schedules as unknown for this call", org_id, exc_info=True)
        return {}


# ──────────────────────────────────────────────────────────────────────
# 1.  DAILY ATTENDANCE
#     Returns 4 buckets matching the screenshot tabs:
#       worked, late, on_leave, absent
# ──────────────────────────────────────────────────────────────────────
@router.get("/daily-attendance")
def daily_attendance(
    date: Optional[str] = Query(None, description="YYYY-MM-DD (default = today)"),
    user_id: Optional[int] = Query(None, description="Filter by employee id"),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    # Anchors in-progress sessions for _session_seconds(). Referenced
    # below but never defined -> NameError, so this endpoint 500'd.
    now_utc = datetime.now(timezone.utc)
    _ck = f"daily-attendance:{date}:{user_id}"
    _hit = _rpt_cache_get(current_org.id, _ck)
    if _hit is not None:
        return _hit
    target_date = _parse_date(date, db, current_org.id)
    start, end  = _day_window(db, current_org.id, target_date)

    users_q = db.query(models.User).filter(models.User.organization_id == current_org.id)
    if user_id:
        users_q = users_q.filter(models.User.id == user_id)
    # Always strip the seeded Administrator (CEO seat) so attendance
    # aggregates, exports, and payroll-grade reports never include it.
    users = exclude_system_admin(users_q.all(), db)
    user_ids = [u.id for u in users]
    _smap = _sched_map(db, current_org.id, users, [target_date])

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id.in_(user_ids),
            models.ClockSession.clock_in_time >= start,
            models.ClockSession.clock_in_time <  end,
        )
        .order_by(models.ClockSession.clock_in_time.asc())
        .all()
    )

    # Approved leaves overlapping the target date — scoped to this org's users
    leaves = (
        db.query(models.LeaveRequest)
        .filter(
            models.LeaveRequest.user_id.in_(user_ids),
            models.LeaveRequest.from_date <= datetime.combine(target_date, time.max, tzinfo=timezone.utc),
            models.LeaveRequest.to_date   >= datetime.combine(target_date, time.min, tzinfo=timezone.utc),
        )
        .all()
    )

    # First clock-in / last clock-out per user for this date
    by_user: dict[int, list] = defaultdict(list)
    for s in sessions:
        by_user[s.user_id].append(s)

    # Active manual time adjustments for this day, keyed by user_id. Folded
    # into worked totals below so "worked time the tracker lost" (power
    # cuts, system crashes) is reflected in this report — see
    # app/_time_adjustments.py for the source of truth.
    adj_by_user = adjustment_seconds_for_day_by_user(db, user_ids, target_date)

    worked, late, on_leave, absent = [], [], [], []

    for u in users:
        u_sessions = by_user.get(u.id, [])
        leave_info = _is_on_leave(u.id, target_date, leaves)
        adj_secs   = int(adj_by_user.get(u.id, 0))

        if u_sessions:
            first = min(u_sessions, key=lambda s: s.clock_in_time)
            last  = max(
                (s for s in u_sessions if s.clock_out_time),
                key=lambda s: s.clock_out_time,
                default=None,
            )

            cin_dt  = first.clock_in_time
            cout_dt = last.clock_out_time if last else None

            # _session_seconds handles closed AND in-progress sessions; summing
            # raw total_seconds silently dropped anyone still clocked in, so a
            # worker mid-shift showed 0 hours next to a colleague who had gone
            # home.
            session_secs = sum(_session_seconds(s, now_utc) for s in u_sessions)
            total_secs = session_secs + adj_secs

            row = {
                **_user_dict(u),
                "date":             target_date.isoformat(),
                "clock_in":         _iso(cin_dt),
                "clock_out":        _iso(cout_dt),
                "clock_out_text":   _iso(cout_dt) or "Working On",
                "duration":         _fmt_duration(total_secs),
                "duration_secs":    total_secs,
                "tracker_secs":     session_secs,
                "adjustment_secs":  adj_secs,
                "is_late":          _is_late(cin_dt, _smap.get((u.id, target_date))),
                "sessions_count":   len(u_sessions),
            }
            worked.append(row)
            if row["is_late"]:
                late.append(row)
        elif adj_secs > 0:
            # No clock sessions at all but admin credited work-time — treat
            # as worked (full-day outage scenario). No clock_in/out fields
            # because the tracker never captured any.
            worked.append({
                **_user_dict(u),
                "date":            target_date.isoformat(),
                "clock_in":        None,
                "clock_out":       None,
                "clock_out_text":  "Manual entry",
                "duration":        _fmt_duration(adj_secs),
                "duration_secs":   adj_secs,
                "tracker_secs":    0,
                "adjustment_secs": adj_secs,
                "is_late":         False,
                "sessions_count":  0,
            })
        elif leave_info:
            on_leave.append({
                **_user_dict(u),
                "date":         target_date.isoformat(),
                **leave_info,
            })
        else:
            absent.append({
                **_user_dict(u),
                "date": target_date.isoformat(),
            })

    return _rpt_cache_set(current_org.id, _ck, {
        "date":     target_date.isoformat(),
        "totals": {
            "worked":   len(worked),
            "late":     len(late),
            "on_leave": len(on_leave),
            "absent":   len(absent),
            "total":    len(users),
        },
        "worked":   worked,
        "late":     late,
        "on_leave": on_leave,
        "absent":   absent,
    })


# ──────────────────────────────────────────────────────────────────────
# 2.  MONTHLY ATTENDANCE
#     Per-day per-user attendance grid for a given month
# ──────────────────────────────────────────────────────────────────────
@router.get("/monthly-attendance")
def monthly_attendance(
    month: Optional[str] = Query(None, description="YYYY-MM (default = current month)"),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    first, last = _parse_month(month, db, current_org.id)
    days_in_month = (last - first).days + 1
    # The month runs from local midnight to local midnight for THIS
    # organization. These were UTC calendar bounds while `today` two lines below
    # was already the org's date, so for a tenant 5½ hours ahead of UTC every
    # session started between 00:00 and 05:30 local on the 1st fell into the
    # previous month — and the same window on the 1st of the NEXT month was
    # counted in this one. `range_end` is now the exclusive next-local-midnight
    # instant, hence `<` rather than `<=` at the query below.
    range_start, _ = _day_window(db, current_org.id, first)
    _, range_end   = _day_window(db, current_org.id, last)
    # LeaveRequest.from_date / to_date are naive `DateTime` columns —
    # comparing them against aware datetimes raises on PostgreSQL and
    # silently filters everything out. Use naive bounds for the leave
    # query so payroll-approved leaves actually surface here.
    leave_range_start = datetime.combine(first, time.min)
    leave_range_end   = datetime.combine(last,  time.max)
    now_utc     = datetime.now(timezone.utc)
    # "Today" decides is_future / today_pending below, i.e. whether a day is
    # counted absent. It must be the organization's date, not the server's.
    today       = _tz_local_date(db, current_org.id)

    users_q = db.query(models.User).filter(models.User.organization_id == current_org.id)
    if user_id:
        users_q = users_q.filter(models.User.id == user_id)
    # Drop the seeded system Administrator (CEO seat) — it never clocks
    # in and inflates the absent column for every month. Other admin-role
    # users are still included because they may genuinely be working.
    users = exclude_system_admin(users_q.all(), db)
    user_ids = [u.id for u in users]

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id.in_(user_ids),
            models.ClockSession.clock_in_time >= range_start,
            models.ClockSession.clock_in_time <  range_end,
        )
        .all()
    )
    # Pull every leave that overlaps the month for these users, then filter
    # status in Python so we can split "counts for payroll" vs "still
    # pending" without two near-identical SQL queries.
    leaves_all = (
        db.query(models.LeaveRequest)
        .filter(
            models.LeaveRequest.user_id.in_(user_ids),
            models.LeaveRequest.from_date <= leave_range_end,
            models.LeaveRequest.to_date   >= leave_range_start,
        )
        .all()
    )
    leaves         = [l for l in leaves_all if l.status in PAYROLL_LEAVE_STATUSES]
    pending_leaves = [l for l in leaves_all if l.status in ("submitted", "under_review")]

    # Public holidays falling in this month — excluded from working /
    # absent / leave counters so payroll never deducts them.
    # Scoped to current_org so one org's holiday calendar cannot corrupt
    # another org's payroll calculations.
    # Via the shared org calendar: one definition of which holidays apply to
    # this organization (including the country scope), and column-scoped so an
    # un-migrated database cannot fail the whole report on an unknown column.
    from app.holidays import org_calendar as _org_calendar
    holiday_dates = _org_calendar.holiday_names_by_date(db, current_org.id, first, last)

    # ── Working-day source (P10b) ────────────────────────────────────────────
    # Engine ON: each employee's own working days, holidays and expected hours,
    # so a Mon-Sat org's Saturdays count and a part-timer is not measured against
    # a full-timer's day. Engine OFF: the weekday()/holidays rules below.
    #
    # Prefetched ONCE for every user and every day in the month — five queries
    # regardless of size (see schedule_policy.resolver.build_context). Wiring the
    # per-call resolver here would have been ~12,000 queries for a 59-user month.
    _sched_by_day: dict = {}
    try:
        from app.schedule_policy.resolver import resolve_work_schedules_over
        _month_dates = [first + timedelta(days=i) for i in range(days_in_month)]
        _sched_by_day = resolve_work_schedules_over(db, users, _month_dates)
    except Exception:
        _rpt_log.warning("monthly schedule prefetch failed org=%s; working-day "
                         "counts will use the holiday calendar only",
                         current_org.id, exc_info=True)
        _sched_by_day = {}

    rows = []
    for u in users:
        # Joining date — drives "start counting attendance from here".
        # official_start_date is the first working day; fall back to
        # created_at for legacy users that pre-date that column.
        joining_d = None
        if u.official_start_date:
            joining_d = u.official_start_date.date()
        elif u.created_at:
            joining_d = u.created_at.date()

        per_day = {}
        u_sessions = [s for s in sessions if s.user_id == u.id]
        # Track presence by date (any clock-in counts, even if the worker
        # is still active and total_seconds hasn't been finalized yet)
        # and worked seconds per date separately so live sessions still
        # contribute to Total Hours.
        u_session_dates: set[str] = set()
        u_session_secs:  dict[str, int] = defaultdict(int)
        for s in u_sessions:
            if not s.clock_in_time:
                continue
            d_key = s.clock_in_time.date().isoformat()
            u_session_dates.add(d_key)
            u_session_secs[d_key] += _session_seconds(s, now_utc)

        u_leaves         = [l for l in leaves         if l.user_id == u.id]
        u_pending_leaves = [l for l in pending_leaves if l.user_id == u.id]

        working_days   = 0
        present_days   = 0
        leave_days     = 0.0
        absent_days    = 0
        total_secs     = 0
        pending_leave_days = 0.0
        leave_breakdown: dict[str, float] = defaultdict(float)
        pending_leave_types: set[str] = set()

        for i in range(days_in_month):
            d = first + timedelta(days=i)
            d_iso = d.isoformat()
            has_session = d_iso in u_session_dates
            secs        = u_session_secs.get(d_iso, 0)
            _sd = _sched_by_day.get(d, {}).get(u.id) if _sched_by_day else None
            if _sd is not None:
                is_holiday = _sd.is_holiday
                is_weekend = not _sd.is_working_day and not is_holiday
            else:
                # No resolved schedule (structural failure, already logged).
                # Fall back to the org's holiday calendar only. A `weekday() >= 5`
                # guess here would silently write a Mon-Sat organization's
                # Saturdays out of `working_days`, and that number feeds payroll.
                is_weekend     = False
                is_holiday     = d in holiday_dates
            is_before_join = joining_d is not None and d < joining_d
            is_future      = d > today
            is_today       = d == today

            # Days that don't count toward any payroll bucket
            if is_before_join:
                per_day[d_iso] = "before_joining"
                continue
            if is_weekend:
                per_day[d_iso] = "weekend"
                continue
            if is_holiday:
                per_day[d_iso] = "holiday"
                continue
            if is_future:
                per_day[d_iso] = "future"
                continue

            # Weekday, post-join, not a holiday, not in the future → counts.
            working_days += 1

            # Presence detection — ANY clock-in record (active or
            # completed) counts as present. Total Hours picks up
            # in-progress seconds via _session_seconds so today's
            # ongoing work is reflected immediately.
            if has_session:
                present_days += 1
                total_secs   += secs
                per_day[d_iso] = "present"
                continue

            # Approved / locked leave covering this date.
            day_leave = None
            for l in u_leaves:
                f = l.from_date.date() if l.from_date else None
                t = l.to_date.date()   if l.to_date   else f
                if f and t and f <= d <= t:
                    day_leave = l
                    break

            if day_leave is not None:
                lt = day_leave.leave_type or "Other"
                credit = 0.5 if getattr(day_leave, "is_half_day", False) else 1.0
                leave_days += credit
                leave_breakdown[lt] += credit
                per_day[d_iso] = "leave"
                continue

            # Today, no session yet, no approved leave — the day isn't
            # over so we DON'T classify as absent. Marking someone absent
            # at 10am because they haven't clocked in yet would corrupt
            # payroll. Keep the day in working_days so the math stays
            # consistent; the UI can render this as "pending" if needed.
            if is_today:
                per_day[d_iso] = "today_pending"
                continue

            # Still-pending leave request covering a past date —
            # absent for now (payroll-grade), but surfaced separately
            # so admins know an approval flip will clear it.
            day_pending = None
            for l in u_pending_leaves:
                f = l.from_date.date() if l.from_date else None
                t = l.to_date.date()   if l.to_date   else f
                if f and t and f <= d <= t:
                    day_pending = l
                    break

            if day_pending is not None:
                pending_leave_days += 0.5 if getattr(day_pending, "is_half_day", False) else 1.0
                pending_leave_types.add(day_pending.leave_type or "Other")
                absent_days += 1
                per_day[d_iso] = "absent_pending_leave"
            else:
                absent_days += 1
                per_day[d_iso] = "absent"

        rows.append({
            **_user_dict(u),
            "joining_date":         joining_d.isoformat() if joining_d else None,
            "working_days":         working_days,
            "present_days":         present_days,
            "leave_days":           leave_days,
            "absent_days":          absent_days,
            "total_seconds":        total_secs,
            "total_duration":       _fmt_duration(total_secs),
            "leave_breakdown":      dict(leave_breakdown),
            "pending_leave_days":   pending_leave_days,
            "pending_leave_types":  sorted(pending_leave_types),
            "per_day":              per_day,
        })

    # Aggregate, distinct list of leave types actually used by approved
    # requests this month — lets the UI render a legend / filter that
    # reflects real data instead of a hard-coded enum.
    leave_types_used = sorted({(l.leave_type or "Other") for l in leaves})

    return {
        "month":         first.strftime("%Y-%m"),
        "first_date":    first.isoformat(),
        "last_date":     last.isoformat(),
        "days_in_month": days_in_month,
        "holidays": [{"date": d.isoformat(), "name": n}
                     for d, n in sorted(holiday_dates.items())],
        "leave_types_used": leave_types_used,
        "rows":          rows,
    }


# ──────────────────────────────────────────────────────────────────────
# 3.  MONTHLY IN/OUT  — first clock-in & last clock-out per day per user
# ──────────────────────────────────────────────────────────────────────
@router.get("/monthly-inout")
def monthly_inout(
    month: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    first, last = _parse_month(month, db, current_org.id)
    # Organization-local month, exclusive upper bound — see the daily-attendance
    # report above. UTC bounds moved sessions between months for every tenant
    # not on UTC.
    range_start, _ = _day_window(db, current_org.id, first)
    _, range_end   = _day_window(db, current_org.id, last)

    users_q = db.query(models.User).filter(models.User.organization_id == current_org.id)
    if user_id:
        users_q = users_q.filter(models.User.id == user_id)
    # Always strip the seeded Administrator (CEO seat) so attendance
    # aggregates, exports, and payroll-grade reports never include it.
    users = exclude_system_admin(users_q.all(), db)
    user_ids = [u.id for u in users]

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id.in_(user_ids),
            models.ClockSession.clock_in_time >= range_start,
            models.ClockSession.clock_in_time <  range_end,
        )
        .order_by(models.ClockSession.clock_in_time.asc())
        .all()
    )

    by_user_day: dict[tuple[int, str], dict] = {}
    for s in sessions:
        d = s.clock_in_time.date().isoformat()
        key = (s.user_id, d)
        if key not in by_user_day:
            by_user_day[key] = {
                "first_in":  s.clock_in_time,
                "last_out":  s.clock_out_time,
                "total_secs": s.total_seconds or 0,
            }
        else:
            entry = by_user_day[key]
            if s.clock_in_time < entry["first_in"]:
                entry["first_in"] = s.clock_in_time
            if s.clock_out_time and (entry["last_out"] is None or s.clock_out_time > entry["last_out"]):
                entry["last_out"] = s.clock_out_time
            entry["total_secs"] += s.total_seconds or 0

    user_lookup = {u.id: u for u in users}
    rows = []
    for (uid, d), entry in sorted(by_user_day.items(), key=lambda x: (x[0][1], x[0][0])):
        u = user_lookup.get(uid)
        if not u:
            continue
        rows.append({
            **_user_dict(u),
            "date":      d,
            "clock_in":  _iso(entry["first_in"]),
            "clock_out": _iso(entry["last_out"]),
            "duration":  _fmt_duration(entry["total_secs"]),
        })

    return {"month": first.strftime("%Y-%m"), "rows": rows}


# ──────────────────────────────────────────────────────────────────────
# 4.  LATE CLOCK-IN
# ──────────────────────────────────────────────────────────────────────
@router.get("/late-clockin")
def late_clockin(
    date: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    target_date = _parse_date(date, db, current_org.id)
    start, end  = _day_window(db, current_org.id, target_date)

    sessions_q = db.query(models.ClockSession).filter(
        models.ClockSession.organization_id == current_org.id,
        models.ClockSession.clock_in_time   >= start,
        models.ClockSession.clock_in_time   <  end,
    )
    if user_id:
        sessions_q = sessions_q.filter(models.ClockSession.user_id == user_id)
    sessions = sessions_q.all()

    users = {
        u.id: u for u in
        db.query(models.User).filter(models.User.organization_id == current_org.id).all()
    }

    _smap = _sched_map(db, current_org.id, users.values(), [target_date])

    rows = []
    seen: set = set()
    for s in sorted(sessions, key=lambda x: x.clock_in_time):
        if s.user_id in seen:
            continue  # only first session per user
        seen.add(s.user_id)
        if not _is_late(s.clock_in_time, _smap.get((s.user_id, target_date))):
            continue
        u = users.get(s.user_id)
        if not u:
            continue
        _sc = _smap.get((s.user_id, target_date))
        scheduled = datetime.combine(
            target_date, _sc.start_local, tzinfo=_zone_of(_sc),
        ).astimezone(timezone.utc)
        late_secs = max(0, int((s.clock_in_time - scheduled).total_seconds()))
        rows.append({
            **_user_dict(u),
            "date":         target_date.isoformat(),
            "clock_in":     _iso(s.clock_in_time),
            "scheduled":    _sc.start_local.strftime("%H:%M"),
            "late_by":      _fmt_duration(late_secs),
            "late_seconds": late_secs,
        })

    return {"date": target_date.isoformat(), "rows": rows, "total": len(rows)}


# ──────────────────────────────────────────────────────────────────────
# 5.  OVER TIME REPORT
# ──────────────────────────────────────────────────────────────────────
@router.get("/overtime")
def overtime(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date:   Optional[str] = Query(None, alias="to"),
    user_id:   Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    today = _tz_local_date(db, current_org.id)
    start_d = _parse_date(from_date, db, current_org.id) if from_date else (today - timedelta(days=6))
    end_d   = _parse_date(to_date, db, current_org.id)   if to_date   else today
    # Organization-local date range, exclusive upper bound — the same rule the
    # daily report and payroll already use. UTC bounds put a session started
    # before local dawn (or after local dusk) on the wrong day for every tenant
    # that is not on UTC.
    range_start, _ = _day_window(db, current_org.id, start_d)
    _, range_end   = _day_window(db, current_org.id, end_d)

    users_q = db.query(models.User).filter(models.User.organization_id == current_org.id)
    if user_id:
        users_q = users_q.filter(models.User.id == user_id)
    # Always strip the seeded Administrator (CEO seat) so attendance
    # aggregates, exports, and payroll-grade reports never include it.
    users = exclude_system_admin(users_q.all(), db)
    user_ids = [u.id for u in users]

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id.in_(user_ids),
            models.ClockSession.clock_in_time >= range_start,
            models.ClockSession.clock_in_time <  range_end,
        )
        .all()
    )

    now_utc = datetime.now(timezone.utc).replace(tzinfo=timezone.utc)

    # Aggregate per user/day using the ORGANIZATION-local date, deducting breaks
    per_user_day: dict[tuple[int, str], int] = defaultdict(int)
    for s in sessions:
        cin_aware = s.clock_in_time
        d = _org_date_of(db, current_org.id, cin_aware).isoformat()

        per_user_day[(s.user_id, d)] += _session_seconds(s, now_utc)

    user_lookup = {u.id: u for u in users}

    rows = []
    # Overtime starts past THIS employee's expected daily hours — the same
    # number the clock-out gate and the risk engine use. Resolved once per user
    # rather than per user-day.
    #
    # Resolved for the whole organization in ONE batched prefetch. This loop
    # used to call the per-user resolve_expected_work_seconds, which re-ran the
    # entire settings cascade against the database for every employee — the
    # same N+1 that made /reports/attendance-detail take ~94 s. Measured here at
    # 748 statements and ~106 s for 60 employees. `_sched_map` is the batched
    # helper the rest of this file already uses; it applies identical precedence
    # and is grouped by organization_id, so tenants stay isolated.
    _ot_threshold: dict = {}
    try:
        _ot_smap = _sched_map(db, current_org.id, users, [today])
        for _uid, _u in user_lookup.items():
            _sc = _ot_smap.get((_uid, today))
            _secs = int(getattr(_sc, "expected_work_seconds", 0) or 0) if _sc else 0
            if not _secs:
                # Degraded path only: the batched resolve failed or omitted this
                # user. Confined to the rows the batch could not cover.
                try:
                    from app.attendance_policy import resolve_expected_work_seconds
                    _secs = resolve_expected_work_seconds(db, _u, today)
                except Exception:
                    _secs = None
            if _secs and _secs > 0:
                _ot_threshold[_uid] = _secs
    except Exception:
        _ot_threshold = {}

    user_totals: dict[int, dict] = defaultdict(lambda: {"days": 0, "ot_secs": 0, "worked_secs": 0})
    for (uid, d), secs in per_user_day.items():
        if secs <= 0:
            continue
        u = user_lookup.get(uid)
        if not u:
            continue
        _limit = _ot_threshold.get(uid)
        if not _limit:
            # No resolvable expected hours for this person -> no overtime claim
            # can be made. Substituting a global 8h here is what produced
            # overtime rows for part-timers and contractors.
            continue
        ot = max(0, secs - _limit)
        user_totals[uid]["days"] += 1
        user_totals[uid]["worked_secs"] += secs
        user_totals[uid]["ot_secs"]     += ot
        if ot > 0:
            rows.append({
                **_user_dict(u),
                "date":           d,
                "worked":         _fmt_duration(secs),
                "worked_seconds": secs,
                "overtime":       _fmt_duration(ot),
                "overtime_secs":  ot,
            })

    summary = [
        {
            **_user_dict(user_lookup[uid]),
            "days_worked":     v["days"],
            "total_worked":    _fmt_duration(v["worked_secs"]),
            "total_overtime":  _fmt_duration(v["ot_secs"]),
            "overtime_secs":   v["ot_secs"],
        }
        for uid, v in user_totals.items()
    ]
    summary.sort(key=lambda r: -r["overtime_secs"])
    rows.sort(key=lambda r: r["date"], reverse=True)

    return {
        "from":    start_d.isoformat(),
        "to":      end_d.isoformat(),
        "rows":    rows,
        "summary": summary,
    }


# ──────────────────────────────────────────────────────────────────────
# 5b. NEW JOINERS  —  how many employees joined within a date range
# ──────────────────────────────────────────────────────────────────────
@router.get("/new-joiners")
def new_joiners(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date:   Optional[str] = Query(None, alias="to"),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    today = _tz_local_date(db, current_org.id)
    start_d = _parse_date(from_date, db, current_org.id) if from_date else (today - timedelta(days=30))
    end_d   = _parse_date(to_date, db, current_org.id)   if to_date   else today
    # official_start_date is a NAIVE DateTime column (stored as
    # datetime.combine(date, min.time())), so bound the filter with naive
    # datetimes too — comparing against tz-aware bounds errors on Postgres.
    range_start = datetime.combine(start_d, time.min)
    range_end   = datetime.combine(end_d, time.max)

    users_q = db.query(models.User).filter(
        models.User.organization_id == current_org.id,
        models.User.official_start_date.isnot(None),
        models.User.official_start_date >= range_start,
        models.User.official_start_date <= range_end,
    )
    users = exclude_system_admin(users_q.all(), db)
    users.sort(key=lambda u: u.official_start_date)

    rows = [
        {**_user_dict(u), "joined_on": u.official_start_date.date().isoformat()}
        for u in users
    ]
    return {
        "from":  start_d.isoformat(),
        "to":    end_d.isoformat(),
        "count": len(rows),
        "rows":  rows,
    }


# ──────────────────────────────────────────────────────────────────────
# 5c. EMPLOYEE DIRECTORY  —  full active roster (name, id, role, type, join)
# ──────────────────────────────────────────────────────────────────────
@router.get("/employee-directory")
def employee_directory(
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    users_q = db.query(models.User).filter(
        models.User.organization_id == current_org.id,
        models.User.is_active == True,  # noqa: E712 — current employees only
    )
    users = exclude_system_admin(users_q.all(), db)
    users.sort(key=lambda u: (u.name or "").lower())
    rows = [
        {
            **_user_dict(u),
            "joined_on": u.official_start_date.date().isoformat()
                          if u.official_start_date else None,
        }
        for u in users
    ]
    return {"count": len(rows), "rows": rows}


# ──────────────────────────────────────────────────────────────────────
# 6.  CUSTOM REPORT  —  flexible date-range / per-employee breakdown
# ──────────────────────────────────────────────────────────────────────
@router.get("/custom")
def custom_report(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date:   Optional[str] = Query(None, alias="to"),
    user_id:   Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    # Anchors in-progress sessions for _session_seconds(). Referenced
    # below but never defined -> NameError, so this endpoint 500'd.
    now_utc = datetime.now(timezone.utc)
    today = _tz_local_date(db, current_org.id)
    start_d = _parse_date(from_date, db, current_org.id) if from_date else (today - timedelta(days=6))
    end_d   = _parse_date(to_date, db, current_org.id)   if to_date   else today
    # Organization-local date range, exclusive upper bound — the same rule the
    # daily report and payroll already use. UTC bounds put a session started
    # before local dawn (or after local dusk) on the wrong day for every tenant
    # that is not on UTC.
    range_start, _ = _day_window(db, current_org.id, start_d)
    _, range_end   = _day_window(db, current_org.id, end_d)

    users_q = db.query(models.User).filter(models.User.organization_id == current_org.id)
    if user_id:
        users_q = users_q.filter(models.User.id == user_id)
    # Always strip the seeded Administrator (CEO seat) so attendance
    # aggregates, exports, and payroll-grade reports never include it.
    users = exclude_system_admin(users_q.all(), db)
    user_ids = [u.id for u in users]

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id.in_(user_ids),
            models.ClockSession.clock_in_time >= range_start,
            models.ClockSession.clock_in_time <  range_end,
        )
        .all()
    )

    user_stats: dict[int, dict] = {
        u.id: {
            **_user_dict(u),
            "days_worked":  0,
            "total_seconds": 0,
            "break_seconds": 0,
            "active_seconds": 0,
            "first_clock_in":  None,
            "last_clock_out":  None,
        } for u in users
    }

    days_present: dict[int, set] = defaultdict(set)
    for s in sessions:
        st = user_stats.get(s.user_id)
        if not st:
            continue
        st["total_seconds"] += _session_seconds(s, now_utc)
        st["break_seconds"] += s.break_seconds or 0
        days_present[s.user_id].add(
            _org_date_of(db, current_org.id, s.clock_in_time).isoformat())
        if not st["first_clock_in"] or s.clock_in_time < st["first_clock_in"]:
            st["first_clock_in"] = s.clock_in_time
        if s.clock_out_time and (not st["last_clock_out"] or s.clock_out_time > st["last_clock_out"]):
            st["last_clock_out"] = s.clock_out_time

    for uid, st in user_stats.items():
        st["days_worked"]    = len(days_present.get(uid, set()))
        # total_seconds is ALREADY net of breaks and offline time (clock-out
        # writes elapsed - break - offline). Subtracting break_seconds again
        # here removed every break twice.
        st["active_seconds"] = st["total_seconds"]
        st["total_duration"]  = _fmt_duration(st["total_seconds"])
        st["break_duration"]  = _fmt_duration(st["break_seconds"])
        st["active_duration"] = _fmt_duration(st["active_seconds"])
        st["first_clock_in"]  = _iso(st["first_clock_in"])
        st["last_clock_out"]  = _iso(st["last_clock_out"])

    return {
        "from": start_d.isoformat(),
        "to":   end_d.isoformat(),
        "rows": list(user_stats.values()),
    }


# ──────────────────────────────────────────────────────────────────────
# 7.  APPS & SITES USAGE
# ──────────────────────────────────────────────────────────────────────
@router.get("/apps-sites")
def apps_sites_usage(
    date: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    _ck = f"apps-sites:{date}:{user_id}"
    _hit = _rpt_cache_get(current_org.id, _ck)
    if _hit is not None:
        return _hit
    target = _parse_date(date, db, current_org.id)
    start, end = _day_window(db, current_org.id, target)

    q = db.query(models.Activity).filter(
        models.Activity.organization_id == current_org.id,
        models.Activity.timestamp       >= start,
        models.Activity.timestamp       <  end,
        models.Activity.app_name.isnot(None),
    )
    if user_id:
        q = q.filter(models.Activity.user_id == user_id)
    activities = q.order_by(models.Activity.timestamp.asc()).all()

    # aggregate per (user_id, app_name)
    bucket: dict[tuple, dict] = {}
    last_ts: dict[tuple, datetime] = {}
    for a in activities:
        key = (a.user_id, a.app_name)
        b = bucket.setdefault(key, {
            "user_id": a.user_id,
            "app_name": a.app_name,
            "total_seconds": 0,
            "key_presses":   0,
            "mouse_clicks":  0,
            "samples":       0,
        })
        if key in last_ts:
            diff = int((a.timestamp - last_ts[key]).total_seconds())
            if 0 < diff < 300:
                b["total_seconds"] += diff
        last_ts[key] = a.timestamp
        b["key_presses"]  += a.key_presses  or 0
        b["mouse_clicks"] += a.mouse_clicks or 0
        b["samples"]      += 1

    # Load only the users that actually appear in the aggregated activity,
    # not the entire User table. On large deployments most users have no
    # activity for a given day, so the old `.all()` loaded thousands of rows
    # to use a handful.
    needed_ids = {uid for (uid, _app) in bucket.keys() if uid is not None}
    users = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(needed_ids)).all()}
        if needed_ids else {}
    )
    rows = []
    for (uid, app), data in bucket.items():
        u = users.get(uid)
        rows.append({
            "user_id":      uid,
            "name":         u.name if u else "Unknown",
            "team":         u.team if u else None,
            "app_name":     app,
            "duration":     _fmt_duration(data["total_seconds"]),
            "duration_secs": data["total_seconds"],
            "key_presses":  data["key_presses"],
            "mouse_clicks": data["mouse_clicks"],
        })
    rows.sort(key=lambda r: -r["duration_secs"])
    return _rpt_cache_set(current_org.id, _ck, {"date": target.isoformat(), "rows": rows})


# ──────────────────────────────────────────────────────────────────────
# 8.  WORK ACTIVITY LOG
# ──────────────────────────────────────────────────────────────────────
@router.get("/work-activity")
def work_activity(
    date: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    target = _parse_date(date, db, current_org.id)
    start, end = _day_window(db, current_org.id, target)

    rows = []

    _report_user_ids = [
        row.id for row in db.query(models.User.id)
        .filter(models.User.organization_id == current_org.id)
        .all()
    ]
    sessions_q = db.query(models.ClockSession).filter(
        models.ClockSession.organization_id == current_org.id,
        models.ClockSession.clock_in_time   >= start,
        models.ClockSession.clock_in_time   <  end,
    )
    breaks_q = db.query(models.BreakSession).filter(
        models.BreakSession.user_id.in_(_report_user_ids),
        models.BreakSession.break_start     >= start,
        models.BreakSession.break_start     <  end,
    )
    if user_id:
        sessions_q = sessions_q.filter(models.ClockSession.user_id == user_id)
        breaks_q   = breaks_q.filter(models.BreakSession.user_id   == user_id)

    sessions = sessions_q.all()
    breaks   = breaks_q.all()

    # Only fetch the users referenced by this day's sessions/breaks rather
    # than the whole User table (which the rows below would mostly discard).
    needed_ids = {s.user_id for s in sessions} | {b.user_id for b in breaks}
    needed_ids.discard(None)
    users = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(needed_ids)).all()}
        if needed_ids else {}
    )

    for s in sessions:
        u = users.get(s.user_id)
        if not u:
            continue
        rows.append({
            **_user_dict(u),
            "type":      "clock_in",
            "label":     "Clocked In",
            "timestamp": _iso(s.clock_in_time),
        })
        if s.clock_out_time:
            rows.append({
                **_user_dict(u),
                "type":      "clock_out",
                "label":     "Clocked Out",
                "timestamp": _iso(s.clock_out_time),
                "duration":  _fmt_duration(s.total_seconds or 0),
            })

    for b in breaks:
        u = users.get(b.user_id)
        if not u:
            continue
        rows.append({
            **_user_dict(u),
            "type":      "break_start",
            "label":     "Break Started",
            "timestamp": _iso(b.break_start),
        })
        if b.break_end:
            rows.append({
                **_user_dict(u),
                "type":      "break_end",
                "label":     "Resumed Work",
                "timestamp": _iso(b.break_end),
                "duration":  _fmt_duration(b.break_seconds or 0),
                "reason":    b.reason or "",
            })

    rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return {"date": target.isoformat(), "rows": rows}


# ──────────────────────────────────────────────────────────────────────
# 9.  WORK LOAD ANALYSIS
# ──────────────────────────────────────────────────────────────────────
@router.get("/workload")
def workload_analysis(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date:   Optional[str] = Query(None, alias="to"),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    today = _tz_local_date(db, current_org.id)
    start_d = _parse_date(from_date, db, current_org.id) if from_date else (today - timedelta(days=6))
    end_d   = _parse_date(to_date, db, current_org.id)   if to_date   else today
    # Organization-local date range, exclusive upper bound — the same rule the
    # daily report and payroll already use. UTC bounds put a session started
    # before local dawn (or after local dusk) on the wrong day for every tenant
    # that is not on UTC.
    range_start, _ = _day_window(db, current_org.id, start_d)
    _, range_end   = _day_window(db, current_org.id, end_d)

    days = (end_d - start_d).days + 1
    # Per-employee expected is computed inside the loop below (part-time 4h/day
    # vs full-time 8h45m/day). No extra queries — the user rows are already
    # loaded for this report.
    from app.attendance_policy import (
        default_expected_seconds_for_type, resolve_expected_work_seconds,
    )

    def _expected_per_day_for(u) -> int:
        """Required seconds per day for one employee, from the canonical
        resolver. The employment-type default is only the last-resort guard if
        resolution itself fails."""
        try:
            secs = resolve_expected_work_seconds(db, u, today)
            if secs and secs > 0:
                return secs
        except Exception:
            _rpt_log.warning("utilization expected-hours resolve failed user=%s",
                             getattr(u, "id", None), exc_info=True)
        return default_expected_seconds_for_type(u.employment_type)

    # Aggregate worked/break seconds per user in the database (GROUP BY) rather
    # than loading every session row into Python and summing. This is
    # mathematically identical to the previous loop — SQL SUM ignores NULLs
    # exactly as the `or 0` did — but avoids shipping the full session table
    # to the app for wide date ranges. Org-scoped via the filter below.
    agg_rows = (
        db.query(
            models.ClockSession.user_id.label("uid"),
            func.coalesce(func.sum(models.ClockSession.total_seconds), 0).label("worked"),
            func.coalesce(func.sum(models.ClockSession.break_seconds), 0).label("breaks"),
        )
        .filter(
            models.ClockSession.organization_id == current_org.id,
            models.ClockSession.clock_in_time   >= range_start,
            models.ClockSession.clock_in_time   <  range_end,
        )
        .group_by(models.ClockSession.user_id)
        .all()
    )

    # Load only the users that actually appear in the aggregate (not the whole
    # User table — most users have no sessions in a given range).
    needed_ids = {r.uid for r in agg_rows if r.uid is not None}
    users = (
        {u.id: u for u in db.query(models.User).filter(models.User.id.in_(needed_ids)).all()}
        if needed_ids else {}
    )

    # ONE batched schedule resolve for every employee in this report across the
    # whole range. _expected_seconds_over resolves per call, so calling it from
    # the loop below rebuilt the prefetch context once per employee — 692
    # statements and ~98 s for 57 employees, the same class of N+1 that made
    # /reports/attendance-detail slow. The summing rule still lives in
    # _expected_seconds_over; only the resolve is hoisted.
    _wl_days = []
    _d = start_d
    while _d <= end_d:
        _wl_days.append(_d)
        _d += timedelta(days=1)
    _wl_smap = _sched_map(db, current_org.id, list(users.values()), _wl_days)

    rows = []
    for r in agg_rows:
        u = users.get(r.uid)
        if not u:
            continue
        worked_secs = int(r.worked or 0)
        break_secs  = int(r.breaks or 0)
        active = max(0, worked_secs - break_secs)
        # Summed over the days this employee is SCHEDULED to work, not over
        # calendar days: multiplying by `days` measured a Mon-Fri worker
        # against a seven-day week, so a full 40h read as 71% utilised and got
        # bucketed "low".
        from app.intelligence.risk_engine import _expected_seconds_over
        # Pass the prefetched rows only when the batch actually covered THIS
        # employee; otherwise None, so _expected_seconds_over resolves for
        # itself exactly as before. Handing it a map of all-None would make it
        # sum to zero and silently report 0% utilisation instead of degrading.
        _u_sched = ({d: _wl_smap.get((u.id, d)) for d in _wl_days}
                    if (u.id, start_d) in _wl_smap else None)
        expected_secs = _expected_seconds_over(db, u, start_d, end_d,
                                               sched_by_date=_u_sched)
        utilization = round((worked_secs / expected_secs) * 100, 1) if expected_secs else 0
        rows.append({
            **_user_dict(u),
            "worked_secs":   worked_secs,
            "break_secs":    break_secs,
            "expected_secs": expected_secs,
            "active_secs":      active,
            "worked_duration":  _fmt_duration(worked_secs),
            "active_duration":  _fmt_duration(active),
            "break_duration":   _fmt_duration(break_secs),
            "expected_duration": _fmt_duration(expected_secs),
            "utilization":      utilization,  # %
            "load":             "high" if utilization >= 100 else "normal" if utilization >= 75 else "low",
        })
    rows.sort(key=lambda r: -r["worked_secs"])

    return {
        "from": start_d.isoformat(),
        "to":   end_d.isoformat(),
        "days": days,
        "rows": rows,
    }


# ──────────────────────────────────────────────────────────────────────
# 10. SHIFT TIME REPORT
# ──────────────────────────────────────────────────────────────────────
@router.get("/shift-time")
def shift_time(
    date: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    target = _parse_date(date, db, current_org.id)
    start, end = _day_window(db, current_org.id, target)

    sessions_q = db.query(models.ClockSession).filter(
        models.ClockSession.organization_id == current_org.id,
        models.ClockSession.clock_in_time   >= start,
        models.ClockSession.clock_in_time   <  end,
    )
    if user_id:
        sessions_q = sessions_q.filter(models.ClockSession.user_id == user_id)
    sessions = sessions_q.order_by(models.ClockSession.clock_in_time.asc()).all()

    users = {
        u.id: u for u in
        db.query(models.User).filter(models.User.organization_id == current_org.id).all()
    }
    _smap = _sched_map(db, current_org.id, users.values(),
                       sorted({_org_date_of(db, current_org.id, x.clock_in_time)
                               for x in sessions}))

    rows = []
    for s in sessions:
        u = users.get(s.user_id)
        if not u:
            continue
        late_secs  = 0
        early_out  = 0
        _sc = _smap.get((s.user_id, _org_date_of(db, current_org.id, s.clock_in_time)))
        # Scheduled in/out come from THIS employee's schedule. There is no
        # module-level 09:30 / 18:30 default any more: an employee with no
        # configured shift simply has no scheduled times, and is neither late
        # nor early.
        _sched_in  = _sc.start_local if _sc is not None else None
        _sched_out = _sc.end_local if _sc is not None else None
        _zone = _zone_of(_sc) if _sc is not None else timezone.utc
        if _sched_in is not None and _is_late(s.clock_in_time, _sc):
            scheduled_dt = datetime.combine(
                target, _sched_in, tzinfo=_zone).astimezone(timezone.utc)
            late_secs = max(0, int((s.clock_in_time - scheduled_dt).total_seconds()))
        if s.clock_out_time and _sched_out is not None:
            sched_out_dt = datetime.combine(
                target, _sched_out, tzinfo=_zone).astimezone(timezone.utc)
            if s.clock_out_time < sched_out_dt:
                early_out = int((sched_out_dt - s.clock_out_time).total_seconds())
        rows.append({
            **_user_dict(u),
            "date":           target.isoformat(),
            "scheduled_in":   _sched_in.strftime("%H:%M") if _sched_in else None,
            "scheduled_out":  _sched_out.strftime("%H:%M") if _sched_out else None,
            "actual_in":      _iso(s.clock_in_time),
            "actual_out":     _iso(s.clock_out_time),
            "late_by":        _fmt_duration(late_secs),
            "early_out_by":   _fmt_duration(early_out),
            "shift_duration": _fmt_duration(s.total_seconds or 0),
        })
    return {"date": target.isoformat(), "rows": rows}


# ──────────────────────────────────────────────────────────────────────
# 11. SUMMARY REPORT
#     A consolidated per-employee snapshot for a date range:
#       — Days present / absent / leave / late
#       — Total worked / break / active hours
#       — Avg work hours per day
#       — On-time arrival %
#       — Last clock in/out
# ──────────────────────────────────────────────────────────────────────
@router.get("/summary")
def summary_report(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date:   Optional[str] = Query(None, alias="to"),
    user_id:   Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    # Anchors in-progress sessions for _session_seconds(). Referenced
    # below but never defined -> NameError, so this endpoint 500'd.
    now_utc = datetime.now(timezone.utc)
    today = _tz_local_date(db, current_org.id)
    start_d = _parse_date(from_date, db, current_org.id) if from_date else today.replace(day=1)
    end_d   = _parse_date(to_date, db, current_org.id)   if to_date   else today
    if end_d < start_d:
        raise HTTPException(status_code=400, detail="`to` must be on or after `from`")

    # Organization-local date range, exclusive upper bound — the same rule the
    # daily report and payroll already use. UTC bounds put a session started
    # before local dawn (or after local dusk) on the wrong day for every tenant
    # that is not on UTC.
    range_start, _ = _day_window(db, current_org.id, start_d)
    _, range_end   = _day_window(db, current_org.id, end_d)
    total_days  = (end_d - start_d).days + 1

    users_q = db.query(models.User).filter(models.User.organization_id == current_org.id)
    if user_id:
        users_q = users_q.filter(models.User.id == user_id)
    # Always strip the seeded Administrator (CEO seat) so attendance
    # aggregates, exports, and payroll-grade reports never include it.
    users = exclude_system_admin(users_q.all(), db)
    _smap = _sched_map(db, current_org.id, users,
                       [start_d + timedelta(days=k) for k in range((end_d - start_d).days + 1)])
    user_ids = [u.id for u in users]

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id.in_(user_ids),
            models.ClockSession.clock_in_time >= range_start,
            models.ClockSession.clock_in_time <  range_end,
        )
        .order_by(models.ClockSession.clock_in_time.asc())
        .all()
    )
    leaves = (
        db.query(models.LeaveRequest)
        .filter(
            models.LeaveRequest.user_id.in_(user_ids),
            # LeaveRequest.from_date / to_date are naive DATE-at-midnight
            # columns, so they get calendar bounds rather than the session
            # window's UTC instants — the same split the monthly report makes.
            # Reusing range_start/range_end here would have shifted the leave
            # overlap test by the organization's UTC offset when those bounds
            # became organization-local.
            models.LeaveRequest.from_date <= datetime.combine(end_d, time.max),
            models.LeaveRequest.to_date   >= datetime.combine(start_d, time.min),
        )
        .all()
    )

    rows = []
    company_totals = {
        "total_employees": len(users),
        "total_worked":    0,
        "total_break":     0,
        "total_active":    0,
        "total_present":   0,
        "total_absent":    0,
        "total_leave":     0,
        "total_late":      0,
    }

    for u in users:
        u_sessions = [s for s in sessions if s.user_id == u.id]

        days_present_set: set = set()
        late_days        = 0
        worked_secs      = 0
        break_secs       = 0
        first_in         = None
        last_out         = None

        # Group by the ORGANIZATION-local date. Using the raw UTC date put a
        # 01:00-local clock-in on the previous day for eastern tenants.
        per_day: dict[str, list] = defaultdict(list)
        for s in u_sessions:
            per_day[_org_date_of(db, current_org.id, s.clock_in_time).isoformat()].append(s)

        for d_iso, day_sessions in per_day.items():
            days_present_set.add(d_iso)
            day_first = min(day_sessions, key=lambda s: s.clock_in_time).clock_in_time
            _d0 = _org_date_of(db, current_org.id, day_first)
            if _is_late(day_first, _smap.get((u.id, _d0))):
                late_days += 1
            for s in day_sessions:
                worked_secs += _session_seconds(s, now_utc)
                break_secs  += s.break_seconds or 0
                if not first_in or s.clock_in_time < first_in:
                    first_in = s.clock_in_time
                if s.clock_out_time and (not last_out or s.clock_out_time > last_out):
                    last_out = s.clock_out_time

        # leave days inside the range for this user
        leave_day_count = 0
        for d in range(total_days):
            day = start_d + timedelta(days=d)
            if _is_on_leave(u.id, day, leaves):
                leave_day_count += 1

        days_present = len(days_present_set)
        # absent = workdays in range up to today minus present minus leave
        cutoff = min(end_d, today)
        # This employee's own working days — so a Mon-Sat organization counts
        # Saturdays, a Sun-Thu organization counts Sundays, and holidays are
        # excluded. No Mon-Fri assumption: when the schedule is unknown the day
        # is not counted rather than guessed, because this number feeds
        # days_absent and therefore payroll.
        def _counts(d0):
            _sc = _smap.get((u.id, d0))
            return bool(_sc.is_working_day) if _sc is not None else False

        weekdays_in_range = sum(
            1 for k in range(total_days)
            if (start_d + timedelta(days=k)) <= cutoff and _counts(start_d + timedelta(days=k))
        )
        days_absent = max(0, weekdays_in_range - days_present - leave_day_count)

        # worked_secs is already net of breaks (see _worked.py) — the old
        # `worked - break` here deducted every break a second time.
        active_secs    = worked_secs
        avg_per_day    = round((worked_secs / days_present) / 3600, 2) if days_present else 0
        on_time_pct    = round(((days_present - late_days) / days_present) * 100, 1) if days_present else 0

        company_totals["total_worked"]  += worked_secs
        company_totals["total_break"]   += break_secs
        company_totals["total_active"]  += active_secs
        company_totals["total_present"] += days_present
        company_totals["total_absent"]  += days_absent
        company_totals["total_leave"]   += leave_day_count
        company_totals["total_late"]    += late_days

        rows.append({
            **_user_dict(u),
            "days_present":    days_present,
            "days_absent":     days_absent,
            "days_leave":      leave_day_count,
            "days_late":       late_days,
            "total_worked":    _fmt_duration(worked_secs),
            "worked_seconds":  worked_secs,
            "total_break":     _fmt_duration(break_secs),
            "break_seconds":   break_secs,
            "total_active":    _fmt_duration(active_secs),
            "active_seconds":  active_secs,
            "avg_hours_per_day": avg_per_day,
            "on_time_pct":     on_time_pct,
            "first_clock_in":  _iso(first_in),
            "last_clock_out":  _iso(last_out),
        })
    rows.sort(key=lambda r: -r["worked_seconds"])

    return {
        "from":     start_d.isoformat(),
        "to":       end_d.isoformat(),
        "days":     total_days,
        "company":  {
            **company_totals,
            "total_worked_duration": _fmt_duration(company_totals["total_worked"]),
            "total_break_duration":  _fmt_duration(company_totals["total_break"]),
            "total_active_duration": _fmt_duration(company_totals["total_active"]),
        },
        "rows":     rows,
    }


# ──────────────────────────────────────────────────────────────────────
# 12. INPUT ACTIVITY REPORT
# ──────────────────────────────────────────────────────────────────────
@router.get("/input-activity")
def input_activity(
    date: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    target = _parse_date(date, db, current_org.id)
    start, end = _day_window(db, current_org.id, target)

    # Pull rows ordered by (user, timestamp) so we can compute idle time from
    # per-user timestamp deltas. We do NOT sum Activity.idle_seconds — that
    # column carries the OS watchdog "consecutive idle seconds" counter,
    # which grows monotonically while the user is idle. Summing it across
    # rows wildly over-counts. See /api/app-analytics for the same fix.
    SAMPLE_GAP_MAX      = 60 * 60   # 1h — anything larger is a session gap
    SAMPLE_GAP_FALLBACK = 30        # used for the first sample of the day
    CLICKS_CAP = 600
    KEYS_CAP   = 1500

    q = db.query(models.Activity).filter(
        models.Activity.organization_id == current_org.id,
        models.Activity.timestamp       >= start,
        models.Activity.timestamp       <  end,
    )
    if user_id:
        q = q.filter(models.Activity.user_id == user_id)
    acts = q.order_by(
        models.Activity.user_id.asc(),
        models.Activity.timestamp.asc(),
    ).all()

    per_user = {}        # uid → {clicks, keys, idle, active, samples}
    prev_ts  = {}        # uid → last timestamp seen for that user
    for a in acts:
        uid = a.user_id
        if uid is None:
            continue
        bucket = per_user.setdefault(uid, {
            "clicks":  0, "keys":    0,
            "idle":    0, "active":  0,
            "samples": 0,
        })
        prev = prev_ts.get(uid)
        if prev is None:
            delta = SAMPLE_GAP_FALLBACK
        else:
            diff = int((a.timestamp - prev).total_seconds())
            delta = diff if 0 < diff <= SAMPLE_GAP_MAX else 0
        prev_ts[uid] = a.timestamp

        bucket["clicks"]  += max(0, min(int(a.mouse_clicks or 0), CLICKS_CAP))
        bucket["keys"]    += max(0, min(int(a.key_presses  or 0), KEYS_CAP))
        bucket["samples"] += 1
        if a.is_active:
            bucket["active"] += delta
        else:
            bucket["idle"]   += delta

    users = {
        u.id: u for u in
        db.query(models.User).filter(models.User.organization_id == current_org.id).all()
    }

    rows = []
    for uid, b in per_user.items():
        u = users.get(uid)
        if not u:
            continue
        rows.append({
            **_user_dict(u),
            "date":          target.isoformat(),
            "key_presses":   b["keys"],
            "mouse_clicks":  b["clicks"],
            "idle_seconds":  b["idle"],
            "idle_duration": _fmt_duration(b["idle"]),
            "samples":       b["samples"],
            "total_inputs":  b["keys"] + b["clicks"],
        })
    rows.sort(key=lambda r: -r["total_inputs"])
    return {"date": target.isoformat(), "rows": rows}


# ──────────────────────────────────────────────────────────────────────
# 13. LOGIN IP REPORT
# ──────────────────────────────────────────────────────────────────────
@router.get("/login-ip")
def login_ip_report(
    user_id: Optional[int] = Query(None),
    db:      Session       = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    """
    Aggregate distinct login IP addresses per user.

    Reads from the `audit_logs` table — every successful login writes a row
    with entity='User' action='UserLogin' and the requesting client IP.
    """
    uq = db.query(models.User).filter(models.User.organization_id == current_org.id)
    if user_id:
        uq = uq.filter(models.User.id == user_id)
    users = uq.all()
    org_user_ids = [u.id for u in users]

    q = db.query(models.AuditLog).filter(
        models.AuditLog.entity    == "User",
        models.AuditLog.action    == "UserLogin",
        models.AuditLog.ip_address.isnot(None),
        models.AuditLog.actor_id.in_(org_user_ids),
    )
    rows_logs = q.all()

    by_user: dict[int, set] = defaultdict(set)
    last_seen: dict[int, datetime] = {}
    for log in rows_logs:
        if not log.actor_id or not log.ip_address:
            continue
        by_user[log.actor_id].add(log.ip_address)
        if log.actor_id not in last_seen or log.created_at > last_seen[log.actor_id]:
            last_seen[log.actor_id] = log.created_at

    # Collect all unique IPs across all users for batch geolocation
    all_ips: set = set()
    for ips_set in by_user.values():
        all_ips.update(ips_set)

    # Batch geolocate using ip-api.com (free, no key, up to 100/batch)
    geo_cache: dict = {}
    ip_list = [ip for ip in all_ips if ip and ip not in ("127.0.0.1", "::1")]
    try:
        for i in range(0, len(ip_list), 100):
            batch = ip_list[i:i+100]
            resp = _requests.post(
                "http://ip-api.com/batch?fields=query,city,regionName,country,isp,status",
                json=batch,
                timeout=5,
            )
            if resp.status_code == 200:
                for item in resp.json():
                    if item.get("status") == "success":
                        geo_cache[item["query"]] = {
                            "city":    item.get("city", ""),
                            "region":  item.get("regionName", ""),
                            "country": item.get("country", ""),
                            "isp":     item.get("isp", ""),
                        }
    except Exception:
        pass  # Geolocation is best-effort; never block the report

    rows = []
    for u in users:
        ips = sorted(by_user.get(u.id, set()))
        ip_details = [
            {"ip": ip, **geo_cache.get(ip, {})}
            for ip in ips
        ]
        rows.append({
            **_user_dict(u),
            "number_of_ips": len(ips),
            "ip_addresses":  ips,
            "ip_details":    ip_details,
            "last_login":    _iso(last_seen.get(u.id)),
        })
    rows.sort(key=lambda r: r["name"].lower())
    return {"rows": rows}


# ──────────────────────────────────────────────────────────────────────
# ATTENDANCE DETAIL  GET /reports/attendance-detail
# Returns per-employee first-in, last-out, total work/break seconds,
# and live status for a single IST calendar day.
# ──────────────────────────────────────────────────────────────────────
from datetime import timezone as _tz_type
from app.commercial.enforcement import shadow_capability as _shadow

@router.get("/attendance-detail")
def attendance_detail(
    date_str: Optional[str] = Query(None, alias="date"),
    user_id:  Optional[int] = Query(None),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
    current_org: models.Organization = Depends(get_current_organization),
):
    # SHADOW ONLY - reports.standard is granted to every plan today; this
    # hook exists to size the blast radius if that ever changes.
    _shadow(db, viewer, "reports.standard",
            route="GET /reports/attendance-detail")
    target_date = _parse_date(date_str, db, current_org.id)

    # Organization-local calendar day → UTC window
    utc_start, utc_end = _day_window(db, current_org.id, target_date)
    now_utc    = datetime.now(timezone.utc)

    users = (
        db.query(models.User)
        .filter(models.User.organization_id == current_org.id, models.User.is_active == True)
        .all()
    )
    if user_id:
        users = [u for u in users if u.id == user_id]

    # Fetch all sessions in the IST day window in one query
    all_sessions = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.organization_id == current_org.id,
            models.ClockSession.clock_in_time   >= utc_start,
            models.ClockSession.clock_in_time   <  utc_end,
        )
    ).all()
    sessions_by_user: dict = defaultdict(list)
    for s in all_sessions:
        sessions_by_user[s.user_id].append(s)

    # Currently-open breaks — bound to a still-active (unclosed) session for
    # this IST day. Without these guards, a stale orphan break (break_end
    # never set on an old session) would pin the user as "On Break" forever.
    open_session_ids_today = {
        s.id for s in all_sessions if s.clock_out_time is None
    }
    open_breaks = set()
    if open_session_ids_today:
        open_breaks = {
            b.user_id
            for b in db.query(models.BreakSession).filter(
                and_(
                    models.BreakSession.break_end.is_(None),
                    models.BreakSession.is_active == True,  # noqa: E712
                    models.BreakSession.session_id.in_(open_session_ids_today),
                )
            ).all()
        }

    # Approved leaves covering this date — scoped to this org's users
    org_user_ids = [u.id for u in users]
    leaves = db.query(models.LeaveRequest).filter(
        and_(
            models.LeaveRequest.user_id.in_(org_user_ids),
            models.LeaveRequest.status == "approved",
            models.LeaveRequest.from_date <= target_date,
            models.LeaveRequest.to_date   >= target_date,
        )
    ).all()
    on_leave_ids = {l.user_id for l in leaves}

    # Required worked seconds per employee for THIS date, resolved server-side.
    # The admin dashboard's history view used to divide by a duplicated 8h/4h
    # table in frontend/src/lib/attendancePolicy.js; it now renders this instead,
    # so the progress bars cannot disagree with the clock-out gate.
    #
    # Resolved for the WHOLE organization in ONE batched prefetch, not once per
    # employee. This line used to call resolve_expected_work_seconds(db, u, ...)
    # from the record loop below, which re-entered resolve_work_schedule WITHOUT
    # a prefetched context — so every employee re-ran the entire settings
    # cascade against the database: three scope-tier reads, a ShiftAssignment
    # lookup, a holiday lookup and an org-setting read, each of the last three
    # additionally wrapped in its own SAVEPOINT/RELEASE round trip by
    # `_safe_load`. Measured on the 57-employee production workspace that was
    # 705 of the endpoint's 710 SQL statements — the attendance data itself is
    # only 5 — and ~94 s wall, essentially all of it network round trips.
    # Selecting Yesterday on the admin dashboard sat on that for the full 94 s.
    #
    # `_sched_map` is the same batched helper every other report in this file
    # already uses (resolve_work_schedules_over -> build_context, ~5 queries for
    # the whole org, grouped by organization_id so tenants stay isolated). It
    # applies the identical precedence rules — verified equal to the per-user
    # path for all 57 users across today/yesterday/7d/30d/month/year boundaries
    # and both weekend days.
    _exp_smap = _sched_map(db, current_org.id, users, [target_date])

    def _expected_for(u):
        sched = _exp_smap.get((u.id, target_date))
        if sched is not None:
            secs = getattr(sched, "expected_work_seconds", None)
            if secs and secs > 0:
                return int(secs)
        # Degraded path only: the batched resolve failed or omitted this user.
        # Same answer as before, still per-user, but now confined to the rows
        # the batch could not cover instead of every row.
        try:
            from app.attendance_policy import resolve_expected_work_seconds
            return resolve_expected_work_seconds(db, u, target_date)
        except Exception:
            return None

    records = []
    for u in users:
        user_sessions = sorted(sessions_by_user.get(u.id, []),
                               key=lambda s: s.clock_in_time)

        if not user_sessions:
            records.append({
                **_user_dict(u),
                "first_clock_in":      None,
                "last_clock_out":      None,
                "total_work_seconds":  0,
                "total_break_seconds": 0,
                "status":              "On Leave" if u.id in on_leave_ids else "Absent",
                "sessions_count":      0,
                "expected_seconds":    _expected_for(u),
            })
            continue

        first_cin  = user_sessions[0].clock_in_time
        last_cout  = None
        all_closed = True
        for s in user_sessions:
            if s.clock_out_time is None:
                all_closed = False
            elif last_cout is None or s.clock_out_time > last_cout:
                last_cout = s.clock_out_time

        total_break = sum(s.break_seconds or 0 for s in user_sessions)

        # Completed sessions contribute total_seconds; active session adds live elapsed
        # One definition for closed and open sessions alike (app/_worked.py);
        # the open-session branch here used to forget offline_seconds.
        total_work = sum(_session_seconds(s, now_utc) for s in user_sessions)

        if not all_closed:
            status = "On Break" if u.id in open_breaks else "Working"
        else:
            status = "Clocked Out"

        records.append({
            **_user_dict(u),
            "first_clock_in":      _iso(first_cin),
            "last_clock_out":      _iso(last_cout),
            "total_work_seconds":  total_work,
            "total_break_seconds": total_break,
            "status":              status,
            "sessions_count":      len(user_sessions),
            "expected_seconds":    _expected_for(u),
        })

    status_order = {"Working": 0, "On Break": 1, "Clocked Out": 2, "On Leave": 3, "Absent": 4}
    records.sort(key=lambda r: (status_order.get(r["status"], 5), (r["name"] or "").lower()))

    present     = [r for r in records if r["status"] not in ("Absent", "On Leave")]
    absent      = [r for r in records if r["status"] == "Absent"]
    on_leave    = [r for r in records if r["status"] == "On Leave"]
    working     = [r for r in records if r["status"] == "Working"]
    on_break    = [r for r in records if r["status"] == "On Break"]
    clocked_out = [r for r in records if r["status"] == "Clocked Out"]

    return {
        "date": target_date.isoformat(),
        "summary": {
            "total":       len(records),
            "present":     len(present),
            "absent":      len(absent),
            "on_leave":    len(on_leave),
            "working":     len(working),
            "on_break":    len(on_break),
            "clocked_out": len(clocked_out),
        },
        "records": records,
    }


# ──────────────────────────────────────────────────────────────────────
# CALENDAR HEATMAP  GET /reports/calendar-heatmap
# Returns per-day attendance counts for a given month so the admin can
# render a color-coded calendar without N per-day queries.
# ──────────────────────────────────────────────────────────────────────
@router.get("/calendar-heatmap")
def calendar_heatmap(
    month: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
    current_org: models.Organization = Depends(get_current_organization),
):
    # SHADOW ONLY - one of the eight capabilities the spec tiers to
    # Governed+ but the shipped manifest still grants to everyone.
    _shadow(db, viewer, "reports.advanced",
            route="GET /reports/calendar-heatmap")
    first_day, last_day = _parse_month(month, db, current_org.id)

    utc_start, _ = _day_window(db, current_org.id, first_day)
    _, utc_end   = _day_window(db, current_org.id, last_day)

    total_employees = (
        db.query(models.User)
        .filter(models.User.organization_id == current_org.id, models.User.is_active == True)
        .count()
    )

    # One query: all sessions in the month window
    rows = db.query(
        models.ClockSession.user_id,
        models.ClockSession.clock_in_time,
    ).filter(
        and_(
            models.ClockSession.organization_id == current_org.id,
            models.ClockSession.clock_in_time   >= utc_start,
            models.ClockSession.clock_in_time   <= utc_end,
        )
    ).all()

    by_date: dict = defaultdict(set)   # org-local date_str -> set of user_ids
    for r in rows:
        by_date[_org_date_of(db, current_org.id, r.clock_in_time).isoformat()].add(r.user_id)

    # The org's configured working days and holidays drive the non-working
    # shading. One prefetch for the whole range.
    _cal_dates = []
    _c = first_day
    while _c <= last_day:
        _cal_dates.append(_c)
        _c += timedelta(days=1)
    _cal_map = _sched_map(db, current_org.id,
                          [models.User(id=0, organization_id=current_org.id)],
                          _cal_dates)

    days = []
    cur = first_day
    while cur <= last_day:
        ds       = cur.isoformat()
        present  = len(by_date.get(ds, set()))
        _csc     = _cal_map.get((0, cur))
        is_wknd  = (not _csc.is_working_day) if _csc is not None else False
        days.append({
            "date":      ds,
            "present":   present,
            "absent":    max(0, total_employees - present),
            "total":     total_employees,
            "is_weekend": is_wknd,
        })
        cur += timedelta(days=1)

    return {
        "month":            first_day.strftime("%Y-%m"),
        "total_employees":  total_employees,
        "days":             days,
    }


# ──────────────────────────────────────────────────────────────────────
# BREAK GOVERNANCE REPORT
# Per-break rows with the spec-required fields plus aggregate time buckets
# (Working / Short Break / Lunch Break / Stopped Working / Overrun). Reads
# the snapshot columns written by the governance service so the numbers are
# stable even if policy changes after the fact.
# ──────────────────────────────────────────────────────────────────────
@router.get("/break-governance")
def break_governance_report(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date:   Optional[str] = Query(None, alias="to"),
    user_id:   Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    from app.breaks import governance as gov

    today = _tz_local_date(db, current_org.id)
    start_d = _parse_date(from_date, db, current_org.id) if from_date else (today - timedelta(days=6))
    end_d   = _parse_date(to_date, db, current_org.id)   if to_date   else today
    # Organization-local date range, exclusive upper bound — see above.
    range_start, _ = _day_window(db, current_org.id, start_d)
    _, range_end   = _day_window(db, current_org.id, end_d)

    users_q = db.query(models.User)
    if user_id:
        users_q = users_q.filter(models.User.id == user_id)
    users = {u.id: u for u in exclude_system_admin(users_q.all(), db)}
    user_ids = list(users.keys())
    if not user_ids:
        return {"from": start_d.isoformat(), "to": end_d.isoformat(), "rows": [], "totals": {}}

    sessions = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id.in_(user_ids),
            models.ClockSession.clock_in_time >= range_start,
            models.ClockSession.clock_in_time <  range_end,
        )
        .all()
    )
    breaks = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.user_id.in_(user_ids),
            models.BreakSession.break_start >= range_start,
            models.BreakSession.break_start <  range_end,
        )
        .order_by(models.BreakSession.break_start.asc())
        .all()
    )

    now = datetime.now(timezone.utc)

    # Aggregate buckets across the whole range (seconds).
    totals = {
        "working_seconds":      0,
        "short_break_seconds":  0,
        "lunch_break_seconds":  0,
        "stopped_seconds":      0,   # clocked-out gaps are implicit; we report total break vs work
        "overrun_seconds":      0,
    }
    for s in sessions:
        # Already net of breaks; the subtraction here removed them twice.
        totals["working_seconds"] += _session_seconds(s, datetime.now(timezone.utc))

    rows = []
    for b in breaks:
        u = users.get(b.user_id)
        if u is None:
            continue
        btype = b.break_type or "break"
        allowed_min = b.allowed_minutes or gov.allowance_minutes(db, btype, u.organization_id)
        start = b.break_start if b.break_start.tzinfo else b.break_start.replace(tzinfo=timezone.utc)
        if b.break_end:
            actual_secs = b.break_seconds or 0
        else:
            actual_secs = max(0, int((now - start).total_seconds()))  # ongoing break
        overrun_secs = max(0, actual_secs - allowed_min * 60)

        if btype == "lunch":
            totals["lunch_break_seconds"] += actual_secs
        else:
            totals["short_break_seconds"] += actual_secs
        totals["overrun_seconds"] += overrun_secs

        rows.append({
            **_user_dict(u),
            "break_id":                  b.id,
            "worker_type":               b.worker_type or gov.normalize_worker_type(u),
            "break_type":                btype,
            "status":                    b.status or ("active" if b.is_active else "ended"),
            "break_start":               _iso(start),
            "break_end":                 _iso(b.break_end),
            "expected_duration_minutes": allowed_min,
            "actual_duration_minutes":   round(actual_secs / 60, 2),
            "overrun_minutes":           round(overrun_secs / 60, 2),
            "overrun_flag":              bool(b.overrun_flag or overrun_secs > 0),
            "overrun_notification_sent": bool(b.overrun_notification_sent),
            "alert_count":               b.alert_count or 0,
            "reason":                    b.reason,
        })

    return {
        "from": start_d.isoformat(),
        "to":   end_d.isoformat(),
        "rows": rows,
        "totals": {
            **totals,
            "working_time":     _fmt_duration(totals["working_seconds"]),
            "short_break_time": _fmt_duration(totals["short_break_seconds"]),
            "lunch_break_time": _fmt_duration(totals["lunch_break_seconds"]),
            "overrun_time":     _fmt_duration(totals["overrun_seconds"]),
        },
    }
