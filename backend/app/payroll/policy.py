"""
Payroll policy — per-organization, through the standard settings cascade.
=========================================================================
Payroll arithmetic embeds business decisions that differ by country, contract
and company. Two lived in ``payroll/service.py`` as literals:

  * the **daily-rate divisor**: ``gross / 30.0``. Thirty is one convention among
    several — 26 (six-day week, common in Indian manufacturing), 22 (average
    working days per month), or the actual number of working days in the period
    are all used in practice. A tenant on a 26-day divisor was being underpaid
    per LWP day by ~13% relative to their own policy.
  * **which days an unpaid-leave span consumes**: the old code counted every
    calendar day between the leave's start and end, so a Friday-to-Monday
    unpaid leave deducted four days including a weekend the employee was never
    scheduled to work.

Both are now settings. Defaults reproduce the previous behaviour exactly, so
existing tenants see no change until they configure otherwise.

This module deliberately holds only the KEYS and the resolution helper. The
arithmetic stays in ``payroll/service.py``; there is no second payroll engine.
"""
from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy.orm import Session

SETTING_LWP_DIVISOR_MODE = "payroll.lwp_divisor_mode"
SETTING_LWP_DIVISOR_DAYS = "payroll.lwp_divisor_days"
SETTING_LWP_COUNTS_NON_WORKING = "payroll.lwp_counts_non_working_days"
# How far back an admin may credit lost work time. This is PAYROLL GOVERNANCE,
# not a platform safety limit: it depends on the tenant's correction window and
# how long its pay periods stay open. It was a module constant of 60 days, so an
# organization on a quarterly correction cycle simply could not credit an
# employee for a power cut two months earlier.
SETTING_TIME_ADJ_MAX_PAST_DAYS = "payroll.time_adjustment_max_past_days"
# Which fortnight a bi-weekly payroll period falls in depends entirely on the
# anchor. It was date(2024, 1, 1) — one convention applied to every employer.
# The default reproduces exactly that, so existing periods do not move.
SETTING_BIWEEKLY_ANCHOR = "payroll.biweekly_anchor_date"

# "fixed"        → always divide by payroll.lwp_divisor_days
# "working_days" → divide by the employee's working days in the payroll period,
#                  resolved from schedule.working_days + the holiday calendar
DIVISOR_MODES = ("fixed", "working_days")

SETTING_DEFAULTS: Dict[str, str] = {
    SETTING_LWP_DIVISOR_MODE: "fixed",
    SETTING_LWP_DIVISOR_DAYS: "30",
    # "true" reproduces the previous calendar-day counting exactly.
    SETTING_LWP_COUNTS_NON_WORKING: "true",
    SETTING_TIME_ADJ_MAX_PAST_DAYS: "60",      # the previous module constant
    SETTING_BIWEEKLY_ANCHOR: "2024-01-01",     # the previous module constant
}

SETTING_DESCRIPTIONS: Dict[str, str] = {
    SETTING_LWP_DIVISOR_MODE:
        'How the per-day salary rate is derived for loss-of-pay deductions: '
        '"fixed" divides monthly gross by the number below; "working_days" '
        'divides by the employee\'s actual working days in the payroll period.',
    SETTING_LWP_DIVISOR_DAYS:
        "Days per month used as the divisor when the mode is \"fixed\" "
        "(commonly 30, 26 or 22).",
    SETTING_LWP_COUNTS_NON_WORKING:
        "Whether an unpaid-leave span deducts weekends and holidays that fall "
        "inside it (true), or only the employee's working days (false).",
    SETTING_TIME_ADJ_MAX_PAST_DAYS:
        "How many days back an admin may credit lost work time to an employee. "
        "Set to match your payroll correction window.",
    SETTING_BIWEEKLY_ANCHOR:
        "Reference date (YYYY-MM-DD) that fixes which fortnight a bi-weekly "
        "pay period falls in. Any date inside a period you consider period 1.",
}


def _effective(db: Session, user, keys=None) -> Dict[str, str]:
    from app.settings import scope as settings_scope
    wanted = {k: SETTING_DEFAULTS[k] for k in (keys or SETTING_DEFAULTS.keys())}
    try:
        return settings_scope.resolve_effective(db, user, wanted)
    except Exception:
        return dict(wanted)


def divisor_days(db: Session, user, period_start=None, period_end=None) -> float:
    """Days to divide monthly gross by for this employee's LWP deduction.

    Never returns 0 — a zero divisor would raise mid-payroll-run, and the safe
    behaviour on misconfiguration is the documented system default.
    """
    cfg = _effective(db, user)
    mode = str(cfg.get(SETTING_LWP_DIVISOR_MODE, "fixed")).strip().lower()

    if mode == "working_days" and period_start and period_end:
        days = working_days_between(db, user, period_start, period_end)
        if days > 0:
            return float(days)

    try:
        fixed = float(str(cfg.get(SETTING_LWP_DIVISOR_DAYS, "30")).strip())
    except (TypeError, ValueError):
        fixed = 30.0
    return fixed if fixed > 0 else 30.0


def counts_non_working_days(db: Session, user) -> bool:
    raw = _effective(db, user, [SETTING_LWP_COUNTS_NON_WORKING]).get(
        SETTING_LWP_COUNTS_NON_WORKING, "true")
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def working_days_between(db: Session, user, start_date, end_date) -> int:
    """Count this employee's working days in ``[start_date, end_date]``.

    Uses the Schedule Resolver, so it honours the organization's configured
    working days, the holiday calendar, roster off-days and any team/employee
    override — the same answer the attendance report gives.
    """
    from datetime import timedelta
    try:
        from app.schedule_policy.resolver import resolve_work_schedules_over
        dates = []
        d = start_date
        while d <= end_date:
            dates.append(d)
            d += timedelta(days=1)
        if not dates:
            return 0
        by_day = resolve_work_schedules_over(db, [user], dates)
        uid = getattr(user, "id", None)
        return sum(
            1 for day in dates
            if (by_day.get(day, {}).get(uid) is not None
                and by_day[day][uid].is_working_day)
        )
    except Exception:
        return 0


def time_adjustment_max_past_days(db: Session, user) -> int:
    """How far back this organization allows a time credit. 0 = same day only."""
    raw = _effective(db, user, [SETTING_TIME_ADJ_MAX_PAST_DAYS]).get(
        SETTING_TIME_ADJ_MAX_PAST_DAYS, "60")
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        n = int(SETTING_DEFAULTS[SETTING_TIME_ADJ_MAX_PAST_DAYS])
    return max(0, n)


def biweekly_anchor(db: Session, user):
    """The reference date that fixes this organization's fortnight parity.

    Falls back to the documented system default on a malformed value rather
    than to whatever the caller last used — a bad string must not silently
    shift everyone's pay period.
    """
    from datetime import date as _date
    raw = _effective(db, user, [SETTING_BIWEEKLY_ANCHOR]).get(
        SETTING_BIWEEKLY_ANCHOR, SETTING_DEFAULTS[SETTING_BIWEEKLY_ANCHOR])
    try:
        y, m, d = (int(x) for x in str(raw).strip().split("-"))
        return _date(y, m, d)
    except Exception:
        y, m, d = (int(x) for x in SETTING_DEFAULTS[SETTING_BIWEEKLY_ANCHOR].split("-"))
        return _date(y, m, d)
