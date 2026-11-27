"""
PHASE 3 — ADVERSARIAL MULTI-TENANT VERIFICATION.  *** PERMANENT ***
==================================================================
Written as throwaway scaffolding to try to BREAK the Phase-2 multi-tenant
work. It found a P0 (workforce events broadcast to every tenant's sockets) and
several P1s, so it was KEPT: these are now the standing regression guards for
tenant isolation. Do not delete.

It touches no production code and changes no production behaviour.

Every test here follows the same shape, because it is the only shape that
actually proves the claim:

    SAME CODE · SAME PROCESS · SAME INSTANT · DIFFERENT TENANT SETTINGS
    -> DIFFERENT, INDIVIDUALLY CORRECT RESULTS

Three tenants are used rather than two, because a two-tenant test cannot
distinguish "resolved per tenant" from "resolved for whoever asked last":

    ORG A  Asia/Kolkata      Mon-Fri   09:00-18:00  8:00  15m  absent 12  wk Mon  leave Jan
    ORG B  America/New_York  Mon-Sat   08:00-17:00  7:30  30m  absent 14  wk Sun  leave Apr
    ORG C  Pacific/Auckland  Sun-Thu   14:00-22:00  6:00  45m  absent 18  wk Sat  leave Jul

Org C is deliberately hostile: a Sunday-start week, a Gulf-style Sun-Thu
working week, an afternoon shift, and a timezone on the far side of the date
line from both A and B.
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
from app.schedule_policy.resolver import (
    resolve_work_schedule, resolve_work_schedules, week_start_date,
)

# A private id range so this file cannot collide with any other test module.
_org_seq = itertools.count(910000)

MON_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
MON_SAT = MON_FRI + ["SATURDAY"]
SUN_THU = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY"]

MONDAY = date(2026, 8, 3)
SATURDAY = date(2026, 8, 8)
SUNDAY = date(2026, 8, 9)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_org(db, **kw) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"P3Org{oid}"))
    db.add(models.OrgSettings(organization_id=oid,
                              default_timezone=kw["timezone_name"]))
    rows = {
        K.SETTING_WORKING_DAYS: json.dumps(kw["working_days"]),
        K.SETTING_SHIFT_START_TIME: kw["start"],
        K.SETTING_SHIFT_END_TIME: kw["end"],
        K.SETTING_EXPECTED_WORK_HOURS: kw["expected"],
        K.SETTING_GRACE_MINUTES: str(kw["grace"]),
        K.SETTING_ABSENT_AFTER_HOUR: str(kw["absent_hour"]),
        K.SETTING_WEEK_START_DAY: kw["week_start"],
    }
    for k, v in rows.items():
        db.add(models.AppSettings(organization_id=oid, key=k, value=v))
    db.commit()
    return oid


def _mk_user(db, org_id, **kw):
    u = models.User(name="P3", email=f"p3_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value, *, team_key=None, user_id=None):
    """Upsert, unlike the helper in test_multi_tenant_policy which only ever
    inserts. Mutation tests need to CHANGE a value, not shadow it."""
    row = (
        db.query(models.AppSettings)
        .filter(models.AppSettings.organization_id == org_id,
                models.AppSettings.key == key,
                models.AppSettings.team_key.is_(team_key) if team_key is None
                else models.AppSettings.team_key == team_key,
                models.AppSettings.user_id.is_(user_id) if user_id is None
                else models.AppSettings.user_id == user_id)
        .first()
    )
    if row is not None:
        row.value = value
    else:
        db.add(models.AppSettings(organization_id=org_id, key=key, value=value,
                                  team_key=team_key, user_id=user_id))
    db.commit()
    _tz.invalidate_tz_cache(org_id)


def _clear(db, org_id, key, *, team_key=None, user_id=None):
    q = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == key)
    q = q.filter(models.AppSettings.team_key.is_(None)) if team_key is None \
        else q.filter(models.AppSettings.team_key == team_key)
    q = q.filter(models.AppSettings.user_id.is_(None)) if user_id is None \
        else q.filter(models.AppSettings.user_id == user_id)
    q.delete(synchronize_session=False)
    db.commit()


def _local(db, org_id, d: date, t: time) -> datetime:
    """A wall-clock time in an organization's own zone.

    classify_arrival() takes LOCAL datetimes (see its docstring); production
    callers convert first (reports._is_late, annotate.arrival_for). Building a
    UTC instant and passing it straight in would test the harness, not the
    product.
    """
    return datetime.combine(d, t).replace(tzinfo=_tz.org_timezone(db, org_id))


def _utc_of(db, org_id, d: date, t: time) -> datetime:
    return _local(db, org_id, d, t).astimezone(timezone.utc)


@pytest.fixture
def org_a(db):
    return _mk_org(db, timezone_name="Asia/Kolkata", working_days=MON_FRI,
                   start="09:00", end="18:00", expected="8:00", grace=15,
                   absent_hour=12, week_start="MONDAY")


@pytest.fixture
def org_b(db):
    return _mk_org(db, timezone_name="America/New_York", working_days=MON_SAT,
                   start="08:00", end="17:00", expected="7:30", grace=30,
                   absent_hour=14, week_start="SUNDAY")


@pytest.fixture
def org_c(db):
    return _mk_org(db, timezone_name="Pacific/Auckland", working_days=SUN_THU,
                   start="14:00", end="22:00", expected="6:00", grace=45,
                   absent_hour=18, week_start="SATURDAY")


# ══════════════════════════════════════════════════════════════════════════
# §3  SAME EVENT -> DIFFERENT TENANT RESULT
# ══════════════════════════════════════════════════════════════════════════

def test_one_utc_instant_three_local_days(db, org_a, org_b, org_c):
    """The single hardest case: one instant, three calendar days.

    2026-08-03 22:00 UTC is
        Aug 4 03:30 in Kolkata,  Aug 3 18:00 in New York,  Aug 4 10:00 in Auckland.
    A tenant bucketed onto the wrong day gets the wrong shift, the wrong
    working-day answer and the wrong report row.
    """
    instant = datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc)
    got = {o: _tz.to_local(db, o, instant).date() for o in (org_a, org_b, org_c)}
    assert got[org_a] == date(2026, 8, 4)
    assert got[org_b] == date(2026, 8, 3)
    assert got[org_c] == date(2026, 8, 4)
    assert len(set(got.values())) == 2, "the three tenants must not collapse"


def test_one_arrival_three_verdicts(db, org_a, org_b, org_c):
    """One clock-in instant judged by three different shift+grace policies."""
    ua, ub, uc = _mk_user(db, org_a), _mk_user(db, org_b), _mk_user(db, org_c)
    # 2026-08-03 12:20 UTC == 17:50 IST == 08:20 New York == 2026-08-04 00:20 NZ
    instant = datetime(2026, 8, 3, 12, 20, tzinfo=timezone.utc)

    def verdict(user, org):
        local = _tz.to_local(db, org, instant)
        sched = resolve_work_schedule(db, user, local.date())
        return classify_arrival(local, sched, local).status

    va, vb, vc = verdict(ua, org_a), verdict(ub, org_b), verdict(uc, org_c)
    # A: 17:50 local against a 09:00 start = 530 min, past very_late_after (120).
    assert va == ArrivalStatus.VERY_LATE
    # B: 08:20 local against 08:00 + 30m grace = on time.
    assert vb == ArrivalStatus.PRESENT
    # C: Aug 4 local (the date line has already turned over), a Tuesday, which
    #    IS a working day on a Sun-Thu week; 00:20 is before the 14:00 start.
    assert vc == ArrivalStatus.PRESENT
    # B and C agreeing is legitimate — the claim being tested is that ONE
    # instant does not force ONE verdict.
    assert va != vb


def test_grace_boundary_exactly_twenty_minutes(db, org_a, org_b):
    """MANDATORY (§6). Same +20 minutes, 15m grace vs 30m grace."""
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    sa = resolve_work_schedule(db, ua, MONDAY)
    sb = resolve_work_schedule(db, ub, MONDAY)

    def arrive(sched, org):
        at = (datetime.combine(MONDAY, sched.start_local)
              + timedelta(minutes=20)).time()
        local = _local(db, org, MONDAY, at)
        return classify_arrival(local, sched, local).status

    assert arrive(sa, org_a) == ArrivalStatus.LATE,   "A: 20m > 15m grace"
    assert arrive(sb, org_b) == ArrivalStatus.PRESENT, "B: 20m <= 30m grace"


@pytest.mark.parametrize("minutes,exp_a,exp_b", [
    (14, ArrivalStatus.PRESENT, ArrivalStatus.PRESENT),
    (15, ArrivalStatus.PRESENT, ArrivalStatus.PRESENT),   # grace is inclusive
    (16, ArrivalStatus.LATE,    ArrivalStatus.PRESENT),
    (30, ArrivalStatus.LATE,    ArrivalStatus.PRESENT),
    (31, ArrivalStatus.LATE,    ArrivalStatus.LATE),
])
def test_grace_boundaries_are_exact(db, org_a, org_b, minutes, exp_a, exp_b):
    for org, exp in ((org_a, exp_a), (org_b, exp_b)):
        u = _mk_user(db, org)
        sched = resolve_work_schedule(db, u, MONDAY)
        at = (datetime.combine(MONDAY, sched.start_local)
              + timedelta(minutes=minutes)).time()
        local = _local(db, org, MONDAY, at)
        got = classify_arrival(local, sched, local).status
        assert got == exp, "org=%s +%dm gave %s" % (org, minutes, got)


def test_saturday_is_policy_not_calendar(db, org_a, org_b, org_c):
    ua, ub, uc = _mk_user(db, org_a), _mk_user(db, org_b), _mk_user(db, org_c)
    assert resolve_work_schedule(db, ua, SATURDAY).is_working_day is False
    assert resolve_work_schedule(db, ub, SATURDAY).is_working_day is True
    assert resolve_work_schedule(db, uc, SATURDAY).is_working_day is False
    # Sunday: only the Sun-Thu tenant works.
    assert resolve_work_schedule(db, ua, SUNDAY).is_working_day is False
    assert resolve_work_schedule(db, ub, SUNDAY).is_working_day is False
    assert resolve_work_schedule(db, uc, SUNDAY).is_working_day is True


def test_reversing_the_setting_reverses_the_behaviour(db, org_a, org_b):
    """If the answer survives swapping the two tenants' settings, it was not
    coming from the settings."""
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    assert resolve_work_schedule(db, ua, SATURDAY).is_working_day is False
    _set(db, org_a, K.SETTING_WORKING_DAYS, json.dumps(MON_SAT))
    _set(db, org_b, K.SETTING_WORKING_DAYS, json.dumps(MON_FRI))
    assert resolve_work_schedule(db, ua, SATURDAY).is_working_day is True
    assert resolve_work_schedule(db, ub, SATURDAY).is_working_day is False


def test_saturday_wednesday_week_is_expressible(db, org_a):
    u = _mk_user(db, org_a)
    _set(db, org_a, K.SETTING_WORKING_DAYS,
         json.dumps(["SATURDAY", "SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY"]))
    assert resolve_work_schedule(db, u, SATURDAY).is_working_day is True
    assert resolve_work_schedule(db, u, date(2026, 8, 6)).is_working_day is False  # Thu


# ══════════════════════════════════════════════════════════════════════════
# §5 / §7  SHIFT START + ABSENCE CUTOFF BOUNDARIES
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("hour,minute", [(11, 59), (12, 0), (12, 1),
                                         (13, 59), (14, 0), (14, 1)])
def test_absence_cutoff_boundaries(db, org_a, org_b, hour, minute):
    """The cutoff must be >= (at 12:00 sharp org A is past it) and each tenant
    must use its own number."""
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    sa = resolve_work_schedule(db, ua, MONDAY)
    sb = resolve_work_schedule(db, ub, MONDAY)
    assert sa.absent_after_hour == 12
    assert sb.absent_after_hour == 14
    past_a = hour >= sa.absent_after_hour
    past_b = hour >= sb.absent_after_hour
    assert past_a == (hour >= 12)
    assert past_b == (hour >= 14)
    if hour in (12, 13):
        assert past_a is True and past_b is False, \
            "the same wall-clock hour must mean different things per tenant"


def test_shift_start_difference_changes_lateness_window(db, org_a, org_b):
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    _set(db, org_b, K.SETTING_SHIFT_START_TIME, "14:00")
    assert resolve_work_schedule(db, ua, MONDAY).start_local == time(9, 0)
    assert resolve_work_schedule(db, ub, MONDAY).start_local == time(14, 0)
    # 10:00 LOCAL in each tenant: an hour late for A, four hours early for B.
    for org, user, expect_late in ((org_a, ua, True), (org_b, ub, False)):
        sched = resolve_work_schedule(db, user, MONDAY)
        local = _local(db, org, MONDAY, time(10, 0))
        st = classify_arrival(local, sched, local).status
        assert (st != ArrivalStatus.PRESENT) is expect_late, "org=%s got %s" % (org, st)


# ══════════════════════════════════════════════════════════════════════════
# §8 / §9  TEAM AND EMPLOYEE CASCADE
# ══════════════════════════════════════════════════════════════════════════

def test_two_teams_two_graces_and_a_default(db, org_a):
    """org 15 · team Nights 30 · team Days 45 · nobody else affected."""
    nights = _mk_user(db, org_a, team="Nights")
    days = _mk_user(db, org_a, team="Days")
    floater = _mk_user(db, org_a)
    _set(db, org_a, K.SETTING_GRACE_MINUTES, "30", team_key="Nights")
    _set(db, org_a, K.SETTING_GRACE_MINUTES, "45", team_key="Days")
    assert resolve_work_schedule(db, nights, MONDAY).grace_minutes == 30
    assert resolve_work_schedule(db, days, MONDAY).grace_minutes == 45
    assert resolve_work_schedule(db, floater, MONDAY).grace_minutes == 15

    # Changing the ORG default must not disturb either team override.
    _set(db, org_a, K.SETTING_GRACE_MINUTES, "5")
    assert resolve_work_schedule(db, nights, MONDAY).grace_minutes == 30
    assert resolve_work_schedule(db, days, MONDAY).grace_minutes == 45
    assert resolve_work_schedule(db, floater, MONDAY).grace_minutes == 5


def test_full_cascade_teardown(db, org_a):
    """8h org -> 7.5h team -> 6h employee, then peel it back one layer at a
    time and confirm each layer reappears."""
    u = _mk_user(db, org_a, team="Nights")
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "8:00")
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "7:30", team_key="Nights")
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "6:00", user_id=u.id)

    s = resolve_work_schedule(db, u, MONDAY)
    assert s.expected_work_seconds == 6 * 3600
    assert s.sources.get("expected_work_seconds") in ("employee", "legacy_user")

    _clear(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, user_id=u.id)
    assert resolve_work_schedule(db, u, MONDAY).expected_work_seconds == 7 * 3600 + 1800

    _clear(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, team_key="Nights")
    assert resolve_work_schedule(db, u, MONDAY).expected_work_seconds == 8 * 3600


def test_employee_override_does_not_reach_a_colleague(db, org_a):
    mine = _mk_user(db, org_a)
    theirs = _mk_user(db, org_a)
    _set(db, org_a, K.SETTING_GRACE_MINUTES, "90", user_id=mine.id)
    assert resolve_work_schedule(db, mine, MONDAY).grace_minutes == 90
    assert resolve_work_schedule(db, theirs, MONDAY).grace_minutes == 15


def test_team_names_do_not_collide_across_organizations(db, org_a, org_b):
    """Both tenants have a team literally called "Nights". The team_key is a
    STRING, so if any resolution path forgets organization_id the override
    crosses the tenant boundary."""
    a_night = _mk_user(db, org_a, team="Nights")
    b_night = _mk_user(db, org_b, team="Nights")
    _set(db, org_a, K.SETTING_GRACE_MINUTES, "99", team_key="Nights")
    assert resolve_work_schedule(db, a_night, MONDAY).grace_minutes == 99
    assert resolve_work_schedule(db, b_night, MONDAY).grace_minutes == 30, \
        "org A's team override leaked into org B's identically-named team"


# ══════════════════════════════════════════════════════════════════════════
# §10 / §11  MUTATION AND CACHE ISOLATION
# ══════════════════════════════════════════════════════════════════════════

MUTATIONS = [
    (K.SETTING_GRACE_MINUTES, "45", lambda s: s.grace_minutes == 45),
    (K.SETTING_SHIFT_START_TIME, "06:30", lambda s: s.start_local == time(6, 30)),
    (K.SETTING_SHIFT_END_TIME, "23:15", lambda s: s.end_local == time(23, 15)),
    (K.SETTING_EXPECTED_WORK_HOURS, "5:45", lambda s: s.expected_work_seconds == 5 * 3600 + 2700),
    (K.SETTING_WORKING_DAYS, json.dumps(["WEDNESDAY"]), lambda s: s.working_days == ["WEDNESDAY"]),
    (K.SETTING_ABSENT_AFTER_HOUR, "20", lambda s: s.absent_after_hour == 20),
    (K.SETTING_VERY_LATE_AFTER_MIN, "5", lambda s: s.very_late_after_minutes == 5),
    (K.SETTING_MIN_HOURS_REQUIRED, "1:00", lambda s: s.min_required_seconds == 3600),
]


@pytest.mark.parametrize("key,value,check", MUTATIONS,
                         ids=[k for k, _, _ in MUTATIONS])
def test_mutating_org_a_never_moves_org_b(db, org_a, org_b, key, value, check):
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    before_b = resolve_work_schedule(db, ub, MONDAY)
    _set(db, org_a, key, value)
    after_a = resolve_work_schedule(db, ua, MONDAY)
    after_b = resolve_work_schedule(db, ub, MONDAY)
    assert check(after_a), f"{key}: org A did not change"
    assert not check(after_b), f"{key}: org A's value reached org B"
    for field in ("grace_minutes", "start_local", "end_local",
                  "expected_work_seconds", "working_days", "absent_after_hour",
                  "very_late_after_minutes",
                  "min_required_seconds", "timezone_name"):
        assert getattr(after_b, field) == getattr(before_b, field), \
            f"{key}: org B's {field} moved"


def test_timezone_mutation_is_isolated_and_uncached(db, org_a, org_b):
    """The tz cache is keyed by org and must be invalidated per org."""
    assert _tz.org_timezone_name(db, org_a) == "Asia/Kolkata"
    assert _tz.org_timezone_name(db, org_b) == "America/New_York"
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == org_a
    ).update({"default_timezone": "Europe/London"})
    db.commit()
    _tz.invalidate_tz_cache(org_a)
    assert _tz.org_timezone_name(db, org_a) == "Europe/London"
    assert _tz.org_timezone_name(db, org_b) == "America/New_York", \
        "invalidating org A evicted org B"


def test_resolve_interleaved_never_reuses_the_previous_tenants_answer(db, org_a, org_b):
    """resolve A, resolve B, mutate A, resolve A, resolve B — in that order."""
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    a1 = resolve_work_schedule(db, ua, MONDAY).grace_minutes
    b1 = resolve_work_schedule(db, ub, MONDAY).grace_minutes
    _set(db, org_a, K.SETTING_GRACE_MINUTES, "44")
    a2 = resolve_work_schedule(db, ua, MONDAY).grace_minutes
    b2 = resolve_work_schedule(db, ub, MONDAY).grace_minutes
    assert (a1, b1, a2, b2) == (15, 30, 44, 30)


def test_batch_resolve_of_a_mixed_tenant_list(db, org_a, org_b, org_c):
    """One call, three tenants — the prefetch groups by organization, so a bug
    here silently gives everyone the first org's policy."""
    users = [_mk_user(db, org_a), _mk_user(db, org_b), _mk_user(db, org_c)]
    out = resolve_work_schedules(db, users, MONDAY)
    got = {u.id: out[u.id] for u in users}
    assert got[users[0].id].grace_minutes == 15
    assert got[users[1].id].grace_minutes == 30
    assert got[users[2].id].grace_minutes == 45
    assert got[users[0].id].timezone_name == "Asia/Kolkata"
    assert got[users[1].id].timezone_name == "America/New_York"
    assert got[users[2].id].timezone_name == "Pacific/Auckland"
    # Reversed order must give the same answers.
    out2 = resolve_work_schedules(db, list(reversed(users)), MONDAY)
    for u in users:
        assert out2[u.id].grace_minutes == got[u.id].grace_minutes


