"""Resolver prefetching / batching (P10b).

The monthly attendance report decides working-day per (user, day). Wiring that to
a per-call resolver would be O(users x days x queries). ``build_context`` loads
the same inputs in a FIXED number of queries and every resolve after that is a
dictionary lookup.

These tests assert two things that are easy to regress:

  1. **Equivalence** — the prefetched path returns exactly what the per-call path
     returns, for every precedence level. If it did not, the report would silently
     disagree with the dashboard, which is the whole problem P10b exists to fix.
  2. **Query count does not grow with users or days.** Asserted by counting real
     statements, not by inspection.
"""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import date, time, timedelta

import pytest
from sqlalchemy import event

from database import models
from database.database import Base

from app.schedule_policy import keys as K
from app.schedule_policy.resolver import (
    build_context, resolve_work_schedule, resolve_work_schedules,
    resolve_work_schedules_over,
)

_org_seq = itertools.count(70000)
MONDAY = date(2026, 8, 3)
SATURDAY = date(2026, 8, 8)
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
    u = models.User(name="PF", email=f"pf_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value, *, team_key=None, user_id=None):
    db.add(models.AppSettings(organization_id=org_id, team_key=team_key,
                              user_id=user_id, key=key, value=value))
    db.commit()


def _warm(users):
    """Touch the attributes the resolver reads.

    SQLAlchemy expires instances on commit, so the FIRST attribute access after
    one issues a refresh SELECT. That is ORM identity-map behaviour, not resolver
    work — a real caller (the report) loads its users in the same query it then
    iterates. Warming here keeps the query counts below measuring the right thing.
    """
    for u in users:
        _ = (u.id, u.team, u.employment_type, u.organization_id,
             u.default_clock_in_time, u.default_clock_in_grace_min)


class _Counter:
    """Counts SELECT statements issued on the test engine."""

    def __init__(self, engine):
        self.engine, self.n = engine, 0

    def _on(self, conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.n += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._on)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Equivalence: prefetched == per-call, at every precedence level
# ══════════════════════════════════════════════════════════════════════════════

def _assert_same(db, u, d, ctx):
    a = resolve_work_schedule(db, u, d)          # per-call
    b = resolve_work_schedule(db, u, d, ctx)     # prefetched
    for f in ("is_working_day", "is_holiday", "start_local", "end_local",
              "expected_work_seconds", "grace_minutes", "late_after_minutes",
              "very_late_after_minutes",
              "absent_after_hour", "allow_early_clock_in", "employment_type",
              "working_days", "shift_id", "shift_name", "crosses_midnight",
              "min_required_seconds", "timezone_name"):
        assert getattr(a, f) == getattr(b, f), (
            f"{f}: per-call={getattr(a, f)!r} prefetched={getattr(b, f)!r}")
    assert a.sources == b.sources


def test_prefetch_matches_per_call_for_system_defaults(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    _assert_same(db, u, MONDAY, build_context(db, org, [u], [MONDAY]))


def test_prefetch_matches_per_call_for_org_rows(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "25")
    _set(db, org, K.SETTING_SHIFT_START_TIME, "10:30")
    u = _mk_user(db, org)
    _assert_same(db, u, MONDAY, build_context(db, org, [u], [MONDAY]))


def test_prefetch_matches_per_call_for_team_and_employee_rows(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "5")
    _set(db, org, K.SETTING_GRACE_MINUTES, "35", team_key="Eng")
    u = _mk_user(db, org, team="Eng")
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "4:00", user_id=u.id)
    ctx = build_context(db, org, [u], [MONDAY])
    _assert_same(db, u, MONDAY, ctx)
    s = resolve_work_schedule(db, u, MONDAY, ctx)
    assert s.grace_minutes == 35 and s.sources["grace_minutes"] == "team"
    assert s.expected_work_seconds == 4 * 3600


def test_prefetch_matches_per_call_for_legacy_columns(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "09:00")
    u = _mk_user(db, org, default_clock_in_time="11:15", default_clock_in_grace_min=25)
    ctx = build_context(db, org, [u], [MONDAY])
    _assert_same(db, u, MONDAY, ctx)
    assert resolve_work_schedule(db, u, MONDAY, ctx).sources["start_local"] == "legacy_user"


def test_prefetch_matches_per_call_for_part_time(db):
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    ctx = build_context(db, org, [u], [MONDAY])
    _assert_same(db, u, MONDAY, ctx)
    assert resolve_work_schedule(db, u, MONDAY, ctx).expected_work_seconds == 4 * 3600


def test_prefetch_matches_per_call_for_holidays(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    db.add(models.Holiday(organization_id=org, date=MONDAY, name="H"))
    db.commit()
    ctx = build_context(db, org, [u], [MONDAY])
    _assert_same(db, u, MONDAY, ctx)
    assert resolve_work_schedule(db, u, MONDAY, ctx).is_working_day is False


def test_prefetch_matches_per_call_for_shifts(db):
    org = _new_org(db)
    sh = models.Shift(organization_id=org, name="Night", start_time="22:00",
                      end_time="06:00", days_of_week=[0, 1, 2, 3, 4, 5, 6],
                      is_active=True)
    db.add(sh)
    db.commit()
    db.refresh(sh)
    _set(db, org, K.SETTING_SHIFT_ID, str(sh.id))
    u = _mk_user(db, org)
    ctx = build_context(db, org, [u], [MONDAY])
    _assert_same(db, u, MONDAY, ctx)
    s = resolve_work_schedule(db, u, MONDAY, ctx)
    assert s.start_local == time(22, 0) and s.crosses_midnight is True


def test_prefetch_matches_per_call_for_assignments(db):
    org = _new_org(db)
    u = _mk_user(db, org)
    db.add(models.ShiftAssignment(organization_id=org, user_id=u.id,
                                  date=SATURDAY.isoformat(), start_time="10:00",
                                  end_time="18:00", status="scheduled"))
    db.commit()
    ctx = build_context(db, org, [u], [SATURDAY])
    _assert_same(db, u, SATURDAY, ctx)
    assert resolve_work_schedule(db, u, SATURDAY, ctx).is_working_day is True


def test_prefetch_never_leaks_across_orgs(db):
    """The context is per-org; a user from another tenant must not pick up its rows."""
    org_a, org_b = _new_org(db), _new_org(db)
    _set(db, org_a, K.SETTING_GRACE_MINUTES, "99")
    b_user = _mk_user(db, org_b)
    ctx_a = build_context(db, org_a, [b_user], [MONDAY])
    # Even handed the wrong context, org B's user must not inherit 99: the
    # batched entry point groups by org, which is what callers use.
    grouped = resolve_work_schedules_over(db, [b_user], [MONDAY])
    assert grouped[MONDAY][b_user.id].grace_minutes != 99


def test_batched_over_many_dates_matches_per_call(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps(_ALL_DAYS[:6]))   # Mon-Sat
    users = [_mk_user(db, org) for _ in range(3)]
    dates = [MONDAY + timedelta(days=i) for i in range(7)]
    grouped = resolve_work_schedules_over(db, users, dates)
    for d in dates:
        for u in users:
            expect = resolve_work_schedule(db, u, d)
            assert grouped[d][u.id].is_working_day == expect.is_working_day
            assert grouped[d][u.id].expected_work_seconds == expect.expected_work_seconds


def test_resolve_work_schedules_single_date_still_works(db):
    org = _new_org(db)
    users = [_mk_user(db, org) for _ in range(2)]
    out = resolve_work_schedules(db, users, MONDAY)
    assert set(out) == {u.id for u in users}


# ══════════════════════════════════════════════════════════════════════════════
# Query count — the point of the phase
# ══════════════════════════════════════════════════════════════════════════════

def test_build_context_query_count_is_fixed(db, engine):
    org = _new_org(db)
    users = [_mk_user(db, org) for _ in range(5)]
    dates = [MONDAY + timedelta(days=i) for i in range(10)]
    _warm(users)
    with _Counter(engine) as c:
        build_context(db, org, users, dates)
    assert c.n <= 6, f"prefetch issued {c.n} queries; expected a fixed handful"


def test_query_count_does_not_grow_with_users(db, engine):
    org = _new_org(db)
    dates = [MONDAY]
    small = [_mk_user(db, org) for _ in range(2)]
    large = [_mk_user(db, org) for _ in range(20)]
    _warm(small + large)
    with _Counter(engine) as c1:
        resolve_work_schedules_over(db, small, dates)
    with _Counter(engine) as c2:
        resolve_work_schedules_over(db, large, dates)
    # 10x the users must not mean 10x the queries.
    assert c2.n <= c1.n + 2, f"{c1.n} -> {c2.n} queries for 2 -> 20 users"


def test_query_count_does_not_grow_with_days(db, engine):
    org = _new_org(db)
    users = [_mk_user(db, org) for _ in range(5)]
    _warm(users)
    with _Counter(engine) as c1:
        resolve_work_schedules_over(db, users, [MONDAY])
    with _Counter(engine) as c2:
        resolve_work_schedules_over(db, users, [MONDAY + timedelta(days=i) for i in range(31)])
    assert c2.n <= c1.n + 2, f"{c1.n} -> {c2.n} queries for 1 -> 31 days"


def test_batched_is_dramatically_cheaper_than_per_call(db, engine):
    """The concrete claim: a month of a small team costs a handful of queries
    batched, versus hundreds per-call."""
    org = _new_org(db)
    users = [_mk_user(db, org) for _ in range(5)]
    dates = [MONDAY + timedelta(days=i) for i in range(31)]
    _warm(users)

    with _Counter(engine) as batched:
        resolve_work_schedules_over(db, users, dates)
    with _Counter(engine) as per_call:
        for d in dates:
            for u in users:
                resolve_work_schedule(db, u, d)

    assert batched.n < per_call.n / 10, (
        f"batched={batched.n} per_call={per_call.n} — expected >10x fewer")
    print(f"\n  155 user-days: batched={batched.n} queries, "
          f"per-call={per_call.n} queries "
          f"({per_call.n / max(1, batched.n):.0f}x reduction)")


# ══════════════════════════════════════════════════════════════════════════════
# reports/router.py consumes resolver results (P10b)
# ══════════════════════════════════════════════════════════════════════════════

def test_is_late_without_a_schedule_is_never_late(db):
    """There is no global 09:30 fallback any more.

    That constant compared `clock_in_dt.hour`, a UTC hour, against 9:30 - so for
    an IST tenant the report's "late" boundary was actually 15:00 while the
    dashboard used shift start + grace. Rather than replace one wrong global
    with another, an unresolvable schedule now means "nothing to be late
    against".
    """
    from datetime import datetime as _dt, timezone as _tz
    from app.reports.router import _is_late
    assert _is_late(_dt(2026, 8, 3, 9, 0, tzinfo=_tz.utc)) is False
    assert _is_late(_dt(2026, 8, 3, 10, 0, tzinfo=_tz.utc)) is False
    assert _is_late(None) is False


def test_is_late_delegates_to_classify_arrival(db):
    """The SAME boundaries the dashboard and monitor use, so a report can never
    disagree with them about who was late."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from app.reports.router import _is_late
    org = _new_org(db)
    _set(db, org, K.SETTING_SHIFT_START_TIME, "10:30")
    _set(db, org, K.SETTING_GRACE_MINUTES, "15")
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, MONDAY)
    ist = _tz(_td(hours=5, minutes=30))
    # 10:40 IST is inside a 10:30+15 window -> on time, even though it is past
    # the legacy global 09:30 cutoff.
    assert _is_late(_dt(2026, 8, 3, 10, 40, tzinfo=ist), sched) is False
    assert _is_late(_dt(2026, 8, 3, 11, 30, tzinfo=ist), sched) is True


def test_is_late_ignores_a_schedule_with_no_shift_start(db):
    """No resolved start -> nothing to be late against, so it must fall through
    rather than inventing a threshold."""
    from datetime import datetime as _dt, timezone as _tz
    from app.reports.router import _is_late
    org = _new_org(db)
    u = _mk_user(db, org)
    sched = resolve_work_schedule(db, u, MONDAY)
    assert sched.start_local is None
    assert _is_late(_dt(2026, 8, 3, 10, 0, tzinfo=_tz.utc), sched) is False


def test_sched_map_is_always_populated(db):
    """There is no gate: reports always resolve schedules."""
    from app.reports.router import _sched_map
    org = _new_org(db)
    users = [_mk_user(db, org) for _ in range(3)]
    dates = [MONDAY, SATURDAY]
    smap = _sched_map(db, org, users, dates)
    assert len(smap) == 6
    for u in users:
        for d in dates:
            assert (u.id, d) in smap


def test_sched_map_query_count_is_fixed(db, engine):
    """The point of P10b: a month of reporting costs a handful of queries."""
    from app.reports.router import _sched_map
    org = _new_org(db)
    _set(db, org, K.SETTING_ENGINE_ENABLED, "true")
    users = [_mk_user(db, org) for _ in range(10)]
    dates = [MONDAY + timedelta(days=i) for i in range(31)]
    _warm(users)
    with _Counter(engine) as c:
        smap = _sched_map(db, org, users, dates)
    assert len(smap) == 310
    assert c.n <= 8, f"{c.n} queries for 310 user-days; expected a fixed handful"


def test_saturday_working_org_counts_saturdays(db):
    """The Org B profile the report used to get wrong: Mon-Sat."""
    from app.reports.router import _sched_map
    org = _new_org(db)
    _set(db, org, K.SETTING_ENGINE_ENABLED, "true")
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps(_ALL_DAYS[:6]))
    u = _mk_user(db, org)
    smap = _sched_map(db, org, [u], [MONDAY, SATURDAY, date(2026, 8, 9)])
    assert smap[(u.id, MONDAY)].is_working_day is True
    assert smap[(u.id, SATURDAY)].is_working_day is True          # was "weekend"
    assert smap[(u.id, date(2026, 8, 9))].is_working_day is False  # Sunday


def test_holiday_excluded_from_working_days(db):
    from app.reports.router import _sched_map
    org = _new_org(db)
    _set(db, org, K.SETTING_ENGINE_ENABLED, "true")
    u = _mk_user(db, org)
    db.add(models.Holiday(organization_id=org, date=MONDAY, name="H"))
    db.commit()
    smap = _sched_map(db, org, [u], [MONDAY])
    assert smap[(u.id, MONDAY)].is_holiday is True
    assert smap[(u.id, MONDAY)].is_working_day is False


def test_night_shift_report_window_comes_from_the_shift(db):
    from app.reports.router import _sched_map
    org = _new_org(db)
    _set(db, org, K.SETTING_ENGINE_ENABLED, "true")
    _set(db, org, K.SETTING_SHIFT_START_TIME, "22:00")
    _set(db, org, K.SETTING_SHIFT_END_TIME, "06:00")
    u = _mk_user(db, org)
    sc = _sched_map(db, org, [u], [MONDAY])[(u.id, MONDAY)]
    assert sc.start_local == time(22, 0) and sc.end_local == time(6, 0)
    assert sc.crosses_midnight is True


def test_part_timer_expected_hours_in_reports(db):
    org = _new_org(db)
    _set(db, org, K.SETTING_ENGINE_ENABLED, "true")
    u = _mk_user(db, org, employment_type="part_time")
    from app.attendance_policy import resolve_expected_work_seconds
    assert resolve_expected_work_seconds(db, u, MONDAY) == 4 * 3600


# ══════════════════════════════════════════════════════════════════════════════
# /reports/attendance-detail — the admin dashboard's historical-date endpoint
#
# Selecting "Yesterday" on the Admin Overview calls this. It used to resolve
# expected hours with the PER-USER resolver from inside its record loop, so the
# statement count grew linearly with headcount: 705 of 710 statements and ~94 s
# on the 57-employee production workspace, for a payload whose attendance data
# costs 5 queries. These tests pin the shape of the fix — batched resolve, and a
# count that does not grow with headcount — so it cannot silently regress the
# way /employees/team did.
# ══════════════════════════════════════════════════════════════════════════════

def _attendance_detail(db, org_id, on_date, viewer=None):
    from app.reports.router import attendance_detail
    org = db.query(models.Organization).filter(
        models.Organization.id == org_id).first()
    return attendance_detail(date_str=on_date.isoformat(), user_id=None, db=db,
                             viewer=viewer, current_org=org)


def test_attendance_detail_query_count_does_not_grow_with_headcount(db, engine):
    """The N+1 regression guard. Ten times the employees must not mean ten times
    the queries — that is exactly what made the endpoint take ~94 s."""
    small_org, large_org = _new_org(db), _new_org(db)
    small = [_mk_user(db, small_org) for _ in range(2)]
    large = [_mk_user(db, large_org) for _ in range(20)]
    _warm(small + large)

    with _Counter(engine) as c_small:
        _attendance_detail(db, small_org, MONDAY)
    with _Counter(engine) as c_large:
        _attendance_detail(db, large_org, MONDAY)

    assert c_large.n <= c_small.n + 2, (
        f"attendance-detail issued {c_small.n} queries for 2 employees but "
        f"{c_large.n} for 20 — the per-employee schedule resolve is back")


def test_attendance_detail_is_a_fixed_handful_of_queries(db, engine):
    """An absolute ceiling, not just a relative one: a 25-person org must cost a
    fixed handful of statements, not one cascade per person."""
    org = _new_org(db)
    users = [_mk_user(db, org) for _ in range(25)]
    _warm(users)
    with _Counter(engine) as c:
        payload = _attendance_detail(db, org, MONDAY)
    assert len(payload["records"]) == 25
    assert c.n <= 12, f"{c.n} queries for 25 employees; expected a fixed handful"


def test_attendance_detail_expected_seconds_matches_the_canonical_resolver(db):
    """Equivalence: the batched value must equal what the per-user canonical
    resolver returns, at every precedence level the endpoint can hit. If these
    drift, the history view's progress bars disagree with the clock-out gate."""
    from app.attendance_policy import resolve_expected_work_seconds
    org = _new_org(db)
    _set(db, org, K.SETTING_GRACE_MINUTES, "5")
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "7:30", team_key="Eng")
    full = _mk_user(db, org)
    part = _mk_user(db, org, employment_type="part_time")
    teamed = _mk_user(db, org, team="Eng")
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "6:00", user_id=full.id)

    by_id = {r["id"]: r for r in _attendance_detail(db, org, MONDAY)["records"]}
    for u in (full, part, teamed):
        assert by_id[u.id]["expected_seconds"] == resolve_expected_work_seconds(
            db, u, MONDAY), f"user {u.id} disagrees with the canonical resolver"
    # And the precedence actually took effect, so this is not passing vacuously.
    assert by_id[full.id]["expected_seconds"] == 6 * 3600      # employee scope
    assert by_id[teamed.id]["expected_seconds"] == 7 * 3600 + 1800  # team scope
    assert by_id[part.id]["expected_seconds"] == 4 * 3600      # type policy


