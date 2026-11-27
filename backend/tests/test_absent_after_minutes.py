"""Absence is measured from the employee's own shift start, per organization.

The production bug these tests pin: `absent_after_hour` defaulted to 12 for
every tenant, was compared as `now.hour >= 12` before anything looked at the
shift, and was not configurable from the admin UI. Eleven part-timers whose
shift starts at 16:30 were marked Absent — and their admins emailed — at 12:02
local, four and a half hours before their shift began.

The rule is now `shift_start + schedule.absent_after_minutes`, resolved through
the same employee > team > org > default cascade as every other threshold, with
`absent_after_hour` retained for employees who have no scheduled start at all.

Nothing here knows what a part-timer is. That is the point: an organization
whose part-timers start in the morning and one whose part-timers start in the
evening are both served by the same rule with different settings.
"""
import itertools
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from database import models
from database.database import Base

from app import _tz
from app.schedule_policy import keys as K
from app.schedule_policy.classify import ArrivalStatus, classify_arrival
from app.schedule_policy.resolver import resolve_work_schedule, resolve_work_schedules

# 64000 belongs to test_absent_cutoff_policy; ids must not collide because
# organizations.id is explicit in these fixtures.
_org_seq = itertools.count(66000)
WORKDAY = date(2026, 8, 6)              # Thursday
IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _new_org(db, tz_name=None) -> int:
    oid = next(_org_seq)
    db.add(models.Organization(id=oid, name=f"Org{oid}"))
    if tz_name:
        db.add(models.OrgSettings(organization_id=oid, default_timezone=tz_name))
    db.commit()
    return oid


def _mk_user(db, org_id, **kw):
    u = models.User(name="W", email=f"w_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value, *, user_id=None, team_key=None):
    db.add(models.AppSettings(organization_id=org_id, user_id=user_id,
                              team_key=team_key, key=key, value=value))
    db.commit()


def _at(hh, mm=0, tz=IST):
    """A local wall-clock instant on the test workday."""
    return datetime.combine(WORKDAY, time(hh, mm), tzinfo=tz)


def _org_with_shift(db, start="09:00", absent_after="60", **user_kw):
    """An org configured the way an admin would configure it: one org-level
    shift start and one org-level absence offset."""
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, start)
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, absent_after)
    return org, _mk_user(db, org, **user_kw)


def _status(db, user, now_local):
    sched = resolve_work_schedule(db, user, WORKDAY)
    return classify_arrival(None, sched, now_local).status


# ── TEST 1-2: the offset decides, not the hour ───────────────────────────────

def test_not_absent_before_the_offset_elapses(db):
    """09:00 + 60 -> 09:30 is still awaiting."""
    org, u = _org_with_shift(db, "09:00", "60")
    assert _status(db, u, _at(9, 30)) != ArrivalStatus.ABSENT


def test_absent_exactly_when_the_offset_elapses(db):
    org, u = _org_with_shift(db, "09:00", "60")
    assert _status(db, u, _at(10, 0)) == ArrivalStatus.ABSENT


# ── TEST 3-5: the afternoon shift — the production bug ───────────────────────

def test_afternoon_shift_is_not_absent_at_noon(db):
    """THE REGRESSION. A 16:30 employee at 12:00 was ABSENT under the old
    `now.hour >= absent_after_hour` rule, with the org's cutoff hour at its
    system default of 12."""
    org, u = _org_with_shift(db, "16:30", "60")
    assert _status(db, u, _at(12, 0)) != ArrivalStatus.ABSENT


def test_afternoon_shift_is_not_absent_one_minute_early(db):
    org, u = _org_with_shift(db, "16:30", "60")
    assert _status(db, u, _at(17, 29)) != ArrivalStatus.ABSENT


def test_afternoon_shift_is_absent_at_the_cutoff(db):
    org, u = _org_with_shift(db, "16:30", "60")
    assert _status(db, u, _at(17, 30)) == ArrivalStatus.ABSENT


