"""
The Schedule Resolver — the single future source of truth.
==========================================================
One question, one answer: *what schedule applies to this employee on this date?*
Every attendance-related subsystem (Early Clock Out, risk alerts, the attendance
monitor, reports, the dashboard, eventually payroll) will read through
``resolve_work_schedule`` rather than re-deriving shift start, grace or expected
hours. Today five files carry their own expected-hours constant and three carry
their own definition of "late"; this exists to collapse all of that.

This is now THE path. There is no rollout gate and no parallel legacy
implementation: attendance, the alert monitor, reports, payroll, risk,
intelligence and the dashboard all read their schedule from here. Values that
used to be constants in those modules (a noon absence cutoff, a 20-minute
grace, a Mon–Fri week, an 8h or 8h45m day, a 10:30 start) are now rows in
``app_settings`` at whichever tier the organization chose.

Design constraints honoured
---------------------------
* **No duplicated cascade.** Tier resolution is
  ``settings.scope.resolve_effective_with_sources`` — the same code that serves
  ``GET /settings``. This module never queries ``app_settings`` itself.
* **Timezone comes from ``OrgSettings.default_timezone``** via ``app._tz``.
  There is no second timezone setting.
* **Employment type is Approach A** — see ``_resolve_employment_type``.
* **Tenant isolation** — every query filters ``organization_id``. A shift id
  belonging to another org is ignored, not loaded.
* **Never raises.** Any failure degrades to the system-default schedule, matching
  the fail-open contract of ``attendance_policy``.

Pre-P3 columns
--------------
P3 (schema) has not shipped, so ``Shift.expected_work_seconds``,
``Shift.crosses_midnight``, ``Shift.config`` and ``ShiftAssignment.is_off_day`` /
``expected_work_seconds`` do not exist yet. They are read with ``getattr(...,
None)`` — the same defensive idiom used throughout ``attendance_policy`` — so
those precedence points activate automatically when P3 lands, with no change
here. Tests cover both the absent and present cases.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app import _tz
from app.settings import scope as settings_scope
from app.schedule_policy import keys as K

# Only the schedule.* keys participate in the cascade read here.
_DEFAULTS: Dict[str, str] = dict(K.SETTING_DEFAULTS)

_MAX_WORK_SECONDS = 24 * 3600
# A cutoff more than a day past the shift start cannot be meant literally.
_MAX_ABSENT_AFTER_MINUTES = 24 * 60

_log = logging.getLogger("zoiko.schedule.resolver")


def _org_calendar():
    """The shared organization holiday calendar, imported lazily.

    ``app.holidays`` imports the ORM and this module is pulled in from several
    request paths, so the import is deferred to keep the module graph acyclic
    and startup unchanged.
    """
    from app.holidays import org_calendar
    return org_calendar


@contextmanager
def _safe_load(db: Session, step: str, org_id=None):
    """Run one optional load so a failure degrades instead of raising — WITHOUT
    leaving the caller's transaction unusable.

    A bare ``except Exception: pass`` around a query is not enough on Postgres.
    The first failing statement aborts the whole transaction, and every later
    statement raises ``InFailedSqlTransaction`` until someone rolls back. That
    turned a missing column into a request-wide cascade once already (the
    ``domain_categories`` outage), and it is live right now: production is
    behind on ``f1a9d4c72e86``, so the shift and roster loads below fail on
    every ``/employees/team`` call and silently poison the session. It only
    looks harmless because this endpoint happens to do its writes first.

    A SAVEPOINT is the fix rather than ``db.rollback()``: rolling the whole
    transaction back would discard uncommitted work the CALLER owns, which is
    not this helper's to throw away. ``begin_nested`` rolls back just this
    statement and leaves the outer transaction usable.

    Failures log at WARNING with the step name — a missing column is a
    deployment fault that someone must see, not something to swallow.
    """
    try:
        with db.begin_nested():
            yield
    except Exception as exc:
        _log.warning(
            "schedule.resolver load failed step=%s org=%s err=%s; "
            "degrading to defaults for this field",
            step, org_id, type(exc).__name__, exc_info=True,
        )


@dataclass
class ScheduleContext:
    """Everything the resolver needs for one org over a date range, prefetched.

    Why this exists
    ---------------
    A per-call resolve issues roughly seven queries (three cascade tiers, the
    team lookup, holidays, the shift, the assignment, org timezone, and the
    legacy type-policy fallback). The monthly attendance report decides
    working-day per (user, day), so wiring it naively would be
    O(users x days x queries) — about 12,000 queries for a 59-user month.

    ``build_context`` loads the same information in a FIXED number of queries
    (five, independent of users and days) and every subsequent resolve becomes
    dictionary lookups. This lives in the resolver layer on purpose: the report
    must not know how resolution is cached, and no second copy of the precedence
    rules may exist.
    """
    org_id: Optional[int]
    timezone_name: str
    engine_enabled: bool
    # tier -> {key: value}; employee/team keyed by user id / team name
    org_rows: Dict[str, str] = field(default_factory=dict)
    team_rows: Dict[str, Dict[str, str]] = field(default_factory=dict)
    employee_rows: Dict[int, Dict[str, str]] = field(default_factory=dict)
    holidays: set = field(default_factory=set)
    shifts_by_id: Dict[int, Any] = field(default_factory=dict)
    # (user_id, "YYYY-MM-DD") -> ShiftAssignment
    assignments: Dict[tuple, Any] = field(default_factory=dict)
    # employment type -> required seconds, from the org's expected_work_durations
    legacy_expected: Dict[str, int] = field(default_factory=dict)
    legacy_threshold: Optional[int] = None
    # Deprecated alias for working days — see _legacy_working_days.
    legacy_flexible_days: Optional[List[str]] = None


def build_context(db: Session, org_id, users, dates) -> ScheduleContext:
    """Prefetch one org's schedule inputs for a set of users and dates.

    Five queries total, regardless of how many users or days are asked for.
    Every failure degrades to an empty context, which resolves to system
    defaults rather than raising.
    """
    from database import models

    user_ids = [getattr(u, "id", None) for u in users]
    user_ids = [i for i in user_ids if i is not None]
    date_strs = [d.isoformat() for d in dates]
    ctx = ScheduleContext(
        org_id=org_id,
        timezone_name=_tz.FALLBACK_TZ_NAME,
        engine_enabled=False,
    )
    if not org_id:
        return ctx

    keys = list(_DEFAULTS.keys()) + [
        "expected_work_durations", "work_hours_threshold",
        "flexible_clock_out_days",
    ]

    # 1 — every settings row for this org across all three tiers, one query.
    with _safe_load(db, "settings", org_id):
        for row in (
            db.query(models.AppSettings)
            .filter(models.AppSettings.organization_id == org_id,
                    models.AppSettings.key.in_(keys))
            .all()
        ):
            if row.user_id is not None:
                ctx.employee_rows.setdefault(row.user_id, {})[row.key] = row.value
            elif row.team_key is not None:
                ctx.team_rows.setdefault(row.team_key, {})[row.key] = row.value
            else:
                ctx.org_rows[row.key] = row.value

    ctx.engine_enabled = _bool_or(ctx.org_rows.get(K.SETTING_ENGINE_ENABLED), False)

    # The legacy employment-type policy, parsed once instead of per user.
    # No DB access, so a plain try is right here — there is no transaction to
    # protect, only malformed JSON in a settings row to survive.
    try:
        from app.attendance_policy.resolvers import (
            DEFAULT_EXPECTED_WORK_DURATIONS, normalize_employment_type,
        )
        policy = dict(DEFAULT_EXPECTED_WORK_DURATIONS)
        raw = ctx.org_rows.get("expected_work_durations")
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k, v in data.items():
                    policy[normalize_employment_type(k)] = v
        ctx.legacy_expected = {
            et: secs for et, secs in
            ((et, _hhmm_seconds(v)) for et, v in policy.items()) if secs
        }
        ctx.legacy_threshold = _hhmm_seconds(ctx.org_rows.get("work_hours_threshold"))
        ctx.legacy_flexible_days = _flex_days_or_none(
            ctx.org_rows.get("flexible_clock_out_days"))
    except Exception:
        _log.warning("schedule.resolver legacy expected-duration policy unparseable "
                     "org=%s; using defaults", org_id, exc_info=True)

    # 2 — holidays in range, via the shared org calendar so the country-scoping
    # rule is defined once (see app/holidays/org_calendar.py). Column-scoped
    # there on purpose: selecting the whole model would name `calendar_key`,
    # which an un-migrated production database does not have. The SAVEPOINT is
    # this module's own recovery strategy, so the raising variant is correct
    # here — `holiday_dates_or_empty` would roll back the caller's work.
    with _safe_load(db, "holidays", org_id):
        ctx.holidays = _org_calendar().holiday_dates(db, org_id, dates=list(dates))

    # 3 — active shifts (small per org; avoids a lookup per referenced id).
    with _safe_load(db, "shifts", org_id):
        for s in (
            db.query(models.Shift)
            .filter(models.Shift.organization_id == org_id,
                    models.Shift.is_active == True)  # noqa: E712
            .all()
        ):
            ctx.shifts_by_id[s.id] = s

    # 4 — roster entries for these users in range.
    if user_ids and date_strs:
        with _safe_load(db, "assignments", org_id):
            for a in (
                db.query(models.ShiftAssignment)
                .filter(models.ShiftAssignment.organization_id == org_id,
                        models.ShiftAssignment.user_id.in_(user_ids),
                        models.ShiftAssignment.date.in_(date_strs),
                        models.ShiftAssignment.status != "cancelled")
                .all()
            ):
                ctx.assignments[(a.user_id, a.date)] = a

    # 5 — org timezone.
    with _safe_load(db, "timezone", org_id):
        ctx.timezone_name = _tz.org_timezone_name(db, org_id)
    return ctx


@dataclass(frozen=True)
class EffectiveSchedule:
    """The resolved schedule for one employee on one date. Immutable."""
    on_date: date
    timezone_name: str
    is_working_day: bool
    is_holiday: bool
    engine_enabled: bool

    shift_id: Optional[int]
    shift_name: Optional[str]
    start_local: Optional[time]
    end_local: Optional[time]
    crosses_midnight: bool

    expected_work_seconds: int
    min_required_seconds: int
    grace_minutes: int
    late_after_minutes: Optional[int]
    very_late_after_minutes: int
    absent_after_hour: int
    # Minutes past start_local before a no-show is Absent. None = not
    # configured at any tier, in which case absent_after_hour decides.
    absent_after_minutes: Optional[int]

    allow_early_clock_in: bool
    allow_late_clock_out: bool
    employment_type: str
    work_mode: str
    working_days: List[str]

    config: Dict[str, Any] = field(default_factory=dict)
    # field name -> "assignment" | "shift" | "employee" | "legacy_user" | "team"
    #               | "org" | "system"
    sources: Dict[str, str] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────
# Parsing helpers (defensive — a malformed row must never raise)
# ──────────────────────────────────────────────────────────────────────────
def _int_or(value, fallback: Optional[int]) -> Optional[int]:
    try:
        text = str(value).strip()
        return int(text) if text else fallback
    except (TypeError, ValueError):
        return fallback


def _bool_or(value, fallback: bool) -> bool:
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return fallback


def _hhmm_time(value) -> Optional[time]:
    """"HH:MM" -> time. None on anything malformed."""
    try:
        parts = str(value).strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return time(h, m)
    except (TypeError, ValueError):
        pass
    return None


def _hhmm_seconds(value) -> Optional[int]:
    """"H:MM" duration -> seconds. Reuses attendance_policy's parser so the two
    subsystems can never disagree about what "8:45" means."""
    from app.attendance_policy import parse_hhmm_to_seconds
    return parse_hhmm_to_seconds(value)


def _system_default_expected_seconds() -> int:
    """The documented SYSTEM DEFAULT working day, in seconds.

    Every "we could not resolve expected hours" path lands here — never on a
    literal copied from one tenant's configuration. If the shipped default is
    itself unparseable that is a packaging bug, so it is loud rather than
    silently substituted.
    """
    secs = _hhmm_seconds(_DEFAULTS[K.SETTING_EXPECTED_WORK_HOURS])
    if not secs:
        raise RuntimeError(
            "schedule.expected_work_hours system default is unparseable: "
            f"{_DEFAULTS[K.SETTING_EXPECTED_WORK_HOURS]!r}"
        )
    return secs


_SYSTEM_DEFAULT_EXPECTED_SECONDS = _system_default_expected_seconds()


def _flex_days_or_none(raw) -> Optional[List[str]]:
    """Parse a configured ``flexible_clock_out_days`` row, or None if unset."""
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return [str(d).strip().upper() for d in data if str(d).strip()]


def _legacy_working_days(flexible_days: Optional[List[str]]) -> Optional[List[str]]:
    """Working days implied by the DEPRECATED ``flexible_clock_out_days`` key.

    ``schedule.working_days`` is canonical. But organizations configured
    ``flexible_clock_out_days`` before it existed, and deleting a setting out
    from under a live tenant is not a migration — an org that had told the
    system "Friday is a non-working day" would silently start expecting
    attendance on Fridays.

    So when NO tier configured ``schedule.working_days``, the legacy list is
    honoured as its inverse and routed through this one resolver, which means
    every consumer (attendance, reports, payroll, alerts, burnout) sees the same
    answer rather than only the clock-out gate that used to read it directly.
    Remove once tenants are migrated onto ``schedule.working_days``.
    """
    if flexible_days is None:
        return None
    flexible = set(flexible_days)
    return [d for d in WEEKDAY_NAMES_ORDER if d not in flexible]


WEEKDAY_NAMES_ORDER = list(K.WEEKDAY_NAMES)


def _json_list(value, fallback: List[str]) -> List[str]:
    try:
        data = json.loads(str(value))
        if isinstance(data, list):
            return [str(x).strip().upper() for x in data if str(x).strip()]
    except (TypeError, ValueError):
        pass
    return list(fallback)


# ──────────────────────────────────────────────────────────────────────────
# Employment type — Approach A
# ──────────────────────────────────────────────────────────────────────────
def _resolve_employment_type(db: Session, user, org_id, tkey,
                             ctx: Optional["ScheduleContext"] = None) -> tuple[str, str]:
    """``User.employment_type`` is authoritative for an individual; the cascade
    supplies only the org/team default for people who have none.

    Returns ``(employment_type, source)``.

    This is the one field that does NOT take an employee-scope settings row.
    ``User.employment_type`` is a real column already read by the risk engine and
    the reports layer, so honouring an employee-scope override here would create
    a second source of truth for the same fact — which is how the 8:00-vs-8:45
    split happened. The admin UI writes employee-level changes straight to the
    column instead. Any employee-scope row that somehow exists is ignored, and
    that is asserted by a test.
    """
    from app.attendance_policy import normalize_employment_type
    own = (getattr(user, "employment_type", None) or "").strip()
    if own:
        return normalize_employment_type(own), "employee"
    system_default = _DEFAULTS[K.SETTING_EMPLOYMENT_TYPE]
    if ctx is not None:
        # org<-team WITHOUT the employee tier, from the prefetched rows.
        value = ((ctx.team_rows.get(tkey) or {}).get(K.SETTING_EMPLOYMENT_TYPE)
                 if tkey else None) or ctx.org_rows.get(K.SETTING_EMPLOYMENT_TYPE)
        if value and value != system_default:
            return normalize_employment_type(value), ("team" if tkey else "org")
        return normalize_employment_type(system_default), "system"
    if org_id:
        # parent_effective gives org<-team WITHOUT the employee tier — exactly
        # the "default for someone with no own value" semantics we need.
        parent = settings_scope.parent_effective(
            db, org_id, _DEFAULTS, scope="employee", team_key=tkey,
        )
        value = parent.get(K.SETTING_EMPLOYMENT_TYPE)
        if value:
            # parent_effective overlays the system default, so an unset key comes
            # back equal to it. Label that "system" rather than claiming the org
            # configured it — `sources` is diagnostic and should not mislead.
            if value == system_default:
                return normalize_employment_type(value), "system"
            return normalize_employment_type(value), ("team" if tkey else "org")
    return normalize_employment_type(system_default), "system"


# ──────────────────────────────────────────────────────────────────────────
# Tenant-scoped lookups
# ──────────────────────────────────────────────────────────────────────────
def _load_shift(db: Session, org_id, shift_id: Optional[int]):
    """Load a shift, but only if it belongs to this org. A shift id from another
    tenant resolves to None rather than leaking that org's window."""
    if not shift_id or not org_id:
        return None
    from database import models
    out = None
    with _safe_load(db, "shift", org_id):
        out = (
            db.query(models.Shift)
            .filter(
                models.Shift.id == shift_id,
                models.Shift.organization_id == org_id,
                models.Shift.is_active == True,  # noqa: E712
            )
            .first()
        )
    return out