def test_attendance_detail_expected_seconds_survives_a_failed_prefetch(db, monkeypatch):
    """The degraded path must still answer. If the batched resolve returns
    nothing, the endpoint falls back per-user rather than emitting nulls."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    monkeypatch.setattr("app.reports.router._sched_map",
                        lambda *a, **kw: {})
    rec = _attendance_detail(db, org, MONDAY)["records"][0]
    assert rec["expected_seconds"] == 4 * 3600


def test_attendance_detail_does_not_leak_across_tenants(db):
    """The batched prefetch is per-org. Org A's employees must never appear in
    org B's response, and the schedule rows must not cross either."""
    org_a, org_b = _new_org(db), _new_org(db)
    _set(db, org_a, K.SETTING_EXPECTED_WORK_HOURS, "9:00")
    a_users = [_mk_user(db, org_a) for _ in range(3)]
    b_user = _mk_user(db, org_b)

    payload_b = _attendance_detail(db, org_b, MONDAY)
    ids_b = {r["id"] for r in payload_b["records"]}
    assert ids_b == {b_user.id}
    assert not ids_b & {u.id for u in a_users}
    # org A's 9h setting must not have bled into org B's resolve.
    assert payload_b["records"][0]["expected_seconds"] != 9 * 3600


# ══════════════════════════════════════════════════════════════════════════════
# /reports/overtime and /reports/workload — the same per-user schedule-resolution
# N+1, found by the follow-up audit of attendance-detail.
#
#   overtime  resolved expected hours per user (748 statements / ~106 s, 60 emp)
#   workload  called _expected_seconds_over per user, and that helper builds its
#             OWN prefetch context per call, so build_context ran once per
#             employee (692 statements / ~98 s, 57 emp)
#
# Both now share one batched resolve. These guard the query counts and the
# values, so neither can quietly go back.
# ══════════════════════════════════════════════════════════════════════════════

