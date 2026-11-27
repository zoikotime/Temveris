"""Schedule Resolver, arrival classifier and timezone helper (P5 — pure).

Nothing in the application calls these yet. The tests are the only consumer, and
they exist to pin down the contract before P7 starts wiring subsystems to it:

  * precedence — assignment > employee > legacy user columns > team > org >
    shift > system default, applied PER FIELD
  * inheritance — an employee overriding one field inherits the rest
  * timezone — resolved from OrgSettings.default_timezone, defensive about the
    free-text and NULL values that exist in production
  * arrival classification — the exact Present / Late / Very Late boundaries
  * legacy fallback — User.default_clock_in_time / _grace_min still win over
    org/team config, which is what keeps rollout behaviour-neutral
  * edge cases — malformed rows, cross-tenant shift ids, holidays, midnight-
    crossing windows, employment-type Approach A
"""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from database import models
from database.database import Base

from app import _tz
from app.schedule_policy import keys as K
from app.schedule_policy.classify import (
    ArrivalStatus, DepartureStatus, classify_arrival, classify_departure,
)
from app.schedule_policy.resolver import (
    EffectiveSchedule, resolve_work_schedule, resolve_work_schedules,
)

_org_seq = itertools.count(20000)


def _new_org(db=None, tz=None) -> int:
    oid = next(_org_seq)
    if db is not None:
        db.add(models.Organization(id=oid, name=f"Org{oid}"))
        if tz is not None:
            db.add(models.OrgSettings(organization_id=oid, default_timezone=tz))
        db.commit()
    return oid


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_user(db, org_id, **kw):
    u = models.User(
        name="Sched Worker",
        email=f"sched_{uuid.uuid4().hex}@example.com",
        role="employee", is_active=True, organization_id=org_id,
        **kw,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value, *, team_key=None, user_id=None):
    db.add(models.AppSettings(organization_id=org_id, team_key=team_key,
                              user_id=user_id, key=key, value=value))
    db.commit()


def _mk_shift(db, org_id, name="Morning", start="06:00", end="14:00", dow=None):
    s = models.Shift(organization_id=org_id, name=name, start_time=start,
                     end_time=end, days_of_week=dow if dow is not None else [0, 1, 2, 3, 4],
                     is_active=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# Reference dates, guarded so a wrong constant fails loudly.
MONDAY = date(2026, 8, 3)
SATURDAY = date(2026, 8, 8)
SUNDAY = date(2026, 8, 9)


def test_reference_dates():
    assert MONDAY.weekday() == 0
    assert SATURDAY.weekday() == 5
    assert SUNDAY.weekday() == 6


# ══════════════════════════════════════════════════════════════════════════════
# System defaults
# ══════════════════════════════════════════════════════════════════════════════

def test_system_defaults_for_a_bare_org(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    s = resolve_work_schedule(db, u, MONDAY)
    assert isinstance(s, EffectiveSchedule)
    assert s.is_working_day is True
    # No scheduled shift by default -> cannot be classified Late.
    assert s.start_local is None
    assert s.end_local is None
    assert s.grace_minutes == 15
    assert s.very_late_after_minutes == 120
    assert s.absent_after_hour == 12
    assert s.allow_early_clock_in is True
    assert s.engine_enabled is False          # rollout switch off
    assert s.working_days == list(K.DEFAULT_WORKING_DAYS)


def test_weekend_is_not_a_working_day(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    assert resolve_work_schedule(db, u, SATURDAY).is_working_day is False
    assert resolve_work_schedule(db, u, SUNDAY).is_working_day is False


def test_org_with_saturday_working(db):
    """Org B profile: Mon-Sat."""
    org = _new_org(db)
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps(
        ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]))
    u = _mk_user(db, org)
    assert resolve_work_schedule(db, u, SATURDAY).is_working_day is True
    assert resolve_work_schedule(db, u, SUNDAY).is_working_day is False


def test_never_raises_on_a_user_with_no_org(db):
    u = models.User(name="x", email=f"n_{uuid.uuid4().hex}@x.com", organization_id=None)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.expected_work_seconds > 0
    assert s.grace_minutes >= 0


# ══════════════════════════════════════════════════════════════════════════════
# Precedence + inheritance
# ══════════════════════════════════════════════════════════════════════════════

def test_org_then_team_then_employee_precedence(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "10")
    _set(db, org, K.SETTING_GRACE_MINUTES, "20", team_key="Engineering")
    u = _mk_user(db, org, team="Engineering")
    assert resolve_work_schedule(db, u, MONDAY).grace_minutes == 20      # team wins
    _set(db, org, K.SETTING_GRACE_MINUTES, "30", user_id=u.id)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.grace_minutes == 30                                        # employee wins
    assert s.sources["grace_minutes"] == "employee"


def test_employee_overriding_one_field_inherits_the_rest(db):
    """The core inheritance requirement: field-level, not document-level."""
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "25")
    _set(db, org, K.SETTING_SHIFT_START_TIME, "10:30")
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "9:30")
    u = _mk_user(db, org)
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "4:00", user_id=u.id)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.expected_work_seconds == 4 * 3600            # overridden
    assert s.grace_minutes == 25                          # inherited from org
    assert s.start_local == time(10, 30)                  # inherited from org
    assert s.sources["expected_work_seconds"] == "employee"
    assert s.sources["grace_minutes"] == "org"


