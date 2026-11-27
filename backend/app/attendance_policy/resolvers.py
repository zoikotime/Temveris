"""
Temveris – Attendance Policy Resolvers
=======================================
Reusable, multi-tenant policy helpers for the Controlled Clock-Out feature.

This is the **Phase 1 policy/configuration foundation only**. Everything here
is a PURE READ helper — nothing in this module is wired into clock-in,
clock-out, breaks, the desktop tracker, or any existing behaviour. Future
phases (Early Clock Out validation) will call these helpers; today they are
dormant.

Two questions are answered, both scoped strictly to ``organization_id``:

  * ``resolve_expected_work_seconds(db, user, on_date)`` – how many seconds is
    this employee expected to work on this date?
  * ``is_flexible_clock_out_day(db, org_id, on_date)`` – is clock-out
    unrestricted on this date (weekend / configured flex day / holiday)?

Expected-duration precedence (confirmed business requirement):

    1. Explicit shift-level *required-worked-duration* field — INTENTIONALLY
       SKIPPED. No such dedicated field exists on Shift/ShiftAssignment today
       (they carry only the scheduled window: start_time/end_time). The raw
       scheduled window (end - start) may include break time and therefore must
       NOT be used as required actual worked duration. If a dedicated field is
       added later, it slots in here.
    2. Org employment-type policy (AppSettings ``expected_work_durations``)
    3. Org work-hours threshold   (AppSettings ``work_hours_threshold``)
    4. Safe global default        (28800 s = 8h)

"Required worked duration" here means actual worked time EXCLUDING breaks. It is
NOT the shift window and NOT adjusted by any break allowance. Phase 2 compares
it against the existing "elapsed session minus recorded breaks" worked-time
calculation — this module never duplicates that calculation.

Robustness contract: a malformed AppSettings row must NEVER raise. Every parse
is defensive and falls back to a safe default, so a bad org setting can never
break clock-out once these helpers are eventually wired in.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Union

from sqlalchemy.orm import Session

from database import models

# ──────────────────────────────────────────────────────────────────────────
# Defaults — single source of truth (also registered with the settings router)
# ──────────────────────────────────────────────────────────────────────────

# 8h00m — required daily worked time for full-time / untyped employees, used as
# the ultimate fallback by resolve_expected_work_seconds(). NOTE: intentionally
# LOWER than the risk router's DEFAULT_THRESHOLD_SECS (31500 / 8h45m). The
# early-clock-out gate now lets full-timers leave at 8h; the risk engine's
# late/deficit alerting still uses its own 8h45m threshold (unchanged).
DEFAULT_EXPECTED_WORK_SECONDS: int = 28800

# Conceptual defaults: full-time 8h00m, part-time 4h00m. Part-time is given a
# real target here (the existing risk engine merely *skips* part-time); this
# module does not change that — the risk engine is untouched.
DEFAULT_EXPECTED_WORK_DURATIONS: Dict[str, str] = {
    "full_time": "8:00",
    "part_time": "4:00",
}

DEFAULT_FLEXIBLE_CLOCK_OUT_DAYS: List[str] = ["SATURDAY", "SUNDAY"]

# Upper bound guard for a single day's expected/shift duration (24h).
_MAX_WORK_SECONDS: int = 24 * 3600

# AppSettings keys
SETTING_EXPECTED_WORK_DURATIONS = "expected_work_durations"
SETTING_FLEXIBLE_CLOCK_OUT_DAYS = "flexible_clock_out_days"
SETTING_WORK_HOURS_THRESHOLD = "work_hours_threshold"  # reused, already registered

# 0=Mon … 6=Sun, matching datetime.date.weekday()
_WEEKDAY_NAMES: List[str] = [
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
]

# ──────────────────────────────────────────────────────────────────────────
# Settings-router registration payload
# The settings router merges these into its DEFAULTS/DESCRIPTIONS so the keys
# are editable via PUT /settings/update and returned by GET /settings, exactly
# like the Active Time Engine tunables. early_clockout_enabled /
# disconnect_detection_enabled are future kill switches — declared OFF; NOTHING
# reads them yet.
# ──────────────────────────────────────────────────────────────────────────
# Business policies that used to be constants in employees/router.py. Each is
# registered here so it flows through the SAME employee > team > org > default
# cascade as everything else and is editable per tenant.
SETTING_EARLY_CLOCKOUT_ENABLED   = "early_clockout_enabled"
SETTING_DISCONNECT_AUTOCLOSE     = "disconnect_detection_enabled"
SETTING_DISCONNECT_GRACE_MINUTES = "disconnect_grace_minutes"
SETTING_IDLE_ALERT_MINUTES       = "attendance.idle_alert_minutes"
SETTING_DESKTOP_ONLY_CLOCK_IN    = "attendance.desktop_only_clock_in"
# The weekly-deficit ratios behind the admin "at risk" list. They were the
# literals 0.75 / 0.5 in employees/router.py, so an organization with a
# stricter or looser tolerance could not change who appeared on it.
SETTING_RISK_DEFICIT_PCT         = "attendance.risk_deficit_percent"
SETTING_RISK_HIGH_PCT            = "attendance.risk_high_percent"

SETTING_DEFAULTS: Dict[str, str] = {
    SETTING_EXPECTED_WORK_DURATIONS: json.dumps(DEFAULT_EXPECTED_WORK_DURATIONS),
    SETTING_FLEXIBLE_CLOCK_OUT_DAYS: json.dumps(DEFAULT_FLEXIBLE_CLOCK_OUT_DAYS),
    # NOW CONSUMED: the Early Clock Out gate in employees/router.clock_out
    # checks this before enforcing. It ships "true" because the gate has been
    # enforcing unconditionally since Phase 2 — shipping "false" would silently
    # switch off a live control for every existing tenant.
    SETTING_EARLY_CLOCKOUT_ENABLED: "true",
    # NOW CONSUMED: _auto_close_abandoned_sessions. "true" matches the live
    # _ABANDONED_AUTOCLOSE_ENABLED flag it replaces.
    SETTING_DISCONNECT_AUTOCLOSE: "true",
    SETTING_DISCONNECT_GRACE_MINUTES: "15",
    SETTING_IDLE_ALERT_MINUTES: "15",
    SETTING_DESKTOP_ONLY_CLOCK_IN: "false",
    SETTING_RISK_DEFICIT_PCT: "75",
    SETTING_RISK_HIGH_PCT: "50",
}

SETTING_DESCRIPTIONS: Dict[str, str] = {
    SETTING_RISK_DEFICIT_PCT:
        "An employee appears on the attendance risk list when their worked "
        "hours this week fall below this percentage of their expected hours.",
    SETTING_RISK_HIGH_PCT:
        "Below this percentage of expected hours the risk level is reported as "
        "high rather than medium.",
    SETTING_EXPECTED_WORK_DURATIONS:
        'Expected daily work duration per employment type as JSON, e.g. '
        '{"full_time": "8:00", "part_time": "4:00"} (HH:MM). Legacy fallback '
        'only — schedule.expected_work_hours is the primary source.',
    SETTING_FLEXIBLE_CLOCK_OUT_DAYS:
        'Days with flexible (unrestricted) clock-out as a JSON list of weekday '
        'names, e.g. ["SATURDAY", "SUNDAY"]. Legacy fallback only — '
        'schedule.working_days is the primary source.',
    SETTING_EARLY_CLOCKOUT_ENABLED:
        "Require a typed reason when an employee clocks out below their "
        "expected daily hours on a working day (true/false).",
    SETTING_DISCONNECT_AUTOCLOSE:
        "Automatically close a clocked-in session when the desktop tracker "
        "goes silent for longer than the disconnect grace window (true/false). "
        "The session is closed at the LAST HEARTBEAT, so hours are never "
        "inflated.",
    SETTING_DISCONNECT_GRACE_MINUTES:
        "Minutes of tracker silence before an open session is treated as an "
        "unexpected disconnect and auto-closed.",
    SETTING_IDLE_ALERT_MINUTES:
        "Minutes of continuous inactivity before an employee is flagged idle "
        "on the admin dashboard and HR is notified. Separate from the desktop "
        "tracker's own idle_threshold_minutes, which only tags screenshots.",
    SETTING_DESKTOP_ONLY_CLOCK_IN:
        "Require employees to clock IN from the Temveris desktop app rather "
        "than the website (true/false). Clock-out is never restricted, so a "
        "tracker crash can't strand someone clocked in.",
}


# ──────────────────────────────────────────────────────────────────────────
# Parsing / normalization helpers (pure, defensive)
# ──────────────────────────────────────────────────────────────────────────
# Aliases → canonical employment type. Case / spaces / hyphens are normalized
# first, then mapped. Anything unrecognized keeps its normalized key so an org
# can still define custom types in `expected_work_durations`; the resolver's
# fallback chain then gives such types the safe full-time default duration.
# Supported aliases:
#   full_time ← full_time, full time, full-time, fulltime, ft, full
#   part_time ← part_time, part time, part-time, parttime, pt, part
_EMPLOYMENT_TYPE_ALIASES: Dict[str, str] = {
    "full_time": "full_time", "fulltime": "full_time", "ft": "full_time", "full": "full_time",
    "part_time": "part_time", "parttime": "part_time", "pt": "part_time", "part": "part_time",
}


def normalize_employment_type(value: Optional[str]) -> str:
    """Canonicalize an employment-type label. Collapses case/spaces/hyphens and
    maps known aliases (ft/pt/parttime/…) to full_time / part_time. Empty or
    None → full_time (safe default). Unknown non-empty values are returned in
    normalized form so custom org policies still resolve."""
    key = re.sub(r"[\s-]+", "_", (value or "").strip().lower())
    if not key:
        return "full_time"
    return _EMPLOYMENT_TYPE_ALIASES.get(key, key)


def default_expected_seconds_for_type(employment_type: Optional[str]) -> int:
    """Required worked seconds for an employment type using the built-in
    DEFAULTS only (no DB / org overrides). Part-time → 4h, full-time and any
    unrecognized type → 8h. Use this in bulk contexts (reports over many
    users) where a per-user DB lookup would be an N+1; the DB-aware, override-
    honoring path is resolve_expected_work_seconds()."""
    et = normalize_employment_type(employment_type)
    val = DEFAULT_EXPECTED_WORK_DURATIONS.get(et)
    secs = parse_hhmm_to_seconds(val) if val else None
    return secs if secs is not None else DEFAULT_EXPECTED_WORK_SECONDS


def parse_hhmm_to_seconds(value: Union[str, int, None]) -> Optional[int]:
    """Parse an "H:MM" / "HH:MM" duration string into seconds.

    Returns ``None`` (not an exception) for anything malformed: wrong shape,
    non-numeric, negative, impossible minutes (>59), zero, or above 24h. The
    caller falls back to the next precedence level on ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except (ValueError, TypeError):
        return None
    if hours < 0 or minutes < 0 or minutes > 59:
        return None
    secs = hours * 3600 + minutes * 60
    if secs <= 0 or secs > _MAX_WORK_SECONDS:
        return None
    return secs