def _load_assignment(db: Session, org_id, user_id, on_date: date):
    """The roster entry for this user on this date, if any. Cancelled entries are
    ignored — they are a record that the assignment was withdrawn."""
    from database import models
    out = None
    with _safe_load(db, "assignment", org_id):
        out = (
            db.query(models.ShiftAssignment)
            .filter(
                models.ShiftAssignment.organization_id == org_id,
                models.ShiftAssignment.user_id == user_id,
                models.ShiftAssignment.date == on_date.isoformat(),
                models.ShiftAssignment.status != "cancelled",
            )
            .first()
        )
    return out


def _org_setting_raw(db: Session, org_id, key: str):
    """One org-tier settings value, for the deprecated aliases only. Everything
    else goes through the cascade in ``settings.scope``."""
    if not org_id:
        return None
    from database import models
    out = None
    with _safe_load(db, f"setting:{key}", org_id):
        row = (
            db.query(models.AppSettings.value)
            .filter(models.AppSettings.organization_id == org_id,
                    models.AppSettings.key == key,
                    models.AppSettings.team_key.is_(None),
                    models.AppSettings.user_id.is_(None))
            .first()
        )
        out = row[0] if row else None
    return out


def _holiday_dates(db: Session, org_id, dates: Sequence[date]) -> set:
    """Org-scoped holiday lookup for one or more dates, in a single query.

    Delegates to ``app.holidays.org_calendar`` so the country-scoping rule
    (unscoped rows plus the organization's own country) lives in one place
    rather than being restated at each consumer.
    """
    if not org_id or not dates:
        return set()
    out: set = set()
    with _safe_load(db, "holidays", org_id):
        out = _org_calendar().holiday_dates(db, org_id, dates=list(dates))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Public resolver