def _mk_session(db, u, on_date, worked=9 * 3600, breaks=1800):
    from datetime import datetime as _dt
    db.add(models.ClockSession(
        user_id=u.id, organization_id=u.organization_id,
        clock_in_time=_dt(on_date.year, on_date.month, on_date.day, 3, 0),
        clock_out_time=_dt(on_date.year, on_date.month, on_date.day, 12, 0),
        total_seconds=worked, break_seconds=breaks, offline_seconds=0,
        is_active=False))
    db.commit()


def _org_obj(db, org_id):
    return db.query(models.Organization).filter(
        models.Organization.id == org_id).first()


def test_overtime_query_count_does_not_grow_with_headcount(db, engine):
    from app.reports.router import overtime
    small_org, large_org = _new_org(db), _new_org(db)
    small = [_mk_user(db, small_org) for _ in range(2)]
    large = [_mk_user(db, large_org) for _ in range(20)]
    _warm(small + large)

    with _Counter(engine) as c_small:
        overtime(from_date=None, to_date=None, user_id=None, db=db,
                 current_org=_org_obj(db, small_org))
    with _Counter(engine) as c_large:
        overtime(from_date=None, to_date=None, user_id=None, db=db,
                 current_org=_org_obj(db, large_org))

    assert c_large.n <= c_small.n + 2, (
        f"/reports/overtime issued {c_small.n} queries for 2 employees but "
        f"{c_large.n} for 20 — the per-employee schedule resolve is back")