def _as_date(on_date: Union[date, datetime]) -> date:
    """Coerce a datetime to a date; pass a date through unchanged."""
    if isinstance(on_date, datetime):
        return on_date.date()
    return on_date


# ──────────────────────────────────────────────────────────────────────────
# Tenant-scoped settings reads
# ──────────────────────────────────────────────────────────────────────────
def _get_setting_value(db: Session, org_id: Optional[int], key: str) -> Optional[str]:
    """Fetch a single **org-level** AppSettings value for this org+key, or ``None``.

    Strictly scoped to ``organization_id`` — a row belonging to another tenant
    can never be returned. Also pinned to the ORG tier (``team_key`` and
    ``user_id`` both NULL): without that, ``.first()`` could return a team- or
    employee-scope override row for the same key and apply one person's
    override to the whole organization. Any DB error degrades to ``None``
    (safe default).
    """
    try:
        row = (
            db.query(models.AppSettings)
            .filter(
                models.AppSettings.organization_id == org_id,
                models.AppSettings.key == key,
                models.AppSettings.team_key.is_(None),   # org level only
                models.AppSettings.user_id.is_(None),
            )
            .first()
        )
    except Exception:
        return None
    if row and row.value is not None:
        return row.value
    return None


def _expected_durations_policy(db: Session, org_id: Optional[int]) -> Dict[str, str]:
    """Return the employment-type → duration map for this org.

    Starts from the safe defaults and layers the org's configured overrides on
    top, so an org can override ``full_time`` without losing the ``part_time``
    default (and vice-versa). Malformed JSON / wrong shape → defaults only.
    """
    policy: Dict[str, str] = dict(DEFAULT_EXPECTED_WORK_DURATIONS)
    raw = _get_setting_value(db, org_id, SETTING_EXPECTED_WORK_DURATIONS)
    if not raw:
        return policy
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return policy
    if not isinstance(data, dict):
        return policy
    for key, val in data.items():
        policy[normalize_employment_type(key)] = val
    return policy