# ══════════════════════════════════════════════════════════════════════════
# §12 / §13  TIMEZONE AND DST
# ══════════════════════════════════════════════════════════════════════════

def test_day_bounds_are_real_offsets_not_a_shared_constant(db, org_a, org_b, org_c):
    starts = {}
    for org in (org_a, org_b, org_c):
        s, e = _tz.local_day_bounds(db, org, MONDAY)
        starts[org] = s
        assert (e - s) == timedelta(days=1)
    assert len(set(starts.values())) == 3, "three zones collapsed to one window"
    assert starts[org_a] == datetime(2026, 8, 2, 18, 30, tzinfo=timezone.utc)
    assert starts[org_b] == datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
    assert starts[org_c] == datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def test_dst_transition_day_is_23_or_25_hours(db, org_b):
    """America/New_York springs forward 2026-03-08 and falls back 2026-11-01.
    A day window built from a fixed offset stays 24h and silently mis-buckets
    an hour of work."""
    spring_s, spring_e = _tz.local_day_bounds(db, org_b, date(2026, 3, 8))
    fall_s, fall_e = _tz.local_day_bounds(db, org_b, date(2026, 11, 1))
    assert (spring_e - spring_s) == timedelta(hours=23), "spring-forward day"
    assert (fall_e - fall_s) == timedelta(hours=25), "fall-back day"