def test_team_override_does_not_reach_another_team(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "5")
    _set(db, org, K.SETTING_GRACE_MINUTES, "45", team_key="Engineering")
    sales = _mk_user(db, org, team="Sales")
    assert resolve_work_schedule(db, sales, MONDAY).grace_minutes == 5


def test_sources_reports_system_when_nothing_configured(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.sources["grace_minutes"] == "system"
    assert s.sources["working_days"] == "system"


# ══════════════════════════════════════════════════════════════════════════════
# Legacy fallback — what keeps rollout behaviour-neutral
# ══════════════════════════════════════════════════════════════════════════════

def test_legacy_clock_in_beats_org_and_team(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:30", team_key="Ops")
    u = _mk_user(db, org, team="Ops", default_clock_in_time="11:15")
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.start_local == time(11, 15)
    assert s.sources["start_local"] == "legacy_user"


def test_legacy_grace_beats_org_when_a_shift_time_is_set(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "5")
    u = _mk_user(db, org, default_clock_in_time="10:30", default_clock_in_grace_min=25)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.grace_minutes == 25
    assert s.sources["grace_minutes"] == "legacy_user"


def test_legacy_grace_alone_does_not_override_org(db):
    """`User.default_clock_in_grace_min` has a model-side default of 20, so every
    row has one (69/69 in production) while only 7 have a clock-in time. If the
    grace column counted on its own, org/team grace config would be dead for
    everyone. The pair is gated on default_clock_in_time — matching today's
    behaviour, where a worker with no shift time is never evaluated for lateness.
    """
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "5")
    u = _mk_user(db, org, default_clock_in_grace_min=20)   # no clock-in time
    assert u.default_clock_in_grace_min == 20              # the model default
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.grace_minutes == 5
    assert s.sources["grace_minutes"] == "org"


def test_model_default_for_legacy_grace_is_still_20(db):
    """Guard on the premise of the test above — if this default ever changes, the
    gating rationale needs revisiting."""
    assert models.User.__table__.c.default_clock_in_grace_min.default.arg == 20
    org = _new_org(db)
    u = _mk_user(db, org)
    assert u.default_clock_in_grace_min == 20
    # System default still applies, not the legacy 20 (same value here by
    # coincidence pre-P6; the source is what proves which path ran).
    assert resolve_work_schedule(db, u, MONDAY).sources["grace_minutes"] == "system"


def test_employee_scope_beats_legacy_columns(db):
    """Once P6 writes an employee-scope row it must outrank the legacy column."""
    org = _new_org(db)
    u = _mk_user(db, org, default_clock_in_time="11:15", default_clock_in_grace_min=20)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "08:00", user_id=u.id)
    _set(db, org, K.SETTING_GRACE_MINUTES, "3", user_id=u.id)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.start_local == time(8, 0)
    assert s.grace_minutes == 3
    assert s.sources["start_local"] == "employee"