# ──────────────────────────────────────────────────────────────────────────
def resolve_work_schedule(db: Session, user, on_date, ctx: Optional[ScheduleContext] = None) -> EffectiveSchedule:
    """The effective schedule for ``user`` on ``on_date``.

    Precedence, highest first — applied per FIELD, not per document, so an
    employee can override only their grace period and inherit everything else:

      1. ``ShiftAssignment`` for (user, date) — rotating roster / one-off
      2. Employee-scope ``app_settings``
      3. Legacy ``User.default_clock_in_time`` / ``default_clock_in_grace_min``
      4. Team-scope ``app_settings``
      5. Org-scope ``app_settings``
      6. The assigned ``Shift`` row's own columns
      7. System defaults

    Level 3 is what keeps rollout behaviour-neutral: those columns are today's
    real per-employee schedule, so they must outrank org/team config until the
    P6 backfill migrates them.
    """
    if isinstance(on_date, datetime):
        on_date = on_date.date()
    org_id = getattr(user, "organization_id", None)

    try:
        return _resolve(db, user, on_date, org_id, ctx)
    except Exception:
        # Fail open to the system default — a bad row must never break clock-in.
        # Logged, not swallowed: reaching here means every field fell back to a
        # default, so an employee is being measured against a schedule nobody
        # configured. That is invisible in the response and must be visible here.
        _log.warning(
            "schedule.resolver resolve failed org=%s user=%s date=%s; "
            "falling back to system defaults",
            org_id, getattr(user, "id", None), on_date, exc_info=True,
        )
        return _system_default_schedule(on_date)