def test_never_absent_before_the_shift_even_without_the_offset(db):
    """The mandatory guard, independent of any setting: an org that has NOT
    configured absent_after_minutes still cannot mark a 16:30 employee absent
    at noon. The cutoff hour is floored at the shift's own arrival window.

    The floor is a WINDOW, not the bare start instant. It was
    ``max(start, hour_cutoff)``, which kept the cutoff out of the pre-shift
    hours and then put it exactly ON the start: a 16:30 employee was Absent at
    16:30:00 and a 13:00 employee at 13:00:00, with zero grace, and since
    ``classify_arrival`` tests ABSENT before the grace branch their
    ``grace_minutes`` was never read. Every shift starting at or after the
    cutoff hour collapsed the same way. The floor is now
    ``very_late_after_minutes`` (default 120), so absence cannot pre-empt the
    LATE / VERY_LATE rungs that precede it.
    """
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "16:30")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert sched.absent_after_minutes is None          # nothing configured
    assert sched.absent_after_hour == 12               # the old cutoff survives
    assert sched.very_late_after_minutes == 120        # the floor that now binds
    assert classify_arrival(None, sched, _at(12, 0)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(16, 29)).status != ArrivalStatus.ABSENT
    # 16:30 is the shift start, so it is emphatically NOT absence any more —
    # this is the exact minute the reported 1 PM worker was emailed about.
    assert classify_arrival(None, sched, _at(16, 30)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(18, 29)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(18, 30)).status == ArrivalStatus.ABSENT


def test_morning_shift_cutoff_is_unchanged_when_nothing_is_configured(db):
    """Backward compatibility for the majority case: a 09:00 employee in an org
    that configured no offset still becomes Absent at the cutoff hour, exactly
    as before. The floor only binds shifts that start after it."""
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert classify_arrival(None, sched, _at(11, 59)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(12, 0)).status == ArrivalStatus.ABSENT


# ── TEST 6: tenant isolation ─────────────────────────────────────────────────

def test_two_organizations_keep_independent_cutoffs(db):
    """Org A 60 min, Org B 90 min, same 09:00 shift. At 10:15 exactly one of
    them is absent, and changing A must not move B."""
    org_a, ua = _org_with_shift(db, "09:00", "60")
    org_b, ub = _org_with_shift(db, "09:00", "90")

    assert _status(db, ua, _at(10, 15)) == ArrivalStatus.ABSENT
    assert _status(db, ub, _at(10, 15)) != ArrivalStatus.ABSENT
    assert _status(db, ub, _at(10, 30)) == ArrivalStatus.ABSENT

    before_b = resolve_work_schedule(db, ub, WORKDAY).absent_after_minutes
    _set(db, org_a, K.SETTING_ABSENT_AFTER_MINUTES, "5", user_id=ua.id)
    assert resolve_work_schedule(db, ua, WORKDAY).absent_after_minutes == 5
    assert resolve_work_schedule(db, ub, WORKDAY).absent_after_minutes == before_b == 90


def test_cascade_precedence_matches_the_other_thresholds(db):
    """employee > team > org, with provenance — the same cascade grace_minutes
    and late_after_minutes use. No employment-type tier is involved."""
    org = _new_org(db)
    u = _mk_user(db, org, team="Support")
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "30")
    assert resolve_work_schedule(db, u, WORKDAY).absent_after_minutes == 30
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "45", team_key="Support")
    assert resolve_work_schedule(db, u, WORKDAY).absent_after_minutes == 45
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "90", user_id=u.id)
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert sched.absent_after_minutes == 90
    assert sched.sources["absent_after_minutes"] == "employee"


# ── TEST 7: employees with no shift start ────────────────────────────────────

def test_no_shift_start_still_uses_the_cutoff_hour(db):
    """Most existing employees have no scheduled start. For them "minutes after
    the shift" has no meaning, so absent_after_hour must keep deciding."""
    org = _new_org(db)
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "60")   # configured but unusable
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "14")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert sched.start_local is None
    assert classify_arrival(None, sched, _at(13, 59)).status == ArrivalStatus.AWAITING
    assert classify_arrival(None, sched, _at(14, 0)).status == ArrivalStatus.ABSENT


# ── TEST 8: each organization on its own clock ───────────────────────────────

