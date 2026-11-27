"""Two organizations, two completely different workforce policies, one process.

This is the acceptance test for the whole multi-tenant settings refactor. It
does not test a module; it tests the PRODUCT CLAIM:

    Organization A          Organization B
    Mon-Fri                 Mon-Sat
    09:00-18:00             08:00-17:00
    8:00 expected           7:30 expected
    15 min grace            30 min grace
    absent at 12:00         absent at 14:00
    Asia/Kolkata            America/New_York

Both must operate simultaneously, correctly, with no source change — and
neither may be able to alter the other's behaviour.

Everything here goes through the real resolver and the real classifier. Nothing
is stubbed, because the failure mode being guarded against is precisely a
module quietly using its own constant instead of the resolved value.
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
from app.schedule_policy.classify import ArrivalStatus, classify_arrival
from app.schedule_policy.resolver import resolve_work_schedule, resolve_work_schedules

_org_seq = itertools.count(77000)

MONDAY = date(2026, 8, 3)
SATURDAY = date(2026, 8, 8)
SUNDAY = date(2026, 8, 9)

ALL_BUT_SUNDAY = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]
MON_TO_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_org(db, *, timezone_name, working_days, start, end, expected,
            grace, absent_hour) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Org{oid}"))
    db.add(models.OrgSettings(organization_id=oid, default_timezone=timezone_name))
    rows = {
        K.SETTING_WORKING_DAYS: json.dumps(working_days),
        K.SETTING_SHIFT_START_TIME: start,
        K.SETTING_SHIFT_END_TIME: end,
        K.SETTING_EXPECTED_WORK_HOURS: expected,
        K.SETTING_GRACE_MINUTES: str(grace),
        K.SETTING_ABSENT_AFTER_HOUR: str(absent_hour),
    }
    for k, v in rows.items():
        db.add(models.AppSettings(organization_id=oid, key=k, value=v))
    db.commit()
    return oid


def _mk_user(db, org_id, **kw):
    u = models.User(name="W", email=f"mt_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value, *, team_key=None, user_id=None):
    db.add(models.AppSettings(organization_id=org_id, key=key, value=value,
                              team_key=team_key, user_id=user_id))
    db.commit()


@pytest.fixture
def org_a(db):
    """Asia/Kolkata, Mon-Fri, 09:00-18:00, 8h, 15m grace, absent at noon."""
    return _mk_org(db, timezone_name="Asia/Kolkata", working_days=MON_TO_FRI,
                   start="09:00", end="18:00", expected="8:00",
                   grace=15, absent_hour=12)


@pytest.fixture
def org_b(db):
    """America/New_York, Mon-Sat, 08:00-17:00, 7h30, 30m grace, absent at 14."""
    return _mk_org(db, timezone_name="America/New_York",
                   working_days=ALL_BUT_SUNDAY,
                   start="08:00", end="17:00", expected="7:30",
                   grace=30, absent_hour=14)


# ══════════════════════════════════════════════════════════════════════════════
# Each organization gets its own answers
# ══════════════════════════════════════════════════════════════════════════════

def test_both_organizations_resolve_their_own_schedule(db, org_a, org_b):
    a = resolve_work_schedule(db, _mk_user(db, org_a), MONDAY)
    b = resolve_work_schedule(db, _mk_user(db, org_b), MONDAY)

    assert (a.start_local, a.end_local) == (time(9, 0), time(18, 0))
    assert (b.start_local, b.end_local) == (time(8, 0), time(17, 0))
    assert a.expected_work_seconds == 8 * 3600
    assert b.expected_work_seconds == 7 * 3600 + 30 * 60
    assert (a.grace_minutes, b.grace_minutes) == (15, 30)
    assert (a.absent_after_hour, b.absent_after_hour) == (12, 14)
    assert a.timezone_name == "Asia/Kolkata"
    assert b.timezone_name == "America/New_York"


def test_working_days_differ(db, org_a, org_b):
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    # Saturday: a weekly off for A, a working day for B.
    assert resolve_work_schedule(db, ua, SATURDAY).is_working_day is False
    assert resolve_work_schedule(db, ub, SATURDAY).is_working_day is True
    # Sunday is off for both.
    assert resolve_work_schedule(db, ua, SUNDAY).is_working_day is False
    assert resolve_work_schedule(db, ub, SUNDAY).is_working_day is False


def test_timezones_are_real_and_different(db, org_a, org_b):
    """Not just a stored string — the offsets must actually differ."""
    a_now = _tz.local_now(db, org_a)
    b_now = _tz.local_now(db, org_b)
    assert a_now.utcoffset() != b_now.utcoffset()
    # And the day boundary each organization uses is its own.
    a_start, _ = _tz.local_day_bounds(db, org_a, MONDAY)
    b_start, _ = _tz.local_day_bounds(db, org_b, MONDAY)
    assert a_start != b_start


def test_the_same_instant_is_a_different_local_day(db, org_a, org_b):
    """03:00 UTC on 4 Aug is 08:30 Monday in Kolkata and 23:00 Sunday in NY."""
    instant = datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)
    assert _tz.to_local(db, org_a, instant).date() == date(2026, 8, 3)
    assert _tz.to_local(db, org_b, instant).date() == date(2026, 8, 2)


# ══════════════════════════════════════════════════════════════════════════════
# The same clock-in classifies differently in each organization
# ══════════════════════════════════════════════════════════════════════════════

def _local(db, org_id, on_date, hh, mm=0):
    return datetime.combine(on_date, time(hh, mm), tzinfo=_tz.org_timezone(db, org_id))


@pytest.mark.parametrize("hh,mm,a_expected,b_expected", [
    # A starts 09:00+15m grace; B starts 08:00+30m grace.
    (8, 20, ArrivalStatus.PRESENT, ArrivalStatus.PRESENT),   # early for A, in grace for B
    (8, 45, ArrivalStatus.PRESENT, ArrivalStatus.LATE),      # early for A, past B's grace
    (9, 10, ArrivalStatus.PRESENT, ArrivalStatus.LATE),      # in A's grace, late for B
    (9, 40, ArrivalStatus.LATE,    ArrivalStatus.LATE),      # late for both
])
def test_one_arrival_time_two_verdicts(db, org_a, org_b, hh, mm, a_expected, b_expected):
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    sa = resolve_work_schedule(db, ua, MONDAY)
    sb = resolve_work_schedule(db, ub, MONDAY)
    a_in = _local(db, org_a, MONDAY, hh, mm)
    b_in = _local(db, org_b, MONDAY, hh, mm)
    assert classify_arrival(a_in, sa, a_in).status == a_expected
    assert classify_arrival(b_in, sb, b_in).status == b_expected


@pytest.mark.parametrize("hour,a_expected,b_expected", [
    (11, ArrivalStatus.LATE_PENDING, ArrivalStatus.LATE_PENDING),
    (12, ArrivalStatus.ABSENT,       ArrivalStatus.LATE_PENDING),
    (14, ArrivalStatus.ABSENT,       ArrivalStatus.ABSENT),
])
def test_absence_cutoffs_differ(db, org_a, org_b, hour, a_expected, b_expected):
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    sa = resolve_work_schedule(db, ua, MONDAY)
    sb = resolve_work_schedule(db, ub, MONDAY)
    assert classify_arrival(None, sa, _local(db, org_a, MONDAY, hour)).status == a_expected
    assert classify_arrival(None, sb, _local(db, org_b, MONDAY, hour)).status == b_expected


# ══════════════════════════════════════════════════════════════════════════════
# Isolation: changing A must not move B
# ══════════════════════════════════════════════════════════════════════════════

def test_changing_a_does_not_change_b(db, org_a, org_b):
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    before = resolve_work_schedule(db, ub, MONDAY)

    _set(db, org_a, K.SETTING_GRACE_MINUTES, "90")
    _set(db, org_a, K.SETTING_ABSENT_AFTER_HOUR, "20")
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "12:00")
    _set(db, org_a, K.SETTING_WORKING_DAYS, json.dumps(["SUNDAY"]))

    after_a = resolve_work_schedule(db, ua, MONDAY)
    after_b = resolve_work_schedule(db, ub, MONDAY)

    assert (after_a.grace_minutes, after_a.absent_after_hour) == (90, 20)
    assert after_a.expected_work_seconds == 12 * 3600
    assert after_a.is_working_day is False              # A now works Sundays only

    assert after_b.grace_minutes == before.grace_minutes == 30
    assert after_b.absent_after_hour == before.absent_after_hour == 14
    assert after_b.expected_work_seconds == before.expected_work_seconds
    assert after_b.is_working_day is True


def test_one_orgs_holiday_does_not_reach_the_other(db, org_a, org_b):
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    db.add(models.Holiday(name="A only", date=MONDAY, organization_id=org_a))
    db.commit()
    assert resolve_work_schedule(db, ua, MONDAY).is_working_day is False
    assert resolve_work_schedule(db, ub, MONDAY).is_working_day is True


def test_a_mixed_tenant_batch_resolves_each_in_its_own_frame(db, org_a, org_b):
    """resolve_work_schedules groups by organization; a caller sweeping several
    tenants must not get one organization's prefetched rows applied to another."""
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    out = resolve_work_schedules(db, [ua, ub], SATURDAY)
    assert out[ua.id].is_working_day is False
    assert out[ub.id].is_working_day is True
    assert out[ua.id].grace_minutes == 15
    assert out[ub.id].grace_minutes == 30


