"""Write-path consumers read their policy from the Schedule Resolver.

Every subsystem that MAKES an attendance decision resolves it. There is no
`schedule.engine_enabled` flag and no parallel legacy branch: those branches
were where the divergence lived — four expected-hours values, three definitions
of "late", a global noon cutoff, a global Mon-Fri week.

Subsystems covered here: attendance_alerts, the risk engine, the intelligence
risk rollup, burnout, and the clock-in guard. Each is asserted to follow
CONFIGURATION, and — just as importantly — to hold no threshold of its own.
"""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from database import models
from database.database import Base

from app.schedule_policy import keys as K

_org_seq = itertools.count(60000)
MONDAY = date(2026, 8, 3)
SATURDAY = date(2026, 8, 8)
_IST = timezone(timedelta(hours=5, minutes=30))
_ALL_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _new_org(db) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Org{oid}"))
    db.commit()
    return oid


def _mk_user(db, org_id, **kw):
    u = models.User(name="WP", email=f"wp_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value, *, user_id=None):
    db.add(models.AppSettings(organization_id=org_id, user_id=user_id,
                              key=key, value=value))
    db.commit()


def _session(db, user, clock_in, clock_out=None, break_secs=0):
    s = models.ClockSession(
        user_id=user.id, organization_id=user.organization_id,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=int(((clock_out or clock_in) - clock_in).total_seconds()),
        break_seconds=break_secs, offline_seconds=0,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ══════════════════════════════════════════════════════════════════════════════
# The shared gate
# ══════════════════════════════════════════════════════════════════════════════

def test_there_is_no_rollout_gate(db):
    """The deprecated shim answers True unconditionally, so a stale caller gets
    canonical behaviour rather than silently taking a legacy path that no longer
    exists."""
    import warnings
    from app.attendance_policy import engine_enabled_for_org
    org = _new_org(db)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert engine_enabled_for_org(db, org) is True
        assert engine_enabled_for_org(db, 999_999) is True
        assert engine_enabled_for_org(db, None) is True


def test_no_consumer_still_checks_a_gate():
    """An `if engine_enabled_for_org(...)` anywhere is a second policy path
    waiting to diverge."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        f.relative_to(root).as_posix()
        for f in root.rglob("*.py")
        if f.relative_to(root).as_posix() != "attendance_policy/resolvers.py"
        and "engine_enabled_for_org(" in f.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"consumers still gate on the removed flag: {offenders}"




# ══════════════════════════════════════════════════════════════════════════════
# Risk engine — threshold + the part-time carve-out
# ══════════════════════════════════════════════════════════════════════════════

def test_risk_engine_has_no_part_time_carve_out(db):
    """"Skip part-timers" was one customer's convention promoted to a product
    rule. A part-timer working 2h of a 4h day is genuinely short."""
    from app.risk.router import _run_risk_check
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    now = datetime.now(timezone.utc)
    s_ = _session(db, u, now - timedelta(hours=2), now)
    assert _run_risk_check(u, s_, db).get("reason") != "part_time"


def test_risk_engine_uses_the_expected_hours_cascade(db):
    from app.risk.router import _run_risk_check
    org = _new_org(db)
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "8:45")
    u = _mk_user(db, org, employment_type="full_time")
    now = datetime.now(timezone.utc)
    s_ = _session(db, u, now - timedelta(hours=9), now)     # 9h worked > 8:45
    out = _run_risk_check(u, s_, db)
    assert out["triggered"] is False
    assert out["threshold_seconds"] == 8 * 3600 + 45 * 60


def test_risk_engine_skips_a_non_working_day(db):
    """A clock-out on a day the employee does not work must not fire a
    low-hours alert."""
    from app.risk.router import _run_risk_check
    org = _new_org(db)
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps([]))
    u = _mk_user(db, org, employment_type="full_time")
    now = datetime.now(timezone.utc)
    s_ = _session(db, u, now - timedelta(hours=1), now)
    assert _run_risk_check(u, s_, db).get("reason") == "not_a_working_day"




def test_risk_engine_measures_part_timers_against_their_own_hours(db):
    """Engine ON replaces the blanket part-time skip with "does this person owe
    anything today". A part-timer working 2h of a 4h day is now genuinely short."""
    from app.risk.router import _run_risk_check
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    now = datetime.now(timezone.utc)
    s = _session(db, u, now - timedelta(hours=2), now)
    out = _run_risk_check(u, s, db)
    assert out.get("reason") != "part_time"
    assert out["threshold_seconds" if "threshold_seconds" in out else "worked_seconds"] is not None


def test_risk_engine_uses_the_employee_override(db):
    from app.risk.router import _run_risk_check
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "4:00", user_id=u.id)
    now = datetime.now(timezone.utc)
    s = _session(db, u, now - timedelta(hours=5), now)     # 5h > their own 4h
    out = _run_risk_check(u, s, db)
    assert out["triggered"] is False
    assert out["threshold_seconds"] == 4 * 3600




# ══════════════════════════════════════════════════════════════════════════════
# attendance_alerts — weekend / grace / absent cutoff
# ══════════════════════════════════════════════════════════════════════════════

def test_alerts_monitor_has_no_global_org_scan():
    """The monitor used to build a set of "engine-enabled orgs" and apply one
    global weekend/noon/grace decision to everyone else in the same sweep."""
    import inspect
    from app import attendance_alerts
    src = inspect.getsource(attendance_alerts)
    assert "_engine_enabled_org_ids" not in src
    assert "_scan_org" in src, "the sweep must be organized per organization"




def test_alerts_saturday_is_workable_for_a_mon_sat_org(db):
    """The legacy monitor aborts the whole tick on Saturday. An engine org that
    works Saturdays must be evaluated, which is why the global short-circuit is
    now conditional on no org having the engine on."""
    from app.schedule_policy.classify import ArrivalStatus, classify_arrival
    from app.schedule_policy.resolver import resolve_work_schedule
    org = _new_org(db)
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps(_ALL_DAYS))
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, SATURDAY)
    assert sched.is_working_day is True
    # No clock-in, 15:00 local, cutoff 12 -> Absent, so the monitor would alert.
    verdict = classify_arrival(None, sched, datetime(2026, 8, 8, 15, 0, tzinfo=_IST))
    assert verdict.status == ArrivalStatus.ABSENT