def _resolve(db: Session, user, on_date: date, org_id,
             ctx: Optional[ScheduleContext] = None) -> EffectiveSchedule:
    tkey = settings_scope.team_key_for_user(db, user)
    if ctx is not None:
        # Prefetched path — same merge_tiers precedence, zero queries.
        merged, sources = settings_scope.merge_tiers(
            _DEFAULTS,
            org=ctx.org_rows,
            team=ctx.team_rows.get(tkey) if tkey else None,
            employee=ctx.employee_rows.get(getattr(user, "id", None)),
        )
    else:
        merged, sources = settings_scope.resolve_effective_with_sources(db, user, _DEFAULTS)

    def src(key: str) -> str:
        return sources.get(key, "system")

    def configured(key: str) -> bool:
        """True when some tier actually set this key (i.e. not the system default)."""
        return src(key) != "system"

    # ── 6/7. shift + system defaults as the base layer ──────────────────────
    shift_id = _int_or(merged.get(K.SETTING_SHIFT_ID), None)
    shift = (ctx.shifts_by_id.get(shift_id) if ctx is not None
             else _load_shift(db, org_id, shift_id))
    if shift is None:
        shift_id = None

    field_sources: Dict[str, str] = {}

    # ── start / end times ───────────────────────────────────────────────────
    start_local = _hhmm_time(merged.get(K.SETTING_SHIFT_START_TIME))
    end_local = _hhmm_time(merged.get(K.SETTING_SHIFT_END_TIME))
    field_sources["start_local"] = src(K.SETTING_SHIFT_START_TIME)
    field_sources["end_local"] = src(K.SETTING_SHIFT_END_TIME)

    # A shift fills the window only where no tier configured it explicitly.
    if shift is not None:
        if not configured(K.SETTING_SHIFT_START_TIME):
            shift_start = _hhmm_time(shift.start_time)
            if shift_start is not None:
                start_local, field_sources["start_local"] = shift_start, "shift"
        if not configured(K.SETTING_SHIFT_END_TIME):
            shift_end = _hhmm_time(shift.end_time)
            if shift_end is not None:
                end_local, field_sources["end_local"] = shift_end, "shift"

    # ── 3. legacy per-employee columns beat team/org, lose to employee scope ─
    #
    # The two columns are treated as ONE unit, gated on default_clock_in_time
    # being set. That is not a convenience — it is required for correctness.
    # `User.default_clock_in_grace_min` carries a model-side default of 20, so
    # EVERY user row has a grace value (69/69 in production) while only a
    # handful have a clock-in time (7/69). Honouring the grace column on its own
    # would make org- and team-level `schedule.grace_minutes` permanently dead
    # for everyone, since the legacy tier outranks them.
    #
    # Gating on the clock-in time also matches today's semantics exactly:
    # attendance_alerts skips a worker whose default_clock_in_time is unset
    # ("no fixed shift -> can't be late") and never consults their grace.
    grace = _int_or(merged.get(K.SETTING_GRACE_MINUTES),
                    _int_or(_DEFAULTS[K.SETTING_GRACE_MINUTES], 15))
    field_sources["grace_minutes"] = src(K.SETTING_GRACE_MINUTES)

    legacy_start = _hhmm_time(getattr(user, "default_clock_in_time", None))
    if legacy_start is not None:
        if src(K.SETTING_SHIFT_START_TIME) != "employee":
            start_local, field_sources["start_local"] = legacy_start, "legacy_user"
        legacy_grace = _int_or(getattr(user, "default_clock_in_grace_min", None), None)
        if legacy_grace is not None and src(K.SETTING_GRACE_MINUTES) != "employee":
            grace, field_sources["grace_minutes"] = legacy_grace, "legacy_user"

    # ── expected worked duration ────────────────────────────────────────────
    expected = _hhmm_seconds(merged.get(K.SETTING_EXPECTED_WORK_HOURS))
    field_sources["expected_work_seconds"] = src(K.SETTING_EXPECTED_WORK_HOURS)

    employment_type, et_source = _resolve_employment_type(db, user, org_id, tkey, ctx)
    field_sources["employment_type"] = et_source

    # A shift's own required duration outranks the cascade (P3 column).
    if shift is not None:
        shift_expected = _int_or(getattr(shift, "expected_work_seconds", None), None)
        if shift_expected and 0 < shift_expected <= _MAX_WORK_SECONDS:
            expected, field_sources["expected_work_seconds"] = shift_expected, "shift"

    # When nothing configured expected hours, keep TODAY's behaviour: the legacy
    # employment-type map (full_time 8:00 / part_time 4:00) still decides, so a
    # part-timer is not silently promoted to the 8:45 system default.
    if not configured(K.SETTING_EXPECTED_WORK_HOURS) and \
            field_sources["expected_work_seconds"] != "shift":
        # The PRIVATE legacy implementation, not the public
        # resolve_expected_work_seconds — that one delegates back to this
        # resolver when the engine is enabled, which would recurse forever.
        if ctx is not None:
            legacy_expected = (ctx.legacy_expected.get(employment_type)
                               or ctx.legacy_threshold)
        else:
            from app.attendance_policy.resolvers import _expected_from_type_policy
            legacy_expected = _expected_from_type_policy(db, user, on_date)
        if legacy_expected:
            expected, field_sources["expected_work_seconds"] = legacy_expected, "legacy_type_policy"

    if not expected or expected > _MAX_WORK_SECONDS:
        expected = _SYSTEM_DEFAULT_EXPECTED_SECONDS

    # ── working days / holiday / off-day ────────────────────────────────────
    working_days = _json_list(merged.get(K.SETTING_WORKING_DAYS),
                              K.DEFAULT_WORKING_DAYS)
    field_sources["working_days"] = src(K.SETTING_WORKING_DAYS)

    # Deprecated alias: honour flexible_clock_out_days when nothing configured
    # schedule.working_days. See _legacy_working_days.
    if not configured(K.SETTING_WORKING_DAYS):
        if ctx is not None:
            _flex = ctx.legacy_flexible_days
        else:
            _flex = _flex_days_or_none(_org_setting_raw(
                db, org_id, "flexible_clock_out_days"))
        _derived = _legacy_working_days(_flex)
        if _derived is not None:
            working_days = _derived
            field_sources["working_days"] = "legacy_flexible_days"

    if shift is not None and not configured(K.SETTING_WORKING_DAYS):
        dow = getattr(shift, "days_of_week", None)
        if isinstance(dow, list) and dow:
            try:
                working_days = [K.WEEKDAY_NAMES[int(d)] for d in dow
                                if 0 <= int(d) <= 6]
                field_sources["working_days"] = "shift"
            except (TypeError, ValueError):
                pass

    is_holiday = (on_date in ctx.holidays if ctx is not None
                  else on_date in _holiday_dates(db, org_id, [on_date]))
    weekday_name = K.WEEKDAY_NAMES[on_date.weekday()]
    is_working_day = (weekday_name in working_days) and not is_holiday

    # ── 1. ShiftAssignment — highest precedence ─────────────────────────────
    assignment = (ctx.assignments.get((getattr(user, "id", None), on_date.isoformat()))
                  if ctx is not None
                  else _load_assignment(db, org_id, getattr(user, "id", None), on_date))
    if assignment is not None:
        a_start = _hhmm_time(assignment.start_time)
        if a_start is not None:
            start_local, field_sources["start_local"] = a_start, "assignment"
        a_end = _hhmm_time(assignment.end_time)
        if a_end is not None:
            end_local, field_sources["end_local"] = a_end, "assignment"
        a_expected = _int_or(getattr(assignment, "expected_work_seconds", None), None)
        if a_expected and 0 < a_expected <= _MAX_WORK_SECONDS:
            expected, field_sources["expected_work_seconds"] = a_expected, "assignment"
        if assignment.shift_id:
            shift_id = assignment.shift_id
        # is_off_day is a P3 column; an explicit roster off-day wins over the
        # weekly pattern. Absent today -> no effect.
        if _bool_or(getattr(assignment, "is_off_day", None), False):
            is_working_day, field_sources["is_working_day"] = False, "assignment"
        elif not is_holiday:
            # A roster entry on a non-pattern day makes it a working day.
            is_working_day, field_sources["is_working_day"] = True, "assignment"

    # ── remaining scalars ───────────────────────────────────────────────────
    late_after = _int_or(merged.get(K.SETTING_LATE_AFTER_MINUTES), None)
    very_late = _int_or(merged.get(K.SETTING_VERY_LATE_AFTER_MIN), 120) or 120
    # The absence cutoff is whatever some tier configured, full stop.
    #
    # There is deliberately no employment-type arithmetic here any more. This
    # used to add six hours for anyone typed `part_time` — an offset inherited
    # from one customer's convention that part-timers start in the afternoon.
    # As a product rule it is wrong twice over: an organization whose
    # part-timers work mornings had them marked absent six hours late, and an
    # organization with no part-time concept still paid for the branch. An org
    # that wants a later cutoff for part-timers now sets
    # `schedule.absent_after_hour` at team or employee scope, which the cascade
    # above already resolves. The P2 migration writes exactly that row for
    # every existing part-timer, so no live behaviour changes.
    default_absent_hour = _int_or(_DEFAULTS[K.SETTING_ABSENT_AFTER_HOUR], 12) or 12
    absent_hour = _int_or(merged.get(K.SETTING_ABSENT_AFTER_HOUR), default_absent_hour)
    if absent_hour is None or not (0 <= absent_hour <= 23):
        _log.warning(
            "schedule.resolver %s=%r out of range for org=%s user=%s; using the "
            "system default %s",
            K.SETTING_ABSENT_AFTER_HOUR, merged.get(K.SETTING_ABSENT_AFTER_HOUR),
            org_id, getattr(user, "id", None), default_absent_hour,
        )
        absent_hour = default_absent_hour

    # The shift-relative cutoff. Empty at every tier -> None -> the hour above
    # still decides, so an organization that has configured nothing sees no
    # change. Out of range degrades to None (the previous rule) rather than to
    # a number nobody chose.
    absent_minutes = _int_or(merged.get(K.SETTING_ABSENT_AFTER_MINUTES), None)
    if absent_minutes is not None and not (0 <= absent_minutes <= _MAX_ABSENT_AFTER_MINUTES):
        _log.warning(
            "schedule.resolver %s=%r out of range for org=%s user=%s; falling "
            "back to %s",
            K.SETTING_ABSENT_AFTER_MINUTES, merged.get(K.SETTING_ABSENT_AFTER_MINUTES),
            org_id, getattr(user, "id", None), K.SETTING_ABSENT_AFTER_HOUR,
        )
        absent_minutes = None

    min_required = _hhmm_seconds(merged.get(K.SETTING_MIN_HOURS_REQUIRED)) or 0

    crosses_midnight = _bool_or(getattr(shift, "crosses_midnight", None), False)
    if not crosses_midnight and start_local and end_local:
        crosses_midnight = end_local <= start_local

    shift_config = getattr(shift, "config", None) if shift is not None else None
    if not isinstance(shift_config, dict):
        shift_config = {}

    for key, name in (
        (K.SETTING_LATE_AFTER_MINUTES, "late_after_minutes"),
        (K.SETTING_VERY_LATE_AFTER_MIN, "very_late_after_minutes"),
        (K.SETTING_MIN_HOURS_REQUIRED, "min_required_seconds"),
        (K.SETTING_ALLOW_EARLY_CLOCK_IN, "allow_early_clock_in"),
        (K.SETTING_ALLOW_LATE_CLOCK_OUT, "allow_late_clock_out"),
        (K.SETTING_WORK_MODE, "work_mode"),
        (K.SETTING_ABSENT_AFTER_HOUR, "absent_after_hour"),
        (K.SETTING_ABSENT_AFTER_MINUTES, "absent_after_minutes"),
    ):
        field_sources[name] = src(key)
    field_sources.setdefault("is_working_day", field_sources["working_days"])
    field_sources["shift_id"] = "assignment" if (
        assignment is not None and assignment.shift_id) else src(K.SETTING_SHIFT_ID)

    return EffectiveSchedule(
        on_date=on_date,
        timezone_name=(ctx.timezone_name if ctx is not None
                       else _tz.org_timezone_name(db, org_id)),
        is_working_day=is_working_day,
        is_holiday=is_holiday,
        engine_enabled=_bool_or(merged.get(K.SETTING_ENGINE_ENABLED), False),
        shift_id=shift_id,
        shift_name=getattr(shift, "name", None) if shift is not None else None,
        start_local=start_local,
        end_local=end_local,
        crosses_midnight=crosses_midnight,
        expected_work_seconds=expected,
        min_required_seconds=min_required,
        grace_minutes=max(0, grace or 0),
        late_after_minutes=late_after,
        very_late_after_minutes=very_late,
        absent_after_hour=absent_hour,
        absent_after_minutes=absent_minutes,
        allow_early_clock_in=_bool_or(merged.get(K.SETTING_ALLOW_EARLY_CLOCK_IN), True),
        allow_late_clock_out=_bool_or(merged.get(K.SETTING_ALLOW_LATE_CLOCK_OUT), True),
        employment_type=employment_type,
        work_mode=(str(merged.get(K.SETTING_WORK_MODE) or "onsite").strip().lower()),
        working_days=working_days,
        config=shift_config,
        sources=field_sources,
    )