def test_each_organization_is_evaluated_in_its_own_timezone(db):
    """One UTC instant, two organizations, opposite verdicts. Both have a 09:00
    shift and a 60-minute cutoff; at 04:45 UTC it is 10:15 in Kolkata (past the
    cutoff) and 00:45 in New York (the shift has not started)."""
    org_in = _new_org(db, "Asia/Kolkata")
    org_us = _new_org(db, "America/New_York")
    for org in (org_in, org_us):
        _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
        _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "60")
    u_in, u_us = _mk_user(db, org_in), _mk_user(db, org_us)

    instant = datetime(2026, 8, 6, 4, 45, tzinfo=timezone.utc)
    now_in = _tz.to_local(db, org_in, instant)
    now_us = _tz.to_local(db, org_us, instant)
    assert (now_in.hour, now_us.hour) == (10, 0)

    assert classify_arrival(
        None, resolve_work_schedule(db, u_in, now_in.date()), now_in,
    ).status == ArrivalStatus.ABSENT
    assert classify_arrival(
        None, resolve_work_schedule(db, u_us, now_us.date()), now_us,
    ).status != ArrivalStatus.ABSENT


# ── TEST 9-10: employment type is not a factor ───────────────────────────────

def test_part_time_and_full_time_follow_the_same_rule(db):
    """Same org, same shift, same cutoff — the verdict must not depend on the
    employment type. The removed `PART_TIME_ABSENT_OFFSET_HOURS = 6` is exactly
    what this forbids."""
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "16:00")
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "60")
    pt = _mk_user(db, org, employment_type="part_time")
    ft = _mk_user(db, org, employment_type="full_time")
    for now, expected in ((_at(12, 0), False), (_at(16, 59), False), (_at(17, 0), True)):
        pt_absent = _status(db, pt, now) == ArrivalStatus.ABSENT
        ft_absent = _status(db, ft, now) == ArrivalStatus.ABSENT
        assert pt_absent == ft_absent == expected, now


def test_an_organization_can_give_its_part_timers_a_later_start(db):
    """The business requirement, end to end: full-timers at 09:00 and
    part-timers at 16:00 in ONE organization, one absence setting, correct
    verdicts for both at 12:00."""
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")       # org default
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "60")
    ft = _mk_user(db, org, employment_type="full_time")
    pt = _mk_user(db, org, employment_type="part_time")
    _set(db, org, K.SETTING_SHIFT_START_TIME, "16:00", user_id=pt.id)

    assert _status(db, ft, _at(12, 0)) == ArrivalStatus.ABSENT      # 09:00 + 60
    assert _status(db, pt, _at(12, 0)) != ArrivalStatus.ABSENT
    assert _status(db, pt, _at(17, 0)) == ArrivalStatus.ABSENT      # 16:00 + 60


# ── TEST 11: the roster wins ─────────────────────────────────────────────────

def test_cutoff_follows_the_shift_assignment_for_that_date(db):
    """A roster entry moves the employee's start, so it must move their absence
    cutoff too — the reason an absolute clock time cannot express this."""
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "60")
    u = _mk_user(db, org)
    db.add(models.ShiftAssignment(organization_id=org, user_id=u.id,
                                  date=WORKDAY.isoformat(), start_time="18:00"))
    db.commit()

    sched = resolve_work_schedule(db, u, WORKDAY)
    assert sched.start_local == time(18, 0)
    assert sched.sources["start_local"] == "assignment"
    assert classify_arrival(None, sched, _at(10, 0)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(18, 59)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(19, 0)).status == ArrivalStatus.ABSENT


# ── TEST 12: nothing else moved ──────────────────────────────────────────────

def test_late_and_very_late_are_unchanged(db):
    """The arrival ladder for someone who DID clock in is untouched by the
    absence change. Shift 09:00, grace 15, late-after 20, very-late 120."""
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, "60")
    _set(db, org, K.SETTING_LATE_AFTER_MINUTES, "20")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, WORKDAY)
    now = _at(20, 0)                       # end of day: classification is final
    for clock_in, expected in (
        (_at(9, 20), ArrivalStatus.PRESENT),
        (_at(9, 21), ArrivalStatus.LATE),
        (_at(11, 0), ArrivalStatus.VERY_LATE),
        (_at(13, 0), ArrivalStatus.VERY_LATE),
    ):
        assert classify_arrival(clock_in, sched, now).status == expected, clock_in