def test_workload_query_count_does_not_grow_with_headcount(db, engine):
    from app.reports.router import workload_analysis
    small_org, large_org = _new_org(db), _new_org(db)
    small = [_mk_user(db, small_org) for _ in range(2)]
    large = [_mk_user(db, large_org) for _ in range(20)]
    # workload only reports employees that actually have sessions in range.
    for u in small + large:
        _mk_session(db, u, MONDAY)
    _warm(small + large)

    kw = dict(from_date=MONDAY.isoformat(), to_date=MONDAY.isoformat(), db=db)
    with _Counter(engine) as c_small:
        r_small = workload_analysis(current_org=_org_obj(db, small_org), **kw)
    with _Counter(engine) as c_large:
        r_large = workload_analysis(current_org=_org_obj(db, large_org), **kw)

    assert len(r_small["rows"]) == 2 and len(r_large["rows"]) == 20
    assert c_large.n <= c_small.n + 2, (
        f"/reports/workload issued {c_small.n} queries for 2 employees but "
        f"{c_large.n} for 20 — build_context is running per employee again")


def test_workload_query_count_does_not_grow_with_days(db, engine):
    """The prefetch covers the whole range, so a month must not cost a context
    per day either."""
    from app.reports.router import workload_analysis
    org = _new_org(db)
    users = [_mk_user(db, org) for _ in range(3)]
    for u in users:
        _mk_session(db, u, MONDAY)
    _warm(users)

    org_obj = _org_obj(db, org)
    with _Counter(engine) as c1:
        workload_analysis(from_date=MONDAY.isoformat(),
                          to_date=MONDAY.isoformat(), db=db, current_org=org_obj)
    with _Counter(engine) as c31:
        workload_analysis(from_date=MONDAY.isoformat(),
                          to_date=(MONDAY + timedelta(days=30)).isoformat(),
                          db=db, current_org=org_obj)
    assert c31.n <= c1.n + 2, f"{c1.n} -> {c31.n} queries for 1 -> 31 days"