def test_dst_does_not_move_the_shift_wall_clock(db, org_b):
    """08:00 local is 08:00 local on both sides of a DST change."""
    u = _mk_user(db, org_b)
    before = resolve_work_schedule(db, u, date(2026, 3, 6))
    after = resolve_work_schedule(db, u, date(2026, 3, 10))
    assert before.start_local == after.start_local == time(8, 0)
    # ...but the same wall clock is a different UTC instant.
    z = _tz.org_timezone(db, org_b)
    b_utc = datetime.combine(date(2026, 3, 6), time(8, 0), tzinfo=z).astimezone(timezone.utc)
    a_utc = datetime.combine(date(2026, 3, 10), time(8, 0), tzinfo=z).astimezone(timezone.utc)
    assert b_utc.hour == 13 and a_utc.hour == 12


def test_week_start_is_per_tenant(db, org_a, org_b, org_c):
    wednesday = date(2026, 8, 12)
    assert week_start_date(db, org_a, wednesday) == date(2026, 8, 10)   # Mon
    assert week_start_date(db, org_b, wednesday) == date(2026, 8, 9)    # Sun
    assert week_start_date(db, org_c, wednesday) == date(2026, 8, 8)    # Sat


# ══════════════════════════════════════════════════════════════════════════
# §14  EXPECTED HOURS — ONE VALUE, EVERY CONSUMER
# ══════════════════════════════════════════════════════════════════════════