def _system_default_schedule(on_date: date) -> EffectiveSchedule:
    """The fail-open result: pure system defaults, no DB involved."""
    d = _DEFAULTS
    return EffectiveSchedule(
        on_date=on_date,
        timezone_name=_tz.FALLBACK_TZ_NAME,
        is_working_day=K.WEEKDAY_NAMES[on_date.weekday()] in K.DEFAULT_WORKING_DAYS,
        is_holiday=False,
        engine_enabled=False,
        shift_id=None,
        shift_name=None,
        start_local=_hhmm_time(d[K.SETTING_SHIFT_START_TIME]),
        end_local=_hhmm_time(d[K.SETTING_SHIFT_END_TIME]),
        crosses_midnight=False,
        expected_work_seconds=(_hhmm_seconds(d[K.SETTING_EXPECTED_WORK_HOURS])
                               or _SYSTEM_DEFAULT_EXPECTED_SECONDS),
        min_required_seconds=_hhmm_seconds(d[K.SETTING_MIN_HOURS_REQUIRED]) or 0,
        grace_minutes=_int_or(d[K.SETTING_GRACE_MINUTES], 15) or 15,
        late_after_minutes=None,
        very_late_after_minutes=_int_or(d[K.SETTING_VERY_LATE_AFTER_MIN], 120) or 120,
        absent_after_hour=_int_or(d[K.SETTING_ABSENT_AFTER_HOUR], 12) or 12,
        absent_after_minutes=_int_or(d[K.SETTING_ABSENT_AFTER_MINUTES], None),
        allow_early_clock_in=True,
        allow_late_clock_out=True,
        employment_type="full_time",
        work_mode="onsite",
        working_days=list(K.DEFAULT_WORKING_DAYS),
        config={},
        sources={k: "system" for k in ("start_local", "end_local", "grace_minutes",
                                       "expected_work_seconds", "working_days",
                                       "employment_type", "is_working_day")},
    )