def test_late_pending_still_fires_between_grace_and_the_cutoff(db):
    """A no-show past grace but before the absence cutoff is LATE_PENDING — the
    Requires-Attention bucket. With the old rule an afternoon shift could never
    reach this state, because it was ABSENT from noon."""
    org, u = _org_with_shift(db, "16:30", "120")
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert classify_arrival(None, sched, _at(16, 0)).status == ArrivalStatus.AWAITING
    assert classify_arrival(None, sched, _at(17, 0)).status == ArrivalStatus.LATE_PENDING
    assert classify_arrival(None, sched, _at(18, 30)).status == ArrivalStatus.ABSENT


def test_non_working_day_is_never_absent(db):
    org, u = _org_with_shift(db, "09:00", "60")
    saturday = date(2026, 8, 8)
    sched = resolve_work_schedule(db, u, saturday)
    assert sched.is_working_day is False
    now = datetime.combine(saturday, time(23, 0), tzinfo=IST)
    assert classify_arrival(None, sched, now).status == ArrivalStatus.NOT_APPLICABLE


# ── Validation + plumbing ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["-5", "abc", "9999", ""])
def test_invalid_values_degrade_to_the_hour_rule(db, raw):
    """A malformed or out-of-range offset must not raise and must not invent a
    number: it falls back to absent_after_hour, the previous behaviour."""
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, raw)
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert sched.absent_after_minutes is None
    assert classify_arrival(None, sched, _at(12, 0)).status == ArrivalStatus.ABSENT


@pytest.mark.parametrize("start,offset,hour", [
    ("16:30", None, 12), ("16:30", 60, 12), ("09:00", None, 12),
    ("09:00", 600, 12), ("23:00", None, 12), ("00:30", 30, 12),
])
def test_the_cutoff_is_never_before_the_shift_start(db, start, offset, hour):
    """The invariant, stated once. /require-action derives `delay_minutes` from
    `cutoff - shift_start`, so a cutoff before the start rendered a negative
    lateness (12:00 - 16:30 = -270 minutes) on the admin card."""
    from app.schedule_policy.classify import absence_cutoff

    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, start)
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, str(hour))
    if offset is not None:
        _set(db, org, K.SETTING_ABSENT_AFTER_MINUTES, str(offset))
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, WORKDAY)

    now = _at(23, 59)
    start_dt = now.replace(hour=sched.start_local.hour,
                           minute=sched.start_local.minute, second=0, microsecond=0)
    assert absence_cutoff(sched, start_dt, now) >= start_dt


def test_zero_minutes_means_absent_at_the_shift_start(db):
    org, u = _org_with_shift(db, "09:00", "0")
    assert _status(db, u, _at(8, 59)) != ArrivalStatus.ABSENT
    assert _status(db, u, _at(9, 0)) == ArrivalStatus.ABSENT


def test_prefetched_and_per_call_resolution_agree(db):
    """resolve_work_schedules (the batched path used by the alert sweep and
    /employees/team) must return what per-user resolution returns."""
    org, u = _org_with_shift(db, "16:30", "45")
    batched = resolve_work_schedules(db, [u], WORKDAY)[u.id]
    assert batched.absent_after_minutes == 45
    assert batched == resolve_work_schedule(db, u, WORKDAY)


def test_the_setting_is_registered_and_rendered():
    """It has to reach the admin UI, or an organization still cannot configure
    its own absence timing — which was half of the original defect."""
    from app.settings import router as settings_router   # merges the meta
    from app.settings import scope as settings_scope

    assert K.SETTING_ABSENT_AFTER_MINUTES in settings_router.DEFAULTS
    assert settings_router.DEFAULTS[K.SETTING_ABSENT_AFTER_MINUTES] == ""
    meta = settings_scope.KEY_META[K.SETTING_ABSENT_AFTER_MINUTES]
    assert meta["label"] == "Absent After"
    assert meta["unit"] == "minutes"
    assert (meta["min"], meta["max"]) == (0, 1440)


