"""
Leave review policy — per-organization, through the standard settings cascade.
=============================================================================
Two HR-review heuristics were hard-coded in ``leave/router.py``:

  * **Sick-leave frequency**: ``sl_count >= 2`` in a calendar month flagged the
    request for HR review. Two is one company's tolerance, not a universal one.
  * **Monday/Friday pattern**: a request starting on a Monday or ending on a
    Friday was flagged as suspected long-weekend abuse. That is only meaningful
    for a Mon–Fri week — for a Mon–Sat tenant Friday is an ordinary mid-week
    day, and for a Sun–Thu week the flag fires on the wrong days entirely.

Both are now settings, and the pattern check is expressed in terms of the
employee's RESOLVED working days rather than fixed weekday numbers: the rule is
"the leave abuts a non-working day", which is what the Monday/Friday test was
approximating for one particular week shape.

Defaults reproduce the previous behaviour for a Mon–Fri organization exactly.

Only keys and resolution live here. The flagging stays in ``leave/router.py``;
there is no second leave engine.
"""
from __future__ import annotations

from typing import Dict

from sqlalchemy.orm import Session

SETTING_SICK_REVIEW_PER_MONTH = "leave.sick_review_threshold_per_month"
SETTING_ADJACENT_REVIEW = "leave.flag_adjacent_to_non_working_day"
SETTING_YEAR_START_MONTH = "leave.year_start_month"
SETTING_EXCLUDE_NON_WORKING = "leave.exclude_non_working_days"

SETTING_DEFAULTS: Dict[str, str] = {
    # 0 disables the check.
    SETTING_SICK_REVIEW_PER_MONTH: "2",
    SETTING_ADJACENT_REVIEW: "true",
    # 1 = January, reproducing the previous hard-coded calendar year exactly.
    SETTING_YEAR_START_MONTH: "1",
    # See `leave_day_cost` below for why this defaults ON.
    SETTING_EXCLUDE_NON_WORKING: "true",
}

SETTING_DESCRIPTIONS: Dict[str, str] = {
    SETTING_SICK_REVIEW_PER_MONTH:
        "Number of sick-leave requests in a calendar month that triggers an HR "
        "review flag. Set to 0 to disable the check.",
    SETTING_ADJACENT_REVIEW:
        "Flag a leave request for HR review when it starts or ends next to a "
        "non-working day (the long-weekend pattern), using this employee's "
        "configured working days.",
    SETTING_YEAR_START_MONTH:
        "Month the leave year begins (1-12). Entitlements, accrual pacing and "
        "used-days totals all reset on the 1st of this month. 1 = calendar "
        "year; 4 = an April-March leave year.",
    SETTING_EXCLUDE_NON_WORKING:
        "Charge leave only for days the employee would actually have worked. "
        "Public holidays, weekends and roster off-days inside a leave span do "
        "not consume the employee's balance. Turn off to charge every calendar "
        "day in the range.",
}

#: A leave span longer than this is not costed day-by-day — see `leave_day_cost`.
MAX_COSTED_LEAVE_DAYS = 366


def _effective(db: Session, user, keys=None) -> Dict[str, str]:
    from app.settings import scope as settings_scope
    wanted = {k: SETTING_DEFAULTS[k] for k in (keys or SETTING_DEFAULTS.keys())}
    try:
        return settings_scope.resolve_effective(db, user, wanted)
    except Exception:
        return dict(wanted)


def sick_review_threshold(db: Session, user) -> int:
    """Requests per month that trigger HR review; 0 means "never flag"."""
    raw = _effective(db, user, [SETTING_SICK_REVIEW_PER_MONTH]).get(
        SETTING_SICK_REVIEW_PER_MONTH, "2")
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        n = int(SETTING_DEFAULTS[SETTING_SICK_REVIEW_PER_MONTH])
    return max(0, n)


def flags_adjacent_leave(db: Session, user) -> bool:
    raw = _effective(db, user, [SETTING_ADJACENT_REVIEW]).get(
        SETTING_ADJACENT_REVIEW, "true")
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def excludes_non_working_days(db: Session, user) -> bool:
    raw = _effective(db, user, [SETTING_EXCLUDE_NON_WORKING]).get(
        SETTING_EXCLUDE_NON_WORKING, "true")
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def _raw_span_days(from_date, to_date) -> float:
    """The old rule: every calendar day in the range, minimum one."""
    return float(max(1, (to_date - from_date).days + 1))