# ══════════════════════════════════════════════════════════════════════════════
# Consumers agree with the resolver — no module keeps its own number
# ══════════════════════════════════════════════════════════════════════════════

def test_clock_out_gate_uses_each_orgs_expected_hours(db, org_a, org_b):
    from app.attendance_policy import resolve_expected_work_seconds
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    assert resolve_expected_work_seconds(db, ua, MONDAY) == 8 * 3600
    assert resolve_expected_work_seconds(db, ub, MONDAY) == 7 * 3600 + 30 * 60


def test_risk_engine_uses_each_orgs_expected_hours(db, org_a, org_b):
    from app.risk.router import _threshold_secs
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    assert _threshold_secs(db, ua) == 8 * 3600
    assert _threshold_secs(db, ub) == 7 * 3600 + 30 * 60


def test_intelligence_uses_each_orgs_expected_hours(db, org_a, org_b):
    from app.intelligence.risk_engine import _expected_hours_per_day
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    assert _expected_hours_per_day(db, ua) == 8.0
    assert _expected_hours_per_day(db, ub) == 7.5


def test_flexible_clock_out_day_follows_each_orgs_working_days(db, org_a, org_b):
    from app.attendance_policy import is_flexible_clock_out_day
    # Saturday: unrestricted for A (not a working day), restricted for B.
    assert is_flexible_clock_out_day(db, org_a, SATURDAY) is True
    assert is_flexible_clock_out_day(db, org_b, SATURDAY) is False