def resolve_work_schedules(db: Session, users: Sequence, on_date) -> Dict[int, EffectiveSchedule]:
    """Resolve many users at once — the entry point ``/employees/team`` and the
    reports layer will call, so those call sites never loop over the single
    resolver themselves.

    Currently a straightforward loop, and deliberately so: it is per-user
    correct, and there is no call site yet to measure. The holiday lookup, org
    timezone and per-tier settings reads are the obvious hoists when P7 wires
    this to a real endpoint — doing it now would be optimising blind. A test
    asserts this returns exactly what per-user resolution returns, so the
    optimisation can be made later without changing observable behaviour.
    """
    if isinstance(on_date, datetime):
        on_date = on_date.date()
    return resolve_work_schedules_over(db, users, [on_date]).get(on_date, {})


def week_start_date(db: Session, org_id, on_date: date) -> date:
    """The first day of the week containing ``on_date``, for this organization.

    Weekly rollups and weekly pay periods used ``d - timedelta(days=d.weekday())``,
    which hard-codes a Monday start. A Sun-Thu or Sat-start organization got a
    window that split its own working week in two.
    """
    raw = _org_setting_raw(db, org_id, K.SETTING_WEEK_START_DAY)
    name = str(raw or "").strip().upper() or _DEFAULTS[K.SETTING_WEEK_START_DAY]
    try:
        start_idx = K.WEEKDAY_NAMES.index(name)
    except ValueError:
        _log.warning("schedule.week_start_day=%r is not a weekday name for "
                     "org=%s; using the system default %s",
                     raw, org_id, _DEFAULTS[K.SETTING_WEEK_START_DAY])
        start_idx = K.WEEKDAY_NAMES.index(_DEFAULTS[K.SETTING_WEEK_START_DAY])
    return on_date - timedelta(days=(on_date.weekday() - start_idx) % 7)


def resolve_work_schedules_over(db: Session, users: Sequence, dates: Sequence):
    """Resolve MANY users over MANY dates with a fixed query count.

    Returns ``{date: {user_id: EffectiveSchedule}}``. One prefetch per
    organization (five queries), then pure in-memory resolution — so a monthly
    report over 59 users and 31 days costs ~5 queries instead of ~12,000.

    Users are grouped by organization so a mixed-tenant caller stays correctly
    isolated: each group only ever sees its own prefetched rows.
    """
    by_org: Dict[Any, list] = {}
    for u in users:
        by_org.setdefault(getattr(u, "organization_id", None), []).append(u)

    out: Dict[Any, Dict[int, EffectiveSchedule]] = {d: {} for d in dates}
    for org_id, group in by_org.items():
        ctx = build_context(db, org_id, group, dates)
        for d in dates:
            for u in group:
                out[d][getattr(u, "id", None)] = resolve_work_schedule(db, u, d, ctx)
    return out