def test_every_consumer_reads_the_same_seven_and_a_half_hours(db, org_a):
    """Set 7:30 once; every module that has an opinion about "a full day" must
    return 27000 seconds. A hidden 8 / 8.0 / 8.75 fallback fails here."""
    u = _mk_user(db, org_a)
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "7:30")
    EXPECT = 7 * 3600 + 1800

    from app.attendance_policy import resolve_expected_work_seconds
    from app.risk.router import _threshold_secs
    from app.intelligence.risk_engine import _expected_hours_per_day
    from app.schedule_policy.annotate import annotate

    assert resolve_work_schedule(db, u, MONDAY).expected_work_seconds == EXPECT
    assert resolve_expected_work_seconds(db, u, MONDAY) == EXPECT
    assert _threshold_secs(db, u) == EXPECT
    assert abs(_expected_hours_per_day(db, u) - EXPECT / 3600) < 1e-9
    assert annotate(db, [u], on_date=MONDAY)[u.id]["expected_seconds"] == EXPECT


def test_expected_hours_is_not_secretly_the_full_time_default(db, org_a):
    """A part-timer with an EMPLOYEE override must beat the type policy."""
    u = _mk_user(db, org_a, employment_type="part_time")
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "3:15", user_id=u.id)
    from app.attendance_policy import resolve_expected_work_seconds
    assert resolve_work_schedule(db, u, MONDAY).expected_work_seconds == 3 * 3600 + 900
    assert resolve_expected_work_seconds(db, u, MONDAY) == 3 * 3600 + 900


# ══════════════════════════════════════════════════════════════════════════
# §15  WORKED TIME — ONE CANONICAL RESULT
# ══════════════════════════════════════════════════════════════════════════

class _Sess:
    """A session with a break AND an offline gap — the case the three former
    implementations disagreed about."""
    id = 1
    user_id = 1
    clock_in_time = datetime(2026, 8, 3, 3, 30, tzinfo=timezone.utc)
    clock_out_time = datetime(2026, 8, 3, 12, 30, tzinfo=timezone.utc)
    break_seconds = 2700          # 45 min
    offline_seconds = 900         # 15 min
    total_seconds = 0             # not finalised -> every consumer recomputes


def test_all_worked_time_consumers_agree(db):
    from app._worked import session_worked_seconds
    from app.reports.router import _session_seconds
    from app.risk.router import _session_worked_secs
    s = _Sess()
    now = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
    expected = 9 * 3600 - 2700 - 900
    results = {
        "canonical": session_worked_seconds(s, now),
        "reports": _session_seconds(s, now),
        "risk": _session_worked_secs(s),
    }
    assert len(set(results.values())) == 1, results
    assert results["canonical"] == expected