def test_break_workday_boundary_follows_each_orgs_timezone(db, org_a, org_b):
    from app.breaks import governance as gov
    instant = datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)
    assert gov.workday_date(db, org_a, instant) == date(2026, 8, 3)
    assert gov.workday_date(db, org_b, instant) == date(2026, 8, 2)


def test_payroll_divisor_is_per_organization(db, org_a, org_b):
    from app.payroll import policy as pp
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    _set(db, org_a, pp.SETTING_LWP_DIVISOR_DAYS, "26")
    assert pp.divisor_days(db, ua) == 26.0
    assert pp.divisor_days(db, ub) == 30.0          # B keeps the system default


def test_payroll_working_day_divisor_uses_each_orgs_week(db, org_a, org_b):
    """Mon-Sat gives B more working days in the same period than Mon-Fri gives A."""
    from app.payroll import policy as pp
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    start, end = date(2026, 8, 3), date(2026, 8, 16)      # two full weeks
    assert pp.working_days_between(db, ua, start, end) == 10
    assert pp.working_days_between(db, ub, start, end) == 12


def test_profile_reports_policy_and_cannot_override_it(db, org_a, org_b):
    """The employee's own profile is a read-out of the cascade, not a fifth
    source of expected hours. Before this it stored a self-service
    `daily_target_hours` that disagreed with reports and payroll."""
    from app.profile.router import _schedule_view, _serialise_prefs, UpdatePrefsRequest
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)

    va, vb = _schedule_view(db, ua), _schedule_view(db, ub)
    assert (va["daily_target_hours"], va["work_start_time"]) == (8.0, "09:00")
    assert (vb["daily_target_hours"], vb["work_start_time"]) == (7.5, "08:00")
    assert va["work_days"] == [0, 1, 2, 3, 4]
    assert vb["work_days"] == [0, 1, 2, 3, 4, 5]
    assert (va["timezone"], vb["timezone"]) == ("Asia/Kolkata", "America/New_York")

    # An employee override goes through the cascade, not through the profile.
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "6:00", user_id=ua.id)
    assert _schedule_view(db, ua)["daily_target_hours"] == 6.0

    # And the write endpoint's schedule fields are inert.
    prefs = models.UserPreferences(user_id=ua.id, daily_target_hours=12.0)
    db.add(prefs); db.commit()
    body = UpdatePrefsRequest(daily_target_hours=12.0, work_start_time="03:00")
    assert body.daily_target_hours == 12.0          # still accepted, never applied
    assert _serialise_prefs(prefs, _schedule_view(db, ua))["daily_target_hours"] == 6.0