def test_alerts_late_pending_uses_the_org_grace(db):
    """Grace comes from the resolver, not DEFAULT_GRACE_MIN."""
    from app.schedule_policy.classify import ArrivalStatus, classify_arrival
    from app.schedule_policy.resolver import resolve_work_schedule
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    _set(db, org, K.SETTING_GRACE_MINUTES, "45")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, MONDAY)
    assert sched.grace_minutes == 45
    # 09:30 is past the legacy 20m grace but inside the configured 45m.
    at_0930 = datetime(2026, 8, 3, 9, 30, tzinfo=_IST)
    assert classify_arrival(None, sched, at_0930).status == ArrivalStatus.AWAITING
    at_1000 = datetime(2026, 8, 3, 10, 0, tzinfo=_IST)
    assert classify_arrival(None, sched, at_1000).status == ArrivalStatus.LATE_PENDING


def test_alerts_absent_cutoff_is_configurable(db):
    from app.schedule_policy.classify import ArrivalStatus, classify_arrival
    from app.schedule_policy.resolver import resolve_work_schedule
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "16")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, MONDAY)
    at_13 = datetime(2026, 8, 3, 13, 0, tzinfo=_IST)
    assert classify_arrival(None, sched, at_13).status == ArrivalStatus.LATE_PENDING
    at_16 = datetime(2026, 8, 3, 16, 0, tzinfo=_IST)
    assert classify_arrival(None, sched, at_16).status == ArrivalStatus.ABSENT


def test_alerts_legacy_constants_are_gone(db):
    """The inverse of the assertion this replaces. P10 deliberately kept the
    legacy constants alive as the engine-OFF path; with that path removed they
    are dead policy that could only ever drift from the settings."""
    from app import attendance_alerts
    for name in ("DEFAULT_GRACE_MIN", "SHIFT_NOON_HOUR", "_is_weekend_ist",
                 "_parse_shift_start", "IST"):
        assert not hasattr(attendance_alerts, name), f"{name} survived"