def _flexible_days_set(db: Session, org_id: Optional[int]) -> set:
    """Return the set of UPPERCASE weekday names that are flexible for this org.

    A valid but empty list (``[]``) is honoured (org works every day). Only a
    missing/malformed setting falls back to the Sat/Sun default.
    """
    default = {d.upper() for d in DEFAULT_FLEXIBLE_CLOCK_OUT_DAYS}
    raw = _get_setting_value(db, org_id, SETTING_FLEXIBLE_CLOCK_OUT_DAYS)
    if not raw:
        return default
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return default
    if not isinstance(data, list):
        return default
    return {str(d).strip().upper() for d in data if str(d).strip()}


def _is_holiday(db: Session, org_id: Optional[int], on_date: date) -> bool:
    """True if a Holiday exists for this org on this date. Scoped to the org so
    one tenant's holiday can never leak into another's clock-out policy.

    Delegates to ``app.holidays.org_calendar`` — one definition of "which
    holidays apply to this organization", now including the country scope, so
    a workspace that keeps both a US and an Indian calendar does not treat
    Diwali as a non-working day for its US staff.

    The ``_or_empty`` variant is deliberate. It selects only the date column
    (a full-entity load emits every mapped column, so a model column not yet
    present in the database made Postgres reject the statement) AND it rolls
    back on failure. Without the rollback the previous "fail-open" left the
    session aborted: `is_flexible_clock_out_day` returned normally while
    poisoning it, the next query in clock_out (`resolve_expected_work_seconds`)
    failed, required_secs became None, and the Early Clock Out reason gate was
    skipped for every worker.
    """
    from app.holidays import org_calendar

    return bool(org_calendar.holiday_dates_or_empty(db, org_id, dates=[on_date]))