def test_week_starts_on_each_organizations_own_day(db, org_a, org_b):
    """Weekly rollups and weekly pay periods used `d - timedelta(d.weekday())`,
    which is a Monday week for everybody."""
    from app.schedule_policy import week_start_date
    _set(db, org_b, K.SETTING_WEEK_START_DAY, "SUNDAY")
    wednesday = date(2026, 8, 12)
    assert week_start_date(db, org_a, wednesday) == date(2026, 8, 10)   # Mon
    assert week_start_date(db, org_b, wednesday) == date(2026, 8, 9)    # Sun
    # A nonsense value falls back to the SYSTEM default, not to org A's.
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Junk{oid}"))
    db.commit()
    _set(db, oid, K.SETTING_WEEK_START_DAY, "Someday")
    assert week_start_date(db, oid, wednesday) == date(2026, 8, 10)


def test_leave_year_start_is_per_organization(db, org_a, org_b):
    """Jan 1 was hard-coded in three places and read off the UTC clock."""
    from app.leave import policy as lp
    _set(db, org_b, lp.SETTING_YEAR_START_MONTH, "4")
    a = lp.leave_year_start(db, org_a)
    b = lp.leave_year_start(db, org_b)
    assert (a.month, a.day) == (1, 1)
    assert (b.month, b.day) == (4, 1)
    # Months-into-the-year paces accrual; it must follow the same boundary.
    assert lp.months_into_leave_year(db, org_a) >= 1
    assert lp.months_into_leave_year(db, org_b) >= 1


def test_non_working_account_names_are_per_organization(db, org_a, org_b):
    """Excluding a seat called "Administrator" was a global name match, so a
    tenant with a real employee of that name lost them from attendance."""
    from app._system_admin import (
        SETTING_NON_WORKING_NAMES, is_system_admin, exclude_system_admin,
    )
    _set(db, org_b, SETTING_NON_WORKING_NAMES, "")
    admin_a = _mk_user(db, org_a); admin_a.name = "Administrator"
    admin_b = _mk_user(db, org_b); admin_b.name = "Administrator"
    db.commit()
    assert is_system_admin(admin_a, db) is True
    assert is_system_admin(admin_b, db) is False, "org B cleared the list"
    assert exclude_system_admin([admin_a], db) == []
    assert exclude_system_admin([admin_b], db) == [admin_b]


def test_risk_deficit_thresholds_are_per_organization(db, org_a, org_b):
    """The 0.75 / 0.5 ratios behind the admin risk list were literals."""
    from app.attendance_policy import (
        policy_int, SETTING_RISK_DEFICIT_PCT, SETTING_RISK_HIGH_PCT,
    )
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    _set(db, org_b, SETTING_RISK_DEFICIT_PCT, "90")
    assert policy_int(db, ua, SETTING_RISK_DEFICIT_PCT) == 75
    assert policy_int(db, ub, SETTING_RISK_DEFICIT_PCT) == 90
    assert policy_int(db, ub, SETTING_RISK_HIGH_PCT) == 50


def test_worked_seconds_has_one_definition(db, org_a):
    """risk, reports and payroll each had their own arithmetic; two of the
    three forgot offline_seconds and two double-subtracted breaks."""
    from app._worked import session_worked_seconds
    from app.reports.router import _session_seconds
    from app.risk.router import _session_worked_secs

    class S:
        clock_in_time = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
        clock_out_time = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
        break_seconds = 1800
        offline_seconds = 600
        total_seconds = 0          # not finalised -> everyone must recompute

    s = S()
    expected = 9 * 3600 - 1800 - 600
    now = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    assert session_worked_seconds(s, now) == expected
    assert _session_seconds(s, now) == expected
    assert _session_worked_secs(s) == expected

    # An in-progress session counts up to `now`, it does not read as zero.
    s.clock_out_time = None
    assert session_worked_seconds(s, now) == 10 * 3600 - 1800 - 600