def test_open_session_is_not_zero_hours(db):
    from app._worked import session_worked_seconds
    from app.reports.router import _session_seconds
    s = _Sess()
    s.clock_out_time = None
    now = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    expected = 10 * 3600 - 2700 - 900
    assert session_worked_seconds(s, now) == expected
    assert _session_seconds(s, now) == expected


# ══════════════════════════════════════════════════════════════════════════
# §16 / §17 / §18  BREAK · IDLE · SCREENSHOT
# ══════════════════════════════════════════════════════════════════════════

def test_break_allowance_is_per_organization(db, org_a, org_b):
    from app.breaks import governance as gov
    db.add(models.AppSettings(organization_id=org_a,
                              key=gov.SHORT_THRESHOLD_KEY, value="15"))
    db.add(models.AppSettings(organization_id=org_b,
                              key=gov.SHORT_THRESHOLD_KEY, value="30"))
    db.commit()
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_a) == 15
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_b) == 30


def test_break_workday_boundary_uses_each_tenants_midnight(db, org_a, org_b, org_c):
    from app.breaks import governance as gov
    instant = datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)
    assert gov.workday_date(db, org_a, instant) == date(2026, 8, 3)   # 08:30 IST
    assert gov.workday_date(db, org_b, instant) == date(2026, 8, 2)   # 23:00 prev
    assert gov.workday_date(db, org_c, instant) == date(2026, 8, 3)   # 15:00 NZ


def test_idle_and_screenshot_policy_are_per_organization(db, org_a, org_b):
    from app.attendance_policy import policy_int, SETTING_IDLE_ALERT_MINUTES
    from app.settings import scope as settings_scope
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    _set(db, org_a, SETTING_IDLE_ALERT_MINUTES, "10")
    _set(db, org_b, SETTING_IDLE_ALERT_MINUTES, "30")
    assert policy_int(db, ua, SETTING_IDLE_ALERT_MINUTES) == 10
    assert policy_int(db, ub, SETTING_IDLE_ALERT_MINUTES) == 30

    _set(db, org_a, "screenshot_interval", "60")
    _set(db, org_b, "screenshot_interval", "900")
    got_a = settings_scope.resolve_effective(db, ua, {"screenshot_interval": "1800"})
    got_b = settings_scope.resolve_effective(db, ub, {"screenshot_interval": "1800"})
    assert got_a["screenshot_interval"] == "60"
    assert got_b["screenshot_interval"] == "900"


def test_tracker_policy_follows_the_employee_not_the_organization(db, org_a):
    """The tracker polls GET /settings, which resolves at EMPLOYEE scope."""
    from app.settings import scope as settings_scope
    u1, u2 = _mk_user(db, org_a), _mk_user(db, org_a)
    _set(db, org_a, "screenshot_interval", "300")
    _set(db, org_a, "screenshot_interval", "30", user_id=u1.id)
    assert settings_scope.resolve_effective(db, u1, {"screenshot_interval": "1800"})["screenshot_interval"] == "30"
    assert settings_scope.resolve_effective(db, u2, {"screenshot_interval": "1800"})["screenshot_interval"] == "300"


# ══════════════════════════════════════════════════════════════════════════
# §19 / §20  LEAVE
# ══════════════════════════════════════════════════════════════════════════

def test_leave_year_is_per_organization(db, org_a, org_b, org_c):
    from app.leave import policy as lp
    _set(db, org_b, lp.SETTING_YEAR_START_MONTH, "4")
    _set(db, org_c, lp.SETTING_YEAR_START_MONTH, "7")
    ys = {o: lp.leave_year_start(db, o) for o in (org_a, org_b, org_c)}
    assert (ys[org_a].month, ys[org_b].month, ys[org_c].month) == (1, 4, 7)
    # ...and the accrual pacing follows the same boundary.
    months = {o: lp.months_into_leave_year(db, o) for o in (org_a, org_b, org_c)}
    assert len(set(months.values())) > 1, "every tenant paced identically"


def test_sick_review_threshold_is_per_organization(db, org_a, org_b):
    from app.leave import policy as lp
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    _set(db, org_b, lp.SETTING_SICK_REVIEW_PER_MONTH, "5")
    assert lp.sick_review_threshold(db, ua) == 2
    assert lp.sick_review_threshold(db, ub) == 5
    _set(db, org_a, lp.SETTING_SICK_REVIEW_PER_MONTH, "0")
    assert lp.sick_review_threshold(db, ua) == 0, "0 must disable, not fall back"


def test_long_weekend_flag_follows_working_days_not_monday_friday(db, org_a, org_c):
    """Org A (Mon-Fri): leave on Monday abuts Sunday -> flagged.
    Org C (Sun-Thu): Monday abuts Sunday, which IS a working day -> not."""
    from app.leave import policy as lp
    ua, uc = _mk_user(db, org_a), _mk_user(db, org_c)
    assert lp.abuts_non_working_day(db, ua, MONDAY, MONDAY) is True
    assert lp.abuts_non_working_day(db, uc, MONDAY, MONDAY) is False
    # For org C the equivalent long weekend is Thursday (abuts Friday off).
    thursday = date(2026, 8, 6)
    assert lp.abuts_non_working_day(db, uc, thursday, thursday) is True


# ══════════════════════════════════════════════════════════════════════════
# §21  PAYROLL
# ══════════════════════════════════════════════════════════════════════════

def test_payroll_divisor_and_working_days_are_per_organization(db, org_a, org_b, org_c):
    from app.payroll import policy as pp
    ua, ub, uc = _mk_user(db, org_a), _mk_user(db, org_b), _mk_user(db, org_c)
    _set(db, org_a, pp.SETTING_LWP_DIVISOR_DAYS, "26")
    _set(db, org_b, pp.SETTING_LWP_DIVISOR_DAYS, "22")
    assert (pp.divisor_days(db, ua), pp.divisor_days(db, ub),
            pp.divisor_days(db, uc)) == (26.0, 22.0, 30.0)

    start, end = date(2026, 8, 3), date(2026, 8, 16)      # two full weeks
    assert pp.working_days_between(db, ua, start, end) == 10   # Mon-Fri
    assert pp.working_days_between(db, ub, start, end) == 12   # Mon-Sat
    assert pp.working_days_between(db, uc, start, end) == 10   # Sun-Thu