# ──────────────────────────────────────────────────────────────────────────
# Public resolvers
# ──────────────────────────────────────────────────────────────────────────
def resolve_expected_work_seconds(
    db: Session,
    user: "models.User",
    on_date: Union[date, datetime],
) -> int:
    """Required actual worked duration (seconds) for ``user`` on ``on_date``,
    EXCLUDING breaks. **The** number the Early Clock Out gate compares against.

    THE canonical answer. The Schedule Resolver walks the full
    ShiftAssignment > employee > team > organization > system-default cascade;
    ``_expected_from_type_policy`` below survives only as the resolver's own
    innermost fallback (and as this function's guard if the resolver itself
    fails), never as a competing source of truth.

    Every consumer that needs "how many hours does this person owe today"
    routes here — the Early Clock Out gate, the risk engine, the overtime
    report, the intelligence rollups and the dashboard — so an employee cannot
    be permitted to leave at one number and be measured against another.

    Always returns a positive int; never raises on malformed config.
    """
    try:
        from app.schedule_policy.resolver import resolve_work_schedule
        secs = resolve_work_schedule(db, user, on_date).expected_work_seconds
        if secs and secs > 0:
            return secs
    except Exception:
        # Fail back to the type map rather than blocking a clock-out. The
        # resolver already fails open internally, so reaching here means
        # something structural is wrong; it is logged there.
        pass
    return _expected_from_type_policy(db, user, on_date)


def engine_enabled_for_org(db: Session, org_id) -> bool:
    """DEPRECATED — always True.

    ``schedule.engine_enabled`` was the rollout gate that selected between the
    Schedule Resolver and a parallel set of hard-coded legacy branches. The
    legacy branches have been removed: the resolver is now the only
    implementation, so there is nothing left to gate. Every consumer that used
    to call this has been migrated.

    Kept for one release so a stale import cannot crash a worker, and so the
    ``schedule.engine_enabled`` rows an operator may already have written are
    harmless rather than behaviour-changing. Remove with the setting key.
    """
    import warnings
    warnings.warn(
        "engine_enabled_for_org is deprecated: the Schedule Resolver is the "
        "canonical policy path and is always active.",
        DeprecationWarning, stacklevel=2,
    )
    return True