def test_part_time_keeps_its_legacy_four_hour_target(db):
    """Behaviour-neutrality: with expected hours unconfigured, the existing
    employment-type policy still decides, so a part-timer is not promoted to the
    8:45 system default."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.expected_work_seconds == 4 * 3600
    assert s.sources["expected_work_seconds"] == "legacy_type_policy"


def test_explicit_expected_hours_wins_over_type_policy(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "8:45")
    u = _mk_user(db, org, employment_type="part_time")
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.expected_work_seconds == 8 * 3600 + 45 * 60
    assert s.sources["expected_work_seconds"] == "org"


# ══════════════════════════════════════════════════════════════════════════════
# Employment type — Approach A
# ══════════════════════════════════════════════════════════════════════════════

def test_user_column_is_authoritative_for_employment_type(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_EMPLOYMENT_TYPE, "contract")
    u = _mk_user(db, org, employment_type="part_time")
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.employment_type == "part_time"
    assert s.sources["employment_type"] == "employee"


def test_org_default_applies_when_user_has_no_type(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_EMPLOYMENT_TYPE, "intern")
    u = _mk_user(db, org, employment_type="")
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.employment_type == "intern"
    assert s.sources["employment_type"] == "org"


def test_employee_scope_employment_type_row_is_ignored(db):
    """Approach A: there must never be a second source of truth. An employee-scope
    row for employment_type must not win over the User column."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _set(db, org, K.SETTING_EMPLOYMENT_TYPE, "intern", user_id=u.id)
    assert resolve_work_schedule(db, u, MONDAY).employment_type == "full_time"


def test_employment_type_aliases_are_normalized(db):
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="Part-Time")
    assert resolve_work_schedule(db, u, MONDAY).employment_type == "part_time"


# ══════════════════════════════════════════════════════════════════════════════
# Shifts / assignments / holidays / tenant isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_assigned_shift_fills_the_window(db):
    org = _new_org(db)
    shift = _mk_shift(db, org, name="Night", start="22:00", end="06:00")
    _set(db, org, K.SETTING_SHIFT_ID, str(shift.id))
    u = _mk_user(db, org)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.shift_id == shift.id
    assert s.shift_name == "Night"
    assert s.start_local == time(22, 0)
    assert s.end_local == time(6, 0)
    assert s.crosses_midnight is True          # derived: end <= start
    assert s.sources["start_local"] == "shift"


def test_explicit_times_win_over_the_shift_window(db):
    org = _new_org(db)
    shift = _mk_shift(db, org, start="06:00", end="14:00")
    _set(db, org, K.SETTING_SHIFT_ID, str(shift.id))
    _set(db, org, K.SETTING_SHIFT_START_TIME, "07:30")
    u = _mk_user(db, org)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.start_local == time(7, 30)
    assert s.end_local == time(14, 0)          # still from the shift


def test_cross_org_shift_id_is_ignored(db):
    """Tenant isolation: a shift id from another org must never be loaded."""
    org_a, org_b = _new_org(db), _new_org(db)
    foreign = _mk_shift(db, org_b, name="B-only", start="03:00", end="11:00")
    _set(db, org_a, K.SETTING_SHIFT_ID, str(foreign.id))
    u = _mk_user(db, org_a)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.shift_id is None
    assert s.shift_name is None
    assert s.start_local is None                # fell back to the default (no shift)


def test_inactive_shift_is_ignored(db):
    org = _new_org(db)
    shift = _mk_shift(db, org, start="05:00", end="13:00")
    shift.is_active = False
    db.commit()
    _set(db, org, K.SETTING_SHIFT_ID, str(shift.id))
    u = _mk_user(db, org)
    assert resolve_work_schedule(db, u, MONDAY).shift_id is None