def test_no_hardcoded_hour_comparison_survives_outside_the_fallback():
    """`now.hour >= absent_after_hour` may appear only inside the one helper
    that owns the no-shift fallback. Its reappearance anywhere else is the
    exact shape of the bug."""
    import inspect
    from app.schedule_policy import classify
    from app.employees import router as employees_router

    # The exact expression that shipped the bug, in either module.
    old = "now_local.hour >= int(sched.absent_after_hour)"
    assert old not in inspect.getsource(classify)
    assert "now_ist.hour >= int(hour)" not in inspect.getsource(employees_router)

    # And absence is decided in ONE function. Every executable read of the
    # cutoff hour lives in _absence_cutoff; the other mentions are docstrings.
    reads = [ln for ln in inspect.getsource(classify).splitlines()
             if "sched.absent_after_hour" in ln and not ln.lstrip().startswith("*")]
    assert len(reads) == 1, reads
    assert reads[0] in inspect.getsource(classify.absence_cutoff)

    # The two router paths that also need a cutoff must call the helper, not
    # rebuild it from the hour.
    router_src = inspect.getsource(employees_router)
    assert "absence_cutoff(sched, expected, now_ist)" in router_src
    assert "hour=int(sched.absent_after_hour)" not in router_src


# ── The 1 PM incident: absence must not pre-empt the arrival ladder ──────────
#
# Reported 2026-09-02. A worker whose shift starts at 13:00 was emailed about as
# Absent, and a workspace seat named "Super Admin" alongside them. This half of
# the fix is the 13:00 worker; the role half is in test_attendance_alert_roles.py.
#
# The fallback used to be `max(start_dt, hour_cutoff)`. It was written to stop a
# cutoff landing BEFORE the shift (the 16:30 part-timer incident above) and it
# does — but for any shift starting at or after the cutoff hour the max()
# collapses onto the start itself, so the cutoff WAS the shift start. Since
# classify_arrival tests ABSENT before the grace branch, such a worker could
# never be AWAITING, LATE_PENDING, LATE or VERY_LATE: they went straight to
# ABSENT at 13:00:00.

def _sched_for(db, start, **settings):
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, start)
    for key, value in settings.items():
        _set(db, org, getattr(K, key), str(value))
    return resolve_work_schedule(db, _mk_user(db, org), WORKDAY)


def test_one_pm_shift_is_not_absent_at_its_own_start(db):
    """THE REPORTED BUG, at the exact minute it fired."""
    sched = _sched_for(db, "13:00")
    assert sched.absent_after_minutes is None          # nothing configured
    assert classify_arrival(None, sched, _at(13, 0)).status != ArrivalStatus.ABSENT


def test_one_pm_shift_keeps_its_whole_arrival_ladder(db):
    """Every rung before ABSENT has to be reachable. Under the old fallback all
    four collapsed, which is why `grace_minutes` was dead for afternoon staff."""
    sched = _sched_for(db, "13:00")
    ladder = {
        _at(12, 30): ArrivalStatus.AWAITING,        # before the shift
        _at(13, 10): ArrivalStatus.AWAITING,        # inside the 15-min grace
        _at(13, 30): ArrivalStatus.LATE_PENDING,    # past grace, not yet absent
        _at(14, 59): ArrivalStatus.LATE_PENDING,
        _at(15, 0): ArrivalStatus.ABSENT,           # 13:00 + very_late 120
    }
    for now_local, expected in ladder.items():
        got = classify_arrival(None, sched, now_local).status
        assert got == expected, f"at {now_local:%H:%M} expected {expected} got {got}"


def test_grace_is_actually_consulted_for_an_afternoon_shift(db):
    """A widened grace has to move the AWAITING/LATE_PENDING boundary. It could
    not before: ABSENT short-circuited ahead of it."""
    sched = _sched_for(db, "13:00", SETTING_GRACE_MINUTES=90)
    assert classify_arrival(None, sched, _at(14, 0)).status == ArrivalStatus.AWAITING
    assert classify_arrival(None, sched, _at(14, 31)).status == ArrivalStatus.LATE_PENDING


def test_the_floor_respects_a_late_window_wider_than_very_late(db):
    """`_present_until` (max of grace and late_after) is independent of
    very_late_after_minutes, so the floor must be the max of BOTH. With
    late_after 300 and very_late 120, a clock-in at start+300 is still PRESENT —
    declaring the same instant Absent would contradict this module's own table.
    """
    sched = _sched_for(db, "13:00",
                       SETTING_LATE_AFTER_MINUTES=300,
                       SETTING_VERY_LATE_AFTER_MIN=120)
    assert classify_arrival(None, sched, _at(15, 0)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(17, 59)).status != ArrivalStatus.ABSENT
    assert classify_arrival(None, sched, _at(18, 0)).status == ArrivalStatus.ABSENT