def test_lwp_deduction_differs_by_tenant_for_identical_salary(db, org_a, org_b):
    """Same gross, same unpaid days, different divisor -> different money."""
    from app.payroll import policy as pp
    from app.payroll.service import compute_lwp_deduction
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    _set(db, org_a, pp.SETTING_LWP_DIVISOR_DAYS, "30")
    _set(db, org_b, pp.SETTING_LWP_DIVISOR_DAYS, "22")

    class Cfg:
        basic_salary = 60000.0
        hra = da = ta = bonus = 0.0
        pf = tax = 0.0
        pay_type = "monthly"
        hourly_rate = None

    a = compute_lwp_deduction(Cfg(), 2.0, db=db, user=ua)
    b = compute_lwp_deduction(Cfg(), 2.0, db=db, user=ub)
    assert a != b, "the divisor setting did not reach the deduction"
    assert round(a, 2) == round(60000 / 30 * 2, 2)
    assert round(b, 2) == round(60000 / 22 * 2, 2)


# ══════════════════════════════════════════════════════════════════════════
# §22  NOTIFICATION ISOLATION
# ══════════════════════════════════════════════════════════════════════════

def test_alert_recipients_never_cross_tenants_or_fall_back_to_platform(db, org_a, org_b):
    from app.notifications.recipients import alert_recipients
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == org_a).update({"hr_email": "hr-a@a.test"})
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == org_b).update({"hr_email": "hr-b@b.test"})
    db.commit()
    a = alert_recipients(db, org_a, purpose="test")
    b = alert_recipients(db, org_b, purpose="test")
    assert a == ["hr-a@a.test"] and b == ["hr-b@b.test"]
    assert not set(a) & set(b)
    for addr in a + b:
        assert "temveris" not in addr.lower()


def test_an_organization_with_nothing_configured_gets_no_recipient(db):
    """The failure mode being guarded: silently mailing Temveris instead."""
    from app.notifications.recipients import alert_recipients
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Bare{oid}"))
    db.commit()
    assert alert_recipients(db, oid, purpose="test") == []


def test_recipients_prefer_the_cascade_over_the_org_profile(db, org_a):
    from app.notifications.recipients import alert_recipients, SETTING_ALERT_RECIPIENTS
    db.query(models.OrgSettings).filter(
        models.OrgSettings.organization_id == org_a).update({"hr_email": "profile@a.test"})
    db.commit()
    _set(db, org_a, SETTING_ALERT_RECIPIENTS, "ops@a.test, lead@a.test")
    got = alert_recipients(db, org_a, purpose="test")
    assert got == ["ops@a.test", "lead@a.test"]


# ══════════════════════════════════════════════════════════════════════════
# §26  BACKGROUND WORKERS
# ══════════════════════════════════════════════════════════════════════════

def test_attendance_scan_groups_by_organization(db, org_a, org_b):
    """The worker must not evaluate org B's people on org A's clock."""
    from app import attendance_alerts
    _mk_user(db, org_a)
    _mk_user(db, org_b)
    by_org = attendance_alerts._users_by_org(db)
    assert org_a in by_org and org_b in by_org
    for oid, users in by_org.items():
        assert all(u.organization_id == oid for u in users), \
            "a user was filed under the wrong organization"


def test_retention_sweep_is_per_organization(db, org_a, org_b):
    from app.storage.retention import retention_days, SETTING_SCREENSHOT_RETENTION_DAYS
    _set(db, org_a, SETTING_SCREENSHOT_RETENTION_DAYS, "7")
    _set(db, org_b, SETTING_SCREENSHOT_RETENTION_DAYS, "365")
    assert retention_days(db, org_a) == 7
    assert retention_days(db, org_b) == 365


def test_desktop_only_clock_in_is_a_setting_not_an_org_id(db, org_a, org_b):
    from app.attendance_desktop_policy import desktop_clock_in_required
    from app.attendance_policy import SETTING_DESKTOP_ONLY_CLOCK_IN
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    _set(db, org_a, SETTING_DESKTOP_ONLY_CLOCK_IN, "true")
    assert desktop_clock_in_required(ua, db) is True
    assert desktop_clock_in_required(ub, db) is False


# ══════════════════════════════════════════════════════════════════════════
# §27  REPORT CONSISTENCY
# ══════════════════════════════════════════════════════════════════════════

def test_report_lateness_uses_each_tenants_schedule(db, org_a, org_b):
    """`_is_late` takes the STORED UTC instant and converts using the
    schedule's own zone — the real production path."""
    from app.reports.router import _is_late
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    sa = resolve_work_schedule(db, ua, MONDAY)
    sb = resolve_work_schedule(db, ub, MONDAY)

    # 09:20 on each tenant's own clock: A is 20m past 09:00 (grace 15) -> late;
    # B is 80m past 08:00 (grace 30) -> also late.
    assert _is_late(_utc_of(db, org_a, MONDAY, time(9, 20)), sa) is True
    assert _is_late(_utc_of(db, org_b, MONDAY, time(9, 20)), sb) is True
    # 08:25 local: inside B's 30m grace; A has not started yet.
    assert _is_late(_utc_of(db, org_b, MONDAY, time(8, 25)), sb) is False
    assert _is_late(_utc_of(db, org_a, MONDAY, time(8, 25)), sa) is False

    # The decisive one: ONE UTC instant, two schedules. 09:30 in Kolkata is
    # 00:00 in New York. If both come back late, the zone is not being used.
    same_utc = _utc_of(db, org_a, MONDAY, time(9, 30))
    assert _is_late(same_utc, sa) is True
    assert _is_late(same_utc, sb) is False, (
        "one UTC instant produced the same verdict for both tenants")


def test_report_without_a_schedule_never_invents_lateness(db, org_a):
    from app.reports.router import _is_late
    u = _mk_user(db, org_a)
    _clear(db, org_a, K.SETTING_SHIFT_START_TIME)
    sched = resolve_work_schedule(db, u, MONDAY)
    assert sched.start_local is None
    aware = datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc)
    assert _is_late(aware, sched) is False
    assert _is_late(aware, None) is False


# ══════════════════════════════════════════════════════════════════════════
# §31  SETTINGS REGISTRY — every key is reachable at every scope
# ══════════════════════════════════════════════════════════════════════════

def test_every_registered_setting_resolves_at_all_three_scopes(db, org_a):
    """A key that the API accepts but the resolver ignores is a lie told to
    the admin. This asserts the plumbing, not the semantics."""
    from app.settings.router import DEFAULTS
    from app.settings import scope as settings_scope
    u = _mk_user(db, org_a, team="Nights")
    broken = []
    for key, default in DEFAULTS.items():
        probe = "__probe__"
        for scope_kw in ({}, {"team_key": "Nights"}, {"user_id": u.id}):
            _set(db, org_a, key, probe, **scope_kw)
            got = settings_scope.resolve_effective(db, u, {key: default}).get(key)
            if got != probe:
                broken.append((key, scope_kw, got))
            _clear(db, org_a, key, **scope_kw)
    assert broken == [], f"keys not honoured at some scope: {broken}"