# ══════════════════════════════════════════════════════════════════════════════
# intelligence risk rollup
# ══════════════════════════════════════════════════════════════════════════════

def test_intelligence_expected_hours_resolves_for_a_part_timer(db):
    """`EXPECTED_HOURS_PER_DAY = 8.0` was a fifth copy of "how long is a work
    day", and it measured part-timers against a full-timer's."""
    from app.intelligence.risk_engine import _expected_hours_per_day
    org = _new_org(db)
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "4:00")
    u = _mk_user(db, org, employment_type="part_time")
    assert _expected_hours_per_day(db, u) == 4.0




def test_intelligence_expected_hours_resolves_per_employee(db):
    from app.intelligence.risk_engine import _expected_hours_per_day
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "6:00", user_id=u.id)
    assert _expected_hours_per_day(db, u) == 6.0


def test_intelligence_expected_hours_never_raises(db):
    from app.intelligence.risk_engine import (
        _expected_hours_per_day,
        _FALLBACK_EXPECTED_HOURS as EXPECTED_HOURS_PER_DAY,
    )

    class Broken:
        organization_id = None
        employment_type = None
        id = -1
    assert _expected_hours_per_day(db, Broken()) == EXPECTED_HOURS_PER_DAY


# ══════════════════════════════════════════════════════════════════════════════
# Burnout — non-working days and the night window
# ══════════════════════════════════════════════════════════════════════════════