def test_morning_shifts_are_bit_for_bit_unchanged(db):
    """The blast-radius guarantee. Any shift whose start + floor still lands at
    or before the cutoff hour keeps exactly the cutoff it had, so the majority
    of tenants see no behaviour change at all."""
    from app.schedule_policy.classify import absence_cutoff

    for start, hh, mm in (("06:00", 12, 0), ("08:00", 12, 0),
                          ("09:00", 12, 0), ("10:00", 12, 0)):
        sched = _sched_for(db, start)
        now = _at(23, 59)
        start_dt = now.replace(hour=sched.start_local.hour,
                               minute=sched.start_local.minute,
                               second=0, microsecond=0)
        assert absence_cutoff(sched, start_dt, now) == _at(hh, mm), start


@pytest.mark.parametrize("start", [
    "00:30", "06:00", "08:00", "09:00", "10:30", "11:00",
    "12:00", "13:00", "16:30", "22:00", "23:00",
])
@pytest.mark.parametrize("hour", [0, 8, 12, 18, 23])
def test_the_cutoff_can_only_move_later_never_earlier(db, start, hour):
    """THE PRODUCTION-SAFETY INVARIANT, and the reason this change was safe to
    deploy to live tenants: the new fallback is monotonically >= the old one, so
    no organization can begin receiving an absence alert SOONER than it did.
    A regression here means somebody starts getting early false absences.
    """
    from app.schedule_policy.classify import absence_cutoff

    sched = _sched_for(db, start, SETTING_ABSENT_AFTER_HOUR=hour)
    now = _at(23, 59)
    start_dt = now.replace(hour=sched.start_local.hour,
                           minute=sched.start_local.minute,
                           second=0, microsecond=0)
    old_rule = max(start_dt, now.replace(hour=hour, minute=0,
                                         second=0, microsecond=0))
    assert absence_cutoff(sched, start_dt, now) >= old_rule


def test_an_explicit_zero_offset_still_means_absent_at_the_start(db):
    """The floor must NOT override a deliberate configuration. An org that sets
    `absent_after_minutes` to 0 is asking for absence at the shift start, and
    that capability (pinned by test_zero_minutes_means_absent_at_the_shift_start)
    has to survive the floor, because the floor lives only in the fallback."""
    sched = _sched_for(db, "13:00", SETTING_ABSENT_AFTER_MINUTES=0)
    assert sched.absent_after_minutes == 0
    assert classify_arrival(None, sched, _at(13, 0)).status == ArrivalStatus.ABSENT


def test_a_negative_very_late_row_cannot_pull_the_cutoff_earlier(db):
    """The resolver coerces very_late with `_int_or(..., 120) or 120`, which lets
    a NEGATIVE value straight through (-5 or 120 == -5), and the settings write
    path does not enforce the meta min/max. A negative floor would break the
    monotonicity invariant above, so the floor clamps at 0."""
    from app.schedule_policy.classify import _absence_floor_minutes, absence_cutoff

    sched = _sched_for(db, "13:00", SETTING_VERY_LATE_AFTER_MIN=-999)
    assert _absence_floor_minutes(sched) >= 0
    now = _at(23, 59)
    start_dt = now.replace(hour=13, minute=0, second=0, microsecond=0)
    assert absence_cutoff(sched, start_dt, now) >= start_dt


def test_the_floor_survives_a_duck_typed_schedule(db):
    """`absence_cutoff` is public and its docstring promises duck-typing, so a
    caller may hand it an object with the attribute missing entirely or set to
    None. Degrade to 0 rather than raising inside the alert sweep."""
    from types import SimpleNamespace

    from app.schedule_policy.classify import _absence_floor_minutes

    assert _absence_floor_minutes(SimpleNamespace()) == 0
    assert _absence_floor_minutes(SimpleNamespace(
        grace_minutes=None, late_after_minutes=None,
        very_late_after_minutes=None)) == 0
    assert _absence_floor_minutes(SimpleNamespace(
        grace_minutes=15, late_after_minutes=None,
        very_late_after_minutes="not a number")) == 15