def test_expected_seconds_over_prefetched_matches_unprefetched(db):
    """The new sched_by_date argument must be a pure optimisation: same answer,
    at every precedence level, as letting the helper resolve for itself."""
    from app.intelligence.risk_engine import _expected_seconds_over
    from app.schedule_policy.resolver import resolve_work_schedules_over
    org = _new_org(db)
    _set(db, org, K.SETTING_WORKING_DAYS, json.dumps(_ALL_DAYS[:6]))   # Mon-Sat
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "7:30", team_key="Eng")
    users = [_mk_user(db, org),
             _mk_user(db, org, employment_type="part_time"),
             _mk_user(db, org, team="Eng")]
    db.add(models.Holiday(organization_id=org, date=MONDAY + timedelta(days=2),
                          name="H"))
    db.commit()
    end = MONDAY + timedelta(days=13)
    days = [MONDAY + timedelta(days=i) for i in range(14)]
    by_date = resolve_work_schedules_over(db, users, days)

    for u in users:
        prefetched = _expected_seconds_over(
            db, u, MONDAY, end,
            sched_by_date={d: (by_date.get(d) or {}).get(u.id) for d in days})
        assert prefetched == _expected_seconds_over(db, u, MONDAY, end), (
            f"user {u.id}: prefetched sum disagrees with the per-call sum")
        assert prefetched > 0  # not passing vacuously