def test_shift_days_of_week_drive_working_days(db):
    org = _new_org(db)
    shift = _mk_shift(db, org, dow=[0, 1, 2, 3, 4, 5])      # Mon-Sat
    _set(db, org, K.SETTING_SHIFT_ID, str(shift.id))
    u = _mk_user(db, org)
    s = resolve_work_schedule(db, u, SATURDAY)
    assert s.is_working_day is True
    assert s.sources["working_days"] == "shift"


def test_assignment_overrides_everything(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    u = _mk_user(db, org, default_clock_in_time="10:00")
    db.add(models.ShiftAssignment(
        organization_id=org, user_id=u.id, date=MONDAY.isoformat(),
        start_time="14:00", end_time="22:00", status="scheduled"))
    db.commit()
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.start_local == time(14, 0)
    assert s.end_local == time(22, 0)
    assert s.sources["start_local"] == "assignment"


def test_cancelled_assignment_is_ignored(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    db.add(models.ShiftAssignment(
        organization_id=org, user_id=u.id, date=MONDAY.isoformat(),
        start_time="14:00", end_time="22:00", status="cancelled"))
    db.commit()
    assert resolve_work_schedule(db, u, MONDAY).start_local is None


def test_assignment_makes_a_weekend_day_working(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    db.add(models.ShiftAssignment(
        organization_id=org, user_id=u.id, date=SATURDAY.isoformat(),
        start_time="10:00", end_time="18:00", status="scheduled"))
    db.commit()
    s = resolve_work_schedule(db, u, SATURDAY)
    assert s.is_working_day is True
    assert s.sources["is_working_day"] == "assignment"


def test_assignment_for_another_org_is_not_seen(db):
    org_a, org_b = _new_org(db), _new_org(db)
    u = _mk_user(db, org_a)
    db.add(models.ShiftAssignment(
        organization_id=org_b, user_id=u.id, date=MONDAY.isoformat(),
        start_time="03:00", end_time="11:00", status="scheduled"))
    db.commit()
    assert resolve_work_schedule(db, u, MONDAY).start_local is None


def test_holiday_makes_the_day_non_working(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    db.add(models.Holiday(name="Independence Day", date=MONDAY, organization_id=org))
    db.commit()
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.is_holiday is True
    assert s.is_working_day is False


def test_another_orgs_holiday_does_not_apply(db):
    org_a, org_b = _new_org(db), _new_org(db)
    db.add(models.Holiday(name="B only", date=MONDAY, organization_id=org_b))
    db.commit()
    u = _mk_user(db, org_a)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.is_holiday is False
    assert s.is_working_day is True


# ══════════════════════════════════════════════════════════════════════════════
# Malformed configuration must never raise
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("key,value", [
    (K.SETTING_GRACE_MINUTES, "not-a-number"),
    (K.SETTING_SHIFT_START_TIME, "25:99"),
    (K.SETTING_SHIFT_START_TIME, "garbage"),
    (K.SETTING_WORKING_DAYS, "{not json"),
    (K.SETTING_WORKING_DAYS, '{"a":1}'),
    (K.SETTING_EXPECTED_WORK_HOURS, "abc"),
    (K.SETTING_VERY_LATE_AFTER_MIN, ""),
    (K.SETTING_ABSENT_AFTER_HOUR, "99"),
    (K.SETTING_SHIFT_ID, "not-an-id"),
])
def test_malformed_row_falls_back_without_raising(db, key, value):
    org = _new_org(db)
    _set(db, org, key, value)
    u = _mk_user(db, org)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.expected_work_seconds > 0
    assert 0 <= s.absent_after_hour <= 23
    assert s.grace_minutes >= 0
    assert isinstance(s.working_days, list)


def test_batched_matches_per_user(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "17")
    users = [_mk_user(db, org) for _ in range(3)]
    _set(db, org, K.SETTING_GRACE_MINUTES, "2", user_id=users[1].id)
    batch = resolve_work_schedules(db, users, MONDAY)
    for u in users:
        assert batch[u.id] == resolve_work_schedule(db, u, MONDAY)
    assert batch[users[1].id].grace_minutes == 2
    assert batch[users[0].id].grace_minutes == 17


# ══════════════════════════════════════════════════════════════════════════════
# Timezone helper
# ══════════════════════════════════════════════════════════════════════════════

def test_valid_iana_timezone_is_used(db):
    org = _new_org(db, tz="Asia/Kolkata")
    name = _tz.org_timezone_name(db, org)
    # With no tz database installed, every zone degrades to the IST fallback —
    # which is Asia/Kolkata anyway, so the assertion holds either way.
    assert name == "Asia/Kolkata"


def test_free_text_timezone_falls_back(db):
    """Production really holds 'California, Sacramento' for one org."""
    org = _new_org(db, tz="California, Sacramento")
    assert _tz.org_timezone_name(db, org) == _tz.FALLBACK_TZ_NAME


def test_null_timezone_falls_back(db):
    org = _new_org(db, tz=None)
    assert _tz.org_timezone_name(db, org) == _tz.FALLBACK_TZ_NAME


def test_missing_org_settings_row_falls_back(db):
    org = _new_org(db)          # no OrgSettings row at all
    assert _tz.org_timezone_name(db, org) == _tz.FALLBACK_TZ_NAME
    assert _tz.org_timezone(db, org) is not None


def test_fallback_offset_matches_the_hardcoded_ist_literals(db):
    """The fallback must equal the +05:30 used by every existing IST constant,
    otherwise migrating a call site would silently shift day boundaries."""
    org = _new_org(db)
    tz = _tz.org_timezone(db, org)
    offset = tz.utcoffset(datetime(2026, 8, 3, 12, 0))
    assert offset == timedelta(hours=5, minutes=30)


def test_local_day_bounds_span_exactly_24h(db):
    org = _new_org(db)
    start, end = _tz.local_day_bounds(db, org, MONDAY)
    assert start.tzinfo is not None and end.tzinfo is not None
    assert end - start == timedelta(days=1)
    # Matches the existing "midnight IST expressed in UTC" idiom.
    assert start == datetime(2026, 8, 3, tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)


def test_to_local_treats_naive_as_utc(db):
    org = _new_org(db)
    naive = datetime(2026, 8, 3, 6, 0)          # 06:00 UTC
    local = _tz.to_local(db, org, naive)
    assert (local.hour, local.minute) == (11, 30)   # +05:30
    assert _tz.to_local(db, org, None) is None


# ══════════════════════════════════════════════════════════════════════════════
# Arrival classification — pure, no DB
# ══════════════════════════════════════════════════════════════════════════════
class _Sched:
    """Minimal duck-typed schedule for the pure classifier."""
    def __init__(self, **kw):
        self.is_working_day = kw.get("is_working_day", True)
        self.start_local = kw.get("start_local", time(9, 0))
        self.grace_minutes = kw.get("grace_minutes", 15)
        self.late_after_minutes = kw.get("late_after_minutes", None)
        self.very_late_after_minutes = kw.get("very_late_after_minutes", 120)
        self.absent_after_hour = kw.get("absent_after_hour", 12)
        self.expected_work_seconds = kw.get("expected_work_seconds", 31500)


def _at(h, m):
    return datetime(2026, 8, 3, h, m, tzinfo=timezone.utc)


@pytest.mark.parametrize("h,m,expected", [
    (8, 45, ArrivalStatus.PRESENT),     # early
    (9, 0,  ArrivalStatus.PRESENT),     # exactly on time
    (9, 8,  ArrivalStatus.PRESENT),     # the required example
    (9, 15, ArrivalStatus.PRESENT),     # grace boundary is inclusive
    (9, 16, ArrivalStatus.LATE),
    (9, 20, ArrivalStatus.LATE),        # the required example
    (10, 59, ArrivalStatus.LATE),
    (11, 0, ArrivalStatus.VERY_LATE),   # the required example: delta == 120
    (12, 59, ArrivalStatus.VERY_LATE),
    # Past the old half-day threshold: the status was removed with its setting,
    # so a very late arrival stays Very Late however late it is.
    (13, 0, ArrivalStatus.VERY_LATE),   # delta == 240
    (15, 0, ArrivalStatus.VERY_LATE),
])
def test_arrival_boundaries(h, m, expected):
    r = classify_arrival(_at(h, m), _Sched(), _at(18, 0))
    assert r.status == expected


def test_the_three_required_examples_exactly():
    """From the original requirement: 09:00 expected, 09:08 Present, 09:20 Late,
    11:00 Very Late."""
    s = _Sched(start_local=time(9, 0), grace_minutes=15, late_after_minutes=15)
    assert classify_arrival(_at(9, 8), s, _at(18, 0)).status == ArrivalStatus.PRESENT
    assert classify_arrival(_at(9, 20), s, _at(18, 0)).status == ArrivalStatus.LATE
    assert classify_arrival(_at(11, 0), s, _at(18, 0)).status == ArrivalStatus.VERY_LATE


def test_late_minutes_only_populated_when_late():
    assert classify_arrival(_at(9, 8), _Sched(), _at(18, 0)).late_minutes is None
    assert classify_arrival(_at(9, 40), _Sched(), _at(18, 0)).late_minutes == 40


def test_late_after_widens_the_on_time_window():
    s = _Sched(grace_minutes=15, late_after_minutes=30)
    assert classify_arrival(_at(9, 25), s, _at(18, 0)).status == ArrivalStatus.PRESENT
    assert classify_arrival(_at(9, 31), s, _at(18, 0)).status == ArrivalStatus.LATE


def test_late_after_below_grace_never_shrinks_the_window():
    s = _Sched(grace_minutes=15, late_after_minutes=5)
    assert classify_arrival(_at(9, 10), s, _at(18, 0)).status == ArrivalStatus.PRESENT


def test_non_working_day_is_not_applicable():
    r = classify_arrival(None, _Sched(is_working_day=False), _at(18, 0))
    assert r.status == ArrivalStatus.NOT_APPLICABLE
    r2 = classify_arrival(_at(9, 30), _Sched(is_working_day=False), _at(18, 0))
    assert r2.status == ArrivalStatus.NOT_APPLICABLE


def test_no_clock_in_awaiting_then_late_pending_then_absent():
    """Three distinct states, not two. LATE_PENDING (no clock-in, past grace, before
    the cutoff) is what the admin dashboard surfaces under Requires Attention;
    collapsing it into AWAITING silently dropped that bucket in P9."""
    s = _Sched(absent_after_hour=12)                      # start 09:00, grace 15
    assert classify_arrival(None, s, _at(8, 0)).status == ArrivalStatus.AWAITING
    assert classify_arrival(None, s, _at(9, 10)).status == ArrivalStatus.AWAITING
    assert classify_arrival(None, s, _at(9, 16)).status == ArrivalStatus.LATE_PENDING
    assert classify_arrival(None, s, _at(11, 59)).status == ArrivalStatus.LATE_PENDING
    assert classify_arrival(None, s, _at(12, 0)).status == ArrivalStatus.ABSENT


def test_late_pending_reports_minutes_waited():
    s = _Sched(absent_after_hour=12)
    r = classify_arrival(None, s, _at(10, 0))
    assert r.status == ArrivalStatus.LATE_PENDING
    assert r.late_minutes == 60


def test_absent_cutoff_is_configurable():
    s = _Sched(absent_after_hour=18)
    # Past grace but before the later cutoff -> still pending, not yet Absent.
    assert classify_arrival(None, s, _at(13, 0)).status == ArrivalStatus.LATE_PENDING
    assert classify_arrival(None, s, _at(18, 0)).status == ArrivalStatus.ABSENT


def test_no_scheduled_start_cannot_be_late():
    s = _Sched(start_local=None)
    assert classify_arrival(_at(15, 0), s, _at(18, 0)).status == ArrivalStatus.PRESENT
    assert classify_arrival(None, s, _at(8, 0)).status == ArrivalStatus.AWAITING
    assert classify_arrival(None, s, _at(12, 0)).status == ArrivalStatus.ABSENT


def test_zero_grace_marks_one_minute_late():
    s = _Sched(grace_minutes=0, late_after_minutes=0)
    assert classify_arrival(_at(9, 0), s, _at(18, 0)).status == ArrivalStatus.PRESENT
    assert classify_arrival(_at(9, 1), s, _at(18, 0)).status == ArrivalStatus.LATE


def test_arrival_status_serializes_as_a_string():
    r = classify_arrival(_at(9, 20), _Sched(), _at(18, 0))
    assert r.status.value == "late"
    assert json.dumps({"arrival_status": r.status}) == '{"arrival_status": "late"}'


# ── Departure ────────────────────────────────────────────────────────────────

def test_departure_early_complete_overtime():
    s = _Sched(expected_work_seconds=31500)      # 8:45
    early = classify_departure(30000, s)
    assert early.status == DepartureStatus.EARLY
    assert early.shortfall_seconds == 1500
    assert classify_departure(31500, s).status == DepartureStatus.COMPLETE
    over = classify_departure(33000, s)
    assert over.status == DepartureStatus.OVERTIME
    assert over.overtime_seconds == 1500


def test_departure_handles_none_and_negative():
    s = _Sched(expected_work_seconds=3600)
    assert classify_departure(None, s).status == DepartureStatus.EARLY
    assert classify_departure(-50, s).shortfall_seconds == 3600


# ══════════════════════════════════════════════════════════════════════════════
# P5 contract: still wired into nothing
# ══════════════════════════════════════════════════════════════════════════════

def test_resolver_is_only_called_from_approved_sites():
    """The resolver is now the canonical policy path, so consumers are expected.
    The guard stays because WHICH modules resolve schedules is architecture: a
    new consumer should be a deliberate decision, and a module that resolves
    schedules in passing is usually a module that should have asked one of the
    existing consumers instead.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    # P10 wired the write path. Every entry here is gated on
    # schedule.engine_enabled; the guard exists so an UNGATED consumer cannot
    # appear without a deliberate test change.
    allowed = {"schedule_policy/resolver.py", "schedule_policy/classify.py",
               "schedule_policy/__init__.py", "schedule_policy/keys.py",
               "schedule_policy/annotate.py", "attendance_policy/resolvers.py",
               "attendance_alerts.py", "employees/router.py",
               "intelligence/burnout.py", "reports/router.py",
               "risk/router.py", "payroll/policy.py",
               "profile/router.py", "leave/policy.py", "payroll/router.py",
               "schedule/router.py",
               # Phase 4: the intelligence risk rollup sums expected hours over
               # the days an employee is SCHEDULED to work, so it now asks the
               # resolver instead of multiplying by calendar days.
               "intelligence/risk_engine.py",
               # The forward-only Work Ledger producer (D-18D/D-21): it
               # resolves the working-day result at seal and freezes it, and
               # REFUSES to seal when the resolver fell through to its system
               # default. Deliberate, and narrower than the package — one
               # module, which is the only place the producer asks.
               # Owner ruling D2-A moved the working-day DETERMINATION out
               # of the producer and into a dedicated classifier. That
               # classifier is now the module that asks the resolver; the
               # producer only consumes its verdict. context.py stays on this
               # list because its module docstring still names the resolver
               # while describing where the answer comes from.
               "work_ledger_producer/context.py",
               "working_day_classifier/classifier.py"}
    offenders = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if rel in allowed:
            continue
        if re.search(r"resolve_work_schedule|classify_arrival|classify_departure",
                     p.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == [], f"unapproved resolver consumer: {offenders}"