def leave_day_cost(db: Session, user, from_date, to_date, *, is_half_day: bool = False) -> float:
    """How many days of balance a leave request consumes.

    This was ``max(1, (to - from).days + 1)`` — a raw calendar span, duplicated
    at the validation gate and again at the apply handler. Public holidays,
    weekends and roster off-days inside a span were all charged to the
    employee, so leave was the ONE consumer of the holiday calendar that
    ignored it: attendance, the schedule resolver, reports and payroll all
    already resolved working days properly. An employee taking Mon-Fri across
    Diwali was debited five days instead of four.

    The count now comes from the same Schedule Resolver those other consumers
    use, so it automatically respects:

      * the organization's configured working days (a Mon-Sat or Sun-Thu week),
      * the holiday calendar, country-scoped via ``holidays.country_code``,
      * team and employee schedule overrides,
      * roster off-days from shift assignments.

    Half-day requests stay 0.5 and never consult the resolver: the apply handler
    already forces ``from == to`` for them, and a half-day on a working day is
    0.5 by definition.

    Failure and edge behaviour, all deliberately biased toward the previous
    number rather than a cheaper one:

      * setting off                → raw span
      * span over a year           → raw span (not worth 366 resolver rows)
      * resolver raises            → raw span
      * resolver returns no data   → raw span
      * every day is non-working   → **0.0**, which is correct: an employee owes
        nothing for days they were not going to work. Callers must accept 0.

    Stored results are not affected retroactively: ``leave_days`` is persisted
    per request and read back from the column, so existing approved leave keeps
    the cost it was granted at.
    """
    if is_half_day:
        return 0.5

    # Call sites pass a mix of `date` and `datetime` (the apply handler parses
    # to datetime, the validation gate hands us `.date()`). Normalise here so
    # neither has to remember, and so the day keys match what the resolver
    # returns.
    from datetime import datetime as _dt
    if isinstance(from_date, _dt):
        from_date = from_date.date()
    if isinstance(to_date, _dt):
        to_date = to_date.date()

    if to_date < from_date:
        return _raw_span_days(from_date, from_date)

    span = (to_date - from_date).days + 1
    if span > MAX_COSTED_LEAVE_DAYS:
        return _raw_span_days(from_date, to_date)
    if not excludes_non_working_days(db, user):
        return _raw_span_days(from_date, to_date)

    from datetime import timedelta
    try:
        from app.schedule_policy.resolver import resolve_work_schedules_over
        days = [from_date + timedelta(days=i) for i in range(span)]
        by_date = resolve_work_schedules_over(db, [user], days)

        working = 0
        resolved = 0
        for day in days:
            schedule = (by_date.get(day) or {}).get(user.id)
            if schedule is None:
                continue
            resolved += 1
            if schedule.is_working_day:
                working += 1

        # Partial resolution would undercount. Only trust a full answer.
        if resolved != span:
            return _raw_span_days(from_date, to_date)
        return float(working)
    except Exception:
        # A resolver problem must never make leave cheaper than it was before.
        return _raw_span_days(from_date, to_date)


def abuts_non_working_day(db: Session, user, from_date, to_date) -> bool:
    """True when the day before the leave, or the day after it, is a day this
    employee would not have worked anyway.

    This is the generalisation of the old ``weekday() == 0 or weekday() == 4``
    test: it asks the Schedule Resolver, so it follows the organization's
    working days, holiday calendar and any team/employee override.
    """
    from datetime import timedelta
    try:
        from app.schedule_policy.resolver import resolve_work_schedules_over
        before, after = from_date - timedelta(days=1), to_date + timedelta(days=1)
        by_date = resolve_work_schedules_over(db, [user], [before, after])
        for d in (before, after):
            sched = (by_date.get(d) or {}).get(user.id)
            if sched is not None and not sched.is_working_day:
                return True
        return False
    except Exception:
        # A resolver problem must not block a leave request; the flag is
        # advisory, so its absence is the safe failure.
        return False


# ── Leave year ────────────────────────────────────────────────────────────
def year_start_month(db: Session, org_id) -> int:
    """The month a leave year begins in for this organization (1-12)."""
    from app.settings import scope as settings_scope
    raw = "1"
    try:
        raw = settings_scope.org_settings(
            db, org_id, {SETTING_YEAR_START_MONTH: SETTING_DEFAULTS[SETTING_YEAR_START_MONTH]},
        ).get(SETTING_YEAR_START_MONTH, "1")
    except Exception:
        pass
    try:
        m = int(str(raw).strip())
    except (TypeError, ValueError):
        m = 1
    return m if 1 <= m <= 12 else 1