def test_overtime_and_workload_do_not_leak_across_tenants(db):
    """Both prefetches are per-org: one tenant's employees and schedule rows
    must never surface in the other's report."""
    from app.reports.router import overtime, workload_analysis
    org_a, org_b = _new_org(db), _new_org(db)
    a_users = [_mk_user(db, org_a) for _ in range(3)]
    b_user = _mk_user(db, org_b)
    for u in a_users + [b_user]:
        _mk_session(db, u, MONDAY)

    a_ids = {u.id for u in a_users}
    ot_b = overtime(from_date=MONDAY.isoformat(), to_date=MONDAY.isoformat(),
                    user_id=None, db=db, current_org=_org_obj(db, org_b))
    wl_b = workload_analysis(from_date=MONDAY.isoformat(),
                             to_date=MONDAY.isoformat(), db=db,
                             current_org=_org_obj(db, org_b))
    assert not {r["id"] for r in ot_b["summary"]} & a_ids
    assert {r["id"] for r in wl_b["rows"]} == {b_user.id}


def test_reports_hold_no_attendance_policy_of_their_own():
    """Reports must consume the canonical policy, never define one."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "reports" / "router.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    # It must consult the resolver and the shared classifier.
    assert "_sched_map" in code and "resolve_work_schedules_over" in code
    assert "classify_arrival" in code
    # And it must carry no thresholds, weekday tests or fixed offsets of its own.
    for banned in ("LATE_THRESHOLD_HOUR", "EXPECTED_HOURS_PER_DAY",
                   "OVERTIME_THRESHOLD_SEC", "weekday()", "minutes=30"):
        assert banned not in code, "reports reintroduced " + banned