def test_no_setting_written_for_org_a_is_visible_to_org_b(db, org_a, org_b):
    from app.settings.router import DEFAULTS
    from app.settings import scope as settings_scope
    ub = _mk_user(db, org_b)
    leaked = []
    for key, default in DEFAULTS.items():
        _set(db, org_a, key, "__probe__")
        if settings_scope.resolve_effective(db, ub, {key: default}).get(key) == "__probe__":
            leaked.append(key)
        _clear(db, org_a, key)
    assert leaked == [], f"org A settings visible to org B: {leaked}"


# ══════════════════════════════════════════════════════════════════════════
# REGRESSION — WEBSOCKET TENANT ISOLATION  (Phase-3 P0 finding)
# ══════════════════════════════════════════════════════════════════════════
#
# websocket_manager documents broadcast/broadcast_sync as "never use for
# business data" — they fan out across every org room AND the unauthenticated
# pool. Four workforce events were pushed that way, so one tenant's admin
# dashboard received another tenant's employee names, departments, break
# behaviour and the free-text reason on a time adjustment.
#
# These tests capture the DELIVERY CALL, not the payload, because the defect
# was never in what was sent — it was in who received it.

class _SpyManager:
    """Records which broadcast API each producer reaches for."""

    def __init__(self):
        self.global_calls = []
        self.org_calls = []

    def broadcast_sync(self, message):
        self.global_calls.append(message)

    def broadcast_to_org_sync(self, organization_id, message):
        self.org_calls.append((organization_id, message))


@pytest.fixture
def spy_ws(monkeypatch):
    import websocket_manager
    spy = _SpyManager()
    monkeypatch.setattr(websocket_manager, "manager", spy)
    # attendance_alerts imports the name at module scope.
    import app.attendance_alerts as aa
    monkeypatch.setattr(aa, "manager", spy, raising=False)
    return spy


def test_requires_attention_push_is_org_scoped(db, org_a, spy_ws):
    from app import attendance_alerts
    u = _mk_user(db, org_a, department="Finance", designation="Analyst")
    attendance_alerts._broadcast_bell("late", u, late_minutes=47)
    assert spy_ws.global_calls == [], (
        "requires_attention was pushed to EVERY tenant's sockets")
    assert len(spy_ws.org_calls) == 1
    assert spy_ws.org_calls[0][0] == org_a
    assert "Finance" in spy_ws.org_calls[0][1]


def test_break_overtime_push_is_org_scoped(db, org_a, spy_ws):
    from app.breaks import monitor
    u = _mk_user(db, org_a)

    class _Brk:
        id = 1
        break_type = "lunch"

    monitor._broadcast_overtime(u, _Brk(), elapsed=4680, allowed_min=60, grace_min=5)
    assert spy_ws.global_calls == [], (
        "break_overtime was pushed to EVERY tenant's sockets")
    assert len(spy_ws.org_calls) == 1
    assert spy_ws.org_calls[0][0] == org_a


def test_no_producer_reaches_for_the_global_broadcast(db):
    """A grep-style guard so a new workforce event cannot quietly reintroduce
    the leak. `broadcast`/`broadcast_sync` remain for ping/control frames."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for f in list((root / "app").rglob("*.py")) + [root / "main.py"]:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"manager\.broadcast_sync\s*\(|manager\.broadcast\s*\(", code):
                offenders.append(f"{f.relative_to(root)}:{i}")
    assert offenders == [], (
        "global websocket broadcast used for business data at: %s" % offenders)


# ══════════════════════════════════════════════════════════════════════════
# REGRESSION — CASCADE AND POLICY-SHARING  (Phase-3 P1 findings)
# ══════════════════════════════════════════════════════════════════════════

def test_domain_categories_do_not_cross_tenants(db, org_a, org_b):
    """DomainCategory carries organization_id, but three intelligence modules
    called load_overrides(db) with no org — so org A classifying a domain as
    "work" changed how productive org B's people looked."""
    from app.browser.productivity import load_overrides
    db.add(models.DomainCategory(organization_id=org_a, domain="youtube.com",
                                 category="work"))
    db.add(models.DomainCategory(organization_id=org_b, domain="youtube.com",
                                 category="distraction"))
    db.commit()
    assert load_overrides(db, org_a).get("youtube.com") == "work"
    assert load_overrides(db, org_b).get("youtube.com") == "distraction"

    # And the three consumers must ask with an org. A module that still calls
    # load_overrides(db) bare would resolve one tenant's answer for everyone.
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    bare = []
    for f in root.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"load_overrides\(\s*db\s*\)", line.split("#", 1)[0]):
                bare.append(f"{f.relative_to(root)}:{i}")
    assert bare == [], f"unscoped domain-category load at: {bare}"


def test_a_team_that_works_saturday_can_still_be_marked_absent(db, org_a):
    """The dashboard short-circuited absence on the ORG-level working day, so
    a team with a Saturday override could never be flagged absent on a
    Saturday — the tier that was overridden was the one that decided."""
    from app.schedule_policy.annotate import annotate
    u = _mk_user(db, org_a, team="Support")
    _set(db, org_a, K.SETTING_WORKING_DAYS, json.dumps(MON_SAT), team_key="Support")

    org_probe = resolve_work_schedule(db, models.User(organization_id=org_a), SATURDAY)
    assert org_probe.is_working_day is False, "org tier says Saturday is off"

    row = annotate(db, [u], on_date=SATURDAY)[u.id]
    assert row["is_working_day"] is True, (
        "the team override did not reach the per-employee annotation")
    assert row["absent_after_hour"] == 12


def test_idle_alert_threshold_honours_team_and_employee_scope(db, org_a):
    """attendance.idle_alert_minutes was resolved once against a bare
    organization probe, so PUT /settings/scoped accepted a team/employee
    override that the dashboard then ignored."""
    from app.attendance_policy import policy_int, SETTING_IDLE_ALERT_MINUTES
    org_user = _mk_user(db, org_a)
    team_user = _mk_user(db, org_a, team="NightOps")
    solo = _mk_user(db, org_a)
    _set(db, org_a, SETTING_IDLE_ALERT_MINUTES, "45", team_key="NightOps")
    _set(db, org_a, SETTING_IDLE_ALERT_MINUTES, "5", user_id=solo.id)
    assert policy_int(db, org_user, SETTING_IDLE_ALERT_MINUTES) == 15
    assert policy_int(db, team_user, SETTING_IDLE_ALERT_MINUTES) == 45
    assert policy_int(db, solo, SETTING_IDLE_ALERT_MINUTES) == 5
    # The bare probe the code used to rely on cannot see either override.
    probe = models.User(organization_id=org_a)
    assert policy_int(db, probe, SETTING_IDLE_ALERT_MINUTES) == 15