def leave_year_start(db: Session, org_id):
    """First instant of the leave year that the organization is currently in.

    Was `datetime(datetime.now(timezone.utc).year, 1, 1)`, copied verbatim in
    three places: a hard-coded January boundary read on the UTC clock. An
    April-March or July-June leave year could not be expressed at all, and near
    New Year the year flipped at the wrong moment for tenants away from UTC.
    """
    from datetime import datetime
    from app import _tz as _tzmod

    today = _tzmod.local_date(db, org_id)
    m = year_start_month(db, org_id)
    year = today.year if today.month >= m else today.year - 1
    return datetime(year, m, 1)


def describe_leave_year(year_start) -> dict:
    """The leave year beginning at ``year_start``, for display.

    ``end`` is the last day the year covers (twelve months on, minus a day).
    ``label`` is the plain year for a January start and the month range
    otherwise ("Apr 2026 – Mar 2027"), so a reviewer sees which period the
    balance figures belong to.
    """
    import calendar
    from datetime import date

    start = year_start.date() if hasattr(year_start, "date") else year_start
    last_month_index = start.year * 12 + (start.month - 1) + 11
    end_year, end_month = divmod(last_month_index, 12)
    end_month += 1
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    label = (str(start.year) if start.month == 1
             else f"{start:%b %Y} – {end:%b %Y}")
    return {"start": start.isoformat(), "end": end.isoformat(), "label": label}


def months_into_leave_year(db: Session, org_id) -> int:
    """How many months of the current leave year have started (1-12).

    Drives accrual pacing. It used to be `datetime.now(timezone.utc).month`,
    which is only correct when the leave year starts in January.
    """
    from app import _tz as _tzmod
    today = _tzmod.local_date(db, org_id)
    m = year_start_month(db, org_id)
    return ((today.month - m) % 12) + 1


# ── Annual-leave joining-date proration (ZT-WFM-LEAVE-001 §4.2) ──────────────
def prorated_annual_entitlement(
    *, start, leave_year_start_dt, monthly_rate, annual_cap, end=None,
) -> float:
    """Joining-date-prorated annual entitlement for one leave year.

    The approved calendar-day rule: for each month the employee is eligible
    within the leave year, credit ``monthly_rate × (eligible calendar days in
    that month ÷ actual days in that month)``, summed across months. A complete
    calendar month therefore contributes exactly ``monthly_rate`` (e.g. 1.25),
    and a partial joining/leaving month is prorated by its inclusive eligible
    days. February automatically uses its real length (28/29). The result never
    exceeds ``annual_cap`` — a full leave year sums to exactly the cap.

    Boundaries are inclusive dates, resolved against the org's leave year:
      • ``start``               – the employee's first eligible day (joining date)
      • ``end``                 – last eligible day if they left; else year-end
      • ``leave_year_start_dt`` – first day of the org's current leave year,
                                  from :func:`leave_year_start`

    Returns 0.0 when the eligible window is empty (e.g. a joining date after the
    leave year has ended). Pure arithmetic — no DB, no clock — so it is fully
    deterministic and unit-testable. ``date``/``datetime`` inputs both accepted.
    """
    from datetime import date, datetime, timedelta
    import calendar

    def _as_date(x):
        return x.date() if isinstance(x, datetime) else x

    ys = _as_date(leave_year_start_dt)
    # The leave year runs [ys, ys + 1 year); the last eligible day is the day
    # before the same month/day next year (Jan-1 start → Dec 31; Apr-1 → Mar 31).
    year_end = date(ys.year + 1, ys.month, ys.day) - timedelta(days=1)

    s = _as_date(start)
    e = _as_date(end) if end is not None else year_end

    eligible_start = max(s, ys)
    eligible_end = min(e, year_end)
    if eligible_start > eligible_end:
        return 0.0

    total = 0.0
    y, m = eligible_start.year, eligible_start.month
    while (y, m) <= (eligible_end.year, eligible_end.month):
        days_in_month = calendar.monthrange(y, m)[1]
        seg_start = max(eligible_start, date(y, m, 1))
        seg_end = min(eligible_end, date(y, m, days_in_month))
        eligible_days = (seg_end - seg_start).days + 1        # inclusive
        total += float(monthly_rate) * eligible_days / days_in_month
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    if annual_cap is not None:
        total = min(total, float(annual_cap))
    return total