def test_alert_recipients_are_per_organization(db, org_a, org_b):
    from app.notifications.recipients import alert_recipients
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == org_a).update({"hr_email": "hr-a@a.test"})
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == org_b).update({"hr_email": "hr-b@b.test"})
    db.commit()
    assert alert_recipients(db, org_a) == ["hr-a@a.test"]
    assert alert_recipients(db, org_b) == ["hr-b@b.test"]


# ══════════════════════════════════════════════════════════════════════════════
# The full cascade, for every major policy
# ══════════════════════════════════════════════════════════════════════════════

CASCADE_KEYS = [
    (K.SETTING_SHIFT_START_TIME, "07:00", lambda s: s.start_local == time(7, 0)),
    (K.SETTING_SHIFT_END_TIME, "16:00", lambda s: s.end_local == time(16, 0)),
    (K.SETTING_EXPECTED_WORK_HOURS, "6:00", lambda s: s.expected_work_seconds == 6 * 3600),
    (K.SETTING_GRACE_MINUTES, "45", lambda s: s.grace_minutes == 45),
    (K.SETTING_LATE_AFTER_MINUTES, "60", lambda s: s.late_after_minutes == 60),
    (K.SETTING_VERY_LATE_AFTER_MIN, "90", lambda s: s.very_late_after_minutes == 90),
    (K.SETTING_ABSENT_AFTER_HOUR, "19", lambda s: s.absent_after_hour == 19),
    (K.SETTING_MIN_HOURS_REQUIRED, "2:00", lambda s: s.min_required_seconds == 7200),
    (K.SETTING_WORKING_DAYS, json.dumps(["SUNDAY"]),
     lambda s: s.working_days == ["SUNDAY"]),
    (K.SETTING_ALLOW_EARLY_CLOCK_IN, "false", lambda s: s.allow_early_clock_in is False),
    (K.SETTING_ALLOW_LATE_CLOCK_OUT, "false", lambda s: s.allow_late_clock_out is False),
    (K.SETTING_WORK_MODE, "remote", lambda s: s.work_mode == "remote"),
]


@pytest.mark.parametrize("key,value,check", CASCADE_KEYS,
                         ids=[k for k, _, _ in CASCADE_KEYS])
def test_team_scope_overrides_org_for_every_policy(db, org_a, key, value, check):
    u = _mk_user(db, org_a, team="Nights")
    _set(db, org_a, key, value, team_key="Nights")
    assert check(resolve_work_schedule(db, u, MONDAY)), f"{key} team override ignored"


@pytest.mark.parametrize("key,value,check", CASCADE_KEYS,
                         ids=[k for k, _, _ in CASCADE_KEYS])
def test_employee_scope_beats_team_for_every_policy(db, org_a, key, value, check):
    u = _mk_user(db, org_a, team="Nights")
    # A conflicting team value, so passing can only mean the employee row won.
    _set(db, org_a, key, "12:00" if ":" in value else "3", team_key="Nights")
    _set(db, org_a, key, value, user_id=u.id)
    assert check(resolve_work_schedule(db, u, MONDAY)), f"{key} employee override ignored"


@pytest.mark.parametrize("key,value,check", CASCADE_KEYS,
                         ids=[k for k, _, _ in CASCADE_KEYS])
def test_a_team_override_does_not_reach_another_team(db, org_a, key, value, check):
    mine = _mk_user(db, org_a, team="Nights")
    theirs = _mk_user(db, org_a, team="Days")
    _set(db, org_a, key, value, team_key="Nights")
    assert check(resolve_work_schedule(db, mine, MONDAY))
    assert not check(resolve_work_schedule(db, theirs, MONDAY)), \
        f"{key} leaked from team Nights to team Days"


def test_system_default_applies_when_nothing_is_configured(db):
    """A brand-new organization with no rows resolves to the documented system
    defaults — never to another tenant's values."""
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Bare{oid}"))
    db.commit()
    s = resolve_work_schedule(db, _mk_user(db, oid), MONDAY)
    assert s.grace_minutes == int(K.SETTING_DEFAULTS[K.SETTING_GRACE_MINUTES])
    assert s.absent_after_hour == int(K.SETTING_DEFAULTS[K.SETTING_ABSENT_AFTER_HOUR])
    assert s.working_days == K.DEFAULT_WORKING_DAYS
    assert s.start_local is None, "a bare org must not gain a scheduled start"
    assert s.end_local is None, "a bare org must not gain a scheduled end either"
    assert s.timezone_name == _tz.FALLBACK_TZ_NAME