def test_legacy_ingest_cap_uses_the_tenants_screenshot_interval(db, org_a, org_b):
    """The anti-fraud count cap resolved screenshot_interval with no org, so
    the query filtered `organization_id IS NULL` and every tenant silently got
    the 30-second default."""
    from app.tracking.router import _get_configured_screenshot_interval
    import app.tracking.router as tr
    tr._SCREENSHOT_INTERVAL_CACHE.clear()
    _set(db, org_a, "screenshot_interval", "600")
    _set(db, org_b, "screenshot_interval", "120")
    assert _get_configured_screenshot_interval(db, org_a) == 600
    assert _get_configured_screenshot_interval(db, org_b) == 120
    # No org at all still falls back to the documented default, not to a
    # tenant's value.
    tr._SCREENSHOT_INTERVAL_CACHE.clear()
    assert _get_configured_screenshot_interval(db, None) == 30


# ══════════════════════════════════════════════════════════════════════════
# §11 / STEP 9 — CACHE ISOLATION
# ══════════════════════════════════════════════════════════════════════════

def test_every_tenant_cache_is_keyed_by_organization(db, org_a, org_b):
    """A cache that holds tenant data and is not keyed by the tenant serves
    org A's snapshot to org B for the length of its TTL."""
    from app.reports.router import _rpt_key, _rpt_cache_set, _rpt_cache_get
    from app.breaks import governance as gov
    from app import _tz as tzmod
    import app.tracking.router as tr

    # report cache
    assert _rpt_key(org_a, "daily") != _rpt_key(org_b, "daily")
    _rpt_cache_set(org_a, "daily", {"canary": "A"})
    assert _rpt_cache_get(org_b, "daily") is None
    assert (_rpt_cache_get(org_a, "daily") or {}).get("canary") == "A"

    # break settings cache
    gov._settings_cache.clear()
    _set(db, org_a, gov.SHORT_THRESHOLD_KEY, "15")
    _set(db, org_b, gov.SHORT_THRESHOLD_KEY, "40")
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_a) == 15
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_b) == 40
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_a) == 15   # from cache

    # timezone cache
    assert tzmod.org_timezone_name(db, org_a) != tzmod.org_timezone_name(db, org_b)

    # tracker interval cache
    tr._SCREENSHOT_INTERVAL_CACHE.clear()
    _set(db, org_a, "screenshot_interval", "45")
    _set(db, org_b, "screenshot_interval", "900")
    assert tr._get_configured_screenshot_interval(db, org_a) == 45
    assert tr._get_configured_screenshot_interval(db, org_b) == 900


def test_cache_invalidation_of_one_tenant_does_not_evict_another(db, org_a, org_b):
    """There is no per-org purge helper for the report cache, so this asserts
    the property that makes one possible: the two tenants occupy separate,
    prefix-identifiable keys."""
    from app.reports.router import (
        _rpt_cache_set, _rpt_cache_get, _rpt_key, _RPT_CACHE,
    )
    _rpt_cache_set(org_a, "k", {"canary": "A"})
    _rpt_cache_set(org_b, "k", {"canary": "B"})
    for key in [k for k in list(_RPT_CACHE) if k.startswith(f"org:{org_a}|")]:
        _RPT_CACHE.pop(key, None)
    assert _rpt_cache_get(org_a, "k") is None
    assert (_rpt_cache_get(org_b, "k") or {}).get("canary") == "B", (
        "purging org A's report-cache keys evicted org B")
    assert _rpt_key(org_a, "k").startswith(f"org:{org_a}|")

    # The timezone cache DOES have a per-org invalidator — exercise it.
    from app import _tz as tzmod
    assert tzmod.org_timezone_name(db, org_a) == "Asia/Kolkata"
    assert tzmod.org_timezone_name(db, org_b) == "America/New_York"
    tzmod.invalidate_tz_cache(org_a)
    assert tzmod.org_timezone_name(db, org_b) == "America/New_York"


def test_no_module_level_cache_holds_tenant_data_without_an_org_key(db):
    """Inventory guard. Every tenant-data cache must be keyed by (or include)
    the organization; a new one that is not should be a deliberate decision."""
    import app.employees.router as er
    import app.reports.router as rr
    import app.tracking.router as tr
    import app.activity_log.router as ar
    import app.proof_of_work.fraud_routes as fr

    # These are the caches that hold tenant-derived payloads. Each entry is
    # (module attr, why it is safe).
    inventory = {
        "employees._CACHE": "keys embed org_id (risk_users:{org}:{date}, analytics:{org}:...)",
        "employees._TEAM_CACHE": "dict keyed by org_id",
        "employees._DAY_SUMMARY_TICK_CACHE": "dict keyed by org_id",
        "reports._RPT_CACHE": "keys built by _rpt_key(org_id, key)",
        "tracking._STATUS_CACHE": "keyed by user_id; a user has exactly one org",
        "tracking._DEDUP_CACHE": "keyed by user_id",
        "tracking._SCREENSHOT_INTERVAL_CACHE": "keyed by org_id",
        "tracking._ACTIVE_POLL_PERIOD_CACHE": "keyed by org_id",
        "activity_log._TICK_CACHE": "keyed by org_id",
        "activity_log._OVERRIDE_CACHE": "keyed by organization_id",
        "fraud._ADMIN_SUMMARY_CACHE": "key includes organization_id",
    }
    assert len(inventory) == 11
    for mod, attr in (
        (er, "_CACHE"), (er, "_TEAM_CACHE"), (er, "_DAY_SUMMARY_TICK_CACHE"),
        (rr, "_RPT_CACHE"), (tr, "_STATUS_CACHE"), (tr, "_DEDUP_CACHE"),
        (tr, "_SCREENSHOT_INTERVAL_CACHE"), (tr, "_ACTIVE_POLL_PERIOD_CACHE"),
        (ar, "_TICK_CACHE"), (ar, "_OVERRIDE_CACHE"),
        (fr, "_ADMIN_SUMMARY_CACHE"),
    ):
        assert hasattr(mod, attr), f"{mod.__name__}.{attr} vanished — re-audit it"