def _expected_from_type_policy(
    db: Session,
    user: "models.User",
    on_date: Union[date, datetime],
) -> int:
    """The original Phase-1 implementation, unchanged.

    Precedence:
      1. Explicit shift-level required-worked-duration field — not implemented
         here; the Schedule Resolver covers it when the engine is on.
      2. Org employment-type policy (``expected_work_durations``).
      3. Org ``work_hours_threshold`` (unmapped/legacy employment types).
      4. Safe global default (28800 s = 8h).

    Kept as a separate function for two reasons: it is the engine-OFF path, and
    the Schedule Resolver calls it as its own fallback when no tier configures
    expected hours. Routing that fallback through the public function instead
    would recurse infinitely once the engine is enabled.
    """
    on_date = _as_date(on_date)  # normalized for forward-compatibility (level 1)
    org_id = getattr(user, "organization_id", None)

    # 1) Explicit shift-level required-worked-duration → intentionally skipped
    #    (no dedicated field on Shift/ShiftAssignment; raw window must not be
    #    used as required worked time).

    # 2) Org employment-type policy
    policy = _expected_durations_policy(db, org_id)
    emp_type = normalize_employment_type(getattr(user, "employment_type", None))
    if emp_type in policy:
        secs = parse_hhmm_to_seconds(policy[emp_type])
        if secs is not None:
            return secs

    # 3) Org work_hours_threshold (explicitly-configured row only)
    threshold_secs = parse_hhmm_to_seconds(
        _get_setting_value(db, org_id, SETTING_WORK_HOURS_THRESHOLD)
    )
    if threshold_secs is not None:
        return threshold_secs

    # 4) Safe global default
    return DEFAULT_EXPECTED_WORK_SECONDS


def attendance_policy_for(db: Session, user, keys: Optional[List[str]] = None) -> Dict[str, str]:
    """Effective values of the attendance policy keys for one employee.

    Goes through ``settings.scope.resolve_effective`` — the SAME cascade that
    serves ``GET /settings`` — so a team or employee override of, say, the idle
    alert threshold actually takes effect. Every consumer of these keys reads
    them here rather than issuing its own org-tier query, which is what let a
    stored override sit inert.
    """
    from app.settings import scope as settings_scope
    wanted = {k: SETTING_DEFAULTS[k] for k in (keys or SETTING_DEFAULTS.keys())
              if k in SETTING_DEFAULTS}
    try:
        return settings_scope.resolve_effective(db, user, wanted)
    except Exception:
        return dict(wanted)


def policy_bool(db: Session, user, key: str) -> bool:
    raw = attendance_policy_for(db, user, [key]).get(key, SETTING_DEFAULTS.get(key, ""))
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def policy_float(db: Session, user, key: str) -> float:
    """A percentage/ratio setting for this employee, through the cascade.

    Separate from policy_int because the risk thresholds are percentages a
    tenant may want to express as 82.5, and int() would silently floor them.
    """
    default = float(SETTING_DEFAULTS.get(key, "0") or 0)
    raw = attendance_policy_for(db, user, [key]).get(key)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def policy_int(db: Session, user, key: str) -> int:
    default = int(SETTING_DEFAULTS.get(key, "0") or 0)
    raw = attendance_policy_for(db, user, [key]).get(key)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def is_flexible_clock_out_day(
    db: Session,
    org_id: Optional[int],
    on_date: Union[date, datetime],
    *,
    include_holidays: bool = True,
) -> bool:
    """True if clock-out should be unrestricted on ``on_date`` for this org.

    A day is flexible when its weekday is in the org's configured
    ``flexible_clock_out_days`` (default Sat/Sun) OR — when
    ``include_holidays`` is True — an org holiday falls on that date. Strictly
    org-scoped; never raises on malformed config.

    When ``schedule.engine_enabled`` is on for the org this becomes the inverse of
    the Schedule Resolver's ``is_working_day``, so working days come from
    ``schedule.working_days`` instead of the legacy flex-day list. Engine off →
    the original implementation below, unchanged.

    Note this takes ``org_id`` rather than a user, so the resolver needs a
    stand-in user to resolve against; org-level working days and holidays do not
    depend on the individual, and a per-user answer belongs to the caller that
    has one (the Early Clock Out gate passes its own user separately).
    """
    on_date = _as_date(on_date)
    if org_id is not None:
        try:
            from app.schedule_policy.resolver import resolve_work_schedule
            from database import models as _m
            probe = _m.User(organization_id=org_id)
            sched = resolve_work_schedule(db, probe, on_date)
            if include_holidays:
                return not sched.is_working_day
            # Weekday-only question: ignore the holiday component.
            return not (_WEEKDAY_NAMES[on_date.weekday()] in sched.working_days)
        except Exception:
            pass  # fall through to the flexible-days list below
    flexible_days = _flexible_days_set(db, org_id)
    weekday_name = _WEEKDAY_NAMES[on_date.weekday()]
    if weekday_name in flexible_days:
        return True
    if include_holidays and _is_holiday(db, org_id, on_date):
        return True
    return False