def test_burnout_default_week_treats_saturday_as_non_working(db):
    """Engine OFF: Sat/Sun hours count toward the weekend-work signal."""
    from app.intelligence.burnout import compute_burnout
    org = _new_org(db)
    u = _mk_user(db, org)
    sat = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)
    _session(db, u, sat, sat + timedelta(hours=6))
    r = compute_burnout(db, u.id, period_days=30,
                        now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
    assert r.metrics.get("weekend_hours", 0) > 0


def test_burnout_respects_a_mon_sat_org(db):
    """Engine ON: Saturday is a working day for this org, so those hours are not
    a weekend-work burnout signal."""
    from app.intelligence.burnout import compute_burnout
    org = _new_org(db)
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps(_ALL_DAYS[:6]))   # Mon-Sat
    u = _mk_user(db, org)
    sat = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)
    _session(db, u, sat, sat + timedelta(hours=6))
    r = compute_burnout(db, u.id, period_days=30,
                        now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
    assert r.metrics.get("weekend_hours", 0) == 0


def test_burnout_night_shift_is_not_penalised(db):
    """A 22:00-06:00 worker used to be scored as burnt out for working their own
    shift, because every hour fell in the fixed late-night window."""
    from app.intelligence.burnout import compute_burnout
    org = _new_org(db)
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps(_ALL_DAYS))
    _set(db, org, K.SETTING_SHIFT_START_TIME, "22:00")
    _set(db, org, K.SETTING_SHIFT_END_TIME, "06:00")
    u = _mk_user(db, org)
    # The shift window is expressed in the ORGANIZATION's local time, so the
    # session has to be stored as the UTC instant of 22:00 local. This test
    # used to store 22:00 UTC and compare it against a local-time window —
    # which only agreed for a tenant at UTC+00:00.
    night = datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc)   # 22:00 IST
    _session(db, u, night, night + timedelta(hours=7))
    r = compute_burnout(db, u.id, period_days=30,
                        now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    assert r.metrics.get("late_night_minutes", -1) == 0


def test_burnout_night_window_falls_back_without_a_shift(db):
    """With no shift configured there is nothing to measure "outside my own
    shift" against, so the documented 22:00-06:00 wellbeing window applies.
    That window is a burnout-model constant, not workforce policy."""
    from app.intelligence.burnout import LATE_NIGHT_HOUR_LOCAL, EARLY_MORNING_HOUR_LOCAL
    assert (LATE_NIGHT_HOUR_LOCAL, EARLY_MORNING_HOUR_LOCAL) == (22, 6)


def test_burnout_holds_no_expected_hours_or_timezone_constant():
    from app.intelligence import burnout
    assert not hasattr(burnout, "EXPECTED_DAILY_HOURS")
    assert not hasattr(burnout, "IST")




# ══════════════════════════════════════════════════════════════════════════════
# Clock-in guard
# ══════════════════════════════════════════════════════════════════════════════

def test_clock_in_guard_is_inert_by_default(db):
    """Three conditions must all hold before anyone is blocked, and all three
    default to "no guard": engine on, allow_early_clock_in false, shift resolved."""
    from app.schedule_policy.resolver import resolve_work_schedule
    org = _new_org(db)
    u = _mk_user(db, org)
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.allow_early_clock_in is True
    assert s.start_local is None


def test_clock_in_guard_needs_all_three_conditions(db):
    from app.schedule_policy.resolver import resolve_work_schedule
    org = _new_org(db)
    _set(db, org, K.SETTING_ALLOW_EARLY_CLOCK_IN, "false")
    u = _mk_user(db, org)
    # No shift start configured -> nothing to be early against.
    assert resolve_work_schedule(db, u, MONDAY).start_local is None
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    s = resolve_work_schedule(db, u, MONDAY)
    assert s.allow_early_clock_in is False
    assert s.start_local == time(9, 0)


def test_clock_in_guard_returns_200_not_4xx(db):
    """Matches the Early Clock Out gate: existing clients treat a non-200 as a
    transport error, so the refusal has to be a 200 body."""
    import inspect
    from app.employees import router as r
    src = inspect.getsource(r.clock_in)
    assert "EARLY_CLOCK_IN_NOT_ALLOWED" in src
    assert "HTTPException" not in src.split("EARLY_CLOCK_IN_NOT_ALLOWED")[0][-400:]
    assert "minutes_until_start" in src


def test_clock_in_guard_fails_open(db):
    """A schedule problem must never stop someone starting work."""
    from app.employees.router import clock_in
    org = _new_org(db)
    u = _mk_user(db, org)
    res = clock_in(db=db, user=u, request=None)
    assert res.get("code") != "EARLY_CLOCK_IN_NOT_ALLOWED"


# ══════════════════════════════════════════════════════════════════
# No module may hold its own copy of a policy
# ══════════════════════════════════════════════════════════════════

def test_legacy_constants_are_all_gone(db):
    """The inverse of the P10 guard this replaces, which asserted that every
    legacy constant SURVIVED as the engine-OFF path. They were the divergence."""
    import app.risk.router as risk
    import app.reports.router as reports
    import app.employees.router as employees
    import app.intelligence.risk_engine as intel
    import app.intelligence.burnout as burnout
    from app import attendance_alerts as alerts

    assert not hasattr(risk, "DEFAULT_THRESHOLD_SECS")
    assert not hasattr(reports, "LATE_THRESHOLD_HOUR")
    assert not hasattr(reports, "EXPECTED_HOURS_PER_DAY")
    assert not hasattr(reports, "OVERTIME_THRESHOLD_SEC")
    assert not hasattr(employees, "SYSTEM_DEFAULT_CLOCK_IN")
    assert not hasattr(employees, "SYSTEM_DEFAULT_CLOCK_IN_GRACE_MIN")
    assert not hasattr(employees, "_DASHBOARD_TZ")
    assert not hasattr(employees, "EXPECTED_HOURS_PER_DAY")
    assert not hasattr(intel, "EXPECTED_HOURS_PER_DAY")
    assert not hasattr(burnout, "EXPECTED_DAILY_HOURS")
    assert not hasattr(alerts, "SHIFT_NOON_HOUR")


def test_no_module_hardcodes_the_workforce_timezone():
    """A fixed +05:30 in a business path means one tenant's clock decides
    another tenant's day."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    allowed = {"_tz.py"}                 # the one place that defines the fallback
    offenders = []
    for f in root.rglob("*.py"):
        rel = f.relative_to(root).as_posix()
        if rel in allowed:
            continue
        code = "\n".join(
            l for l in f.read_text(encoding="utf-8").splitlines()
            if not l.lstrip().startswith("#")
        )
        if "minutes=30" in code and "hours=5" in code:
            offenders.append(rel)
    assert offenders == [], f"hardcoded IST remains in: {offenders}"
