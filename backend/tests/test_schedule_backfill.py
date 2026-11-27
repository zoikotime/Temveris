"""Backfill plan: the migration that carries today's numbers into settings.

The fixture mirrors the shape measured in production on 2026-08-03 by read-only
audit, so the proof is about real data characteristics rather than invented ones:

  * 4 organisations; org 1 is the large tenant
  * 69 users, of which 7 have a real ``default_clock_in_time``
  * ALL users carry ``default_clock_in_grace_min = 20`` (the model-side default)
  * 11 part-time users, all in org 1
  * only org 1 configures ``work_hours_threshold`` (8:45)
  * no org configures ``expected_work_durations`` or ``flexible_clock_out_days``
  * a holiday on 2026-08-15 observed by all four orgs, and one on 2026-09-04
    observed by orgs 3 and 6 only — so tenant-scoped holidays are exercised
  * one user with ``organization_id = NULL``

Everything runs against the SQLite test database on the P3 schema. No production
access: an earlier attempt to verify by looping the resolver over the wire caused
Supabase rate limiting and held ACCESS EXCLUSIVE locks, and is not repeated.

The legacy-vs-resolver equivalence proof that used to live here is gone with the
legacy implementation it measured. What replaces it is stricter and more direct:
these tests assert that after the migration runs, the RESOLVER produces the same
numbers the removed constants produced — expected hours, grace, shift start and
the part-time absence cutoff — for every user in a production-shaped fixture.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import itertools

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from database import models                     # noqa: E402
from database.database import Base              # noqa: E402
from app.schedule_policy import keys as K       # noqa: E402

import backfill_schedule_policy as bf           # noqa: E402

MONDAY = date(2026, 8, 3)
SATURDAY = date(2026, 8, 8)
SUNDAY = date(2026, 8, 9)
HOLIDAY_ALL = date(2026, 8, 15)      # observed by every org
HOLIDAY_SOME = date(2026, 9, 4)      # orgs 3 and 6 only; a working Friday
DATES = [MONDAY, SATURDAY, SUNDAY, HOLIDAY_ALL, HOLIDAY_SOME]

# Production: 7 users with a configured legacy clock-in, all in org 1.
LEGACY_CLOCK_INS = ["10:30", "09:30", "10:30", "11:00", "10:30", "10:30", "13:30"]

# Fresh org-id range per fixture use — the session-scoped SQLite DB keeps rows.
# Step is deliberately small so the range stays compact and cannot wander into
# the fixed ids other suites hardcode (test_subscription_ops uses org 700001).
_base_seq = itertools.count(31000, 100)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def prod_like(db):
    """A replica of production's shape.

    The session-scoped SQLite database keeps rows across tests, so each
    invocation allocates a fresh id range rather than reusing fixed ids.
    """
    base = next(_base_seq)
    orgs = {"big": base + 1, "internal": base + 3, "harish": base + 6, "indra": base + 7}
    for oid in orgs.values():
        db.add(models.Organization(id=oid, name=f"Org{oid}"))
    # Only the large tenant configures a work-hours threshold.
    db.add(models.AppSettings(organization_id=orgs["big"],
                              key="work_hours_threshold", value="8:45"))
    db.add(models.AppSettings(organization_id=orgs["big"],
                              key="risk_alert_enabled", value="true"))
    # Timezone data as it really is: free text, valid, and NULL.
    db.add(models.OrgSettings(organization_id=orgs["big"],
                              default_timezone="California, Sacramento"))
    db.add(models.OrgSettings(organization_id=orgs["internal"],
                              default_timezone="Asia/Kolkata"))
    db.add(models.OrgSettings(organization_id=orgs["harish"], default_timezone=None))
    db.commit()

    users = []
    seq = itertools.count(1)

    def add_user(org_id, emp_type="full_time", clock_in=None, grace=20, active=True):
        # Ids are left to the database. Assigning them explicitly collided with
        # the fixed user ids other suites hardcode once this fixture had been
        # used enough times for its range to reach them.
        u = models.User(name=f"U{base}-{next(seq)}",
                        email=f"u{base}_{next(seq)}@sched.example.com",
                        organization_id=org_id, employment_type=emp_type,
                        default_clock_in_time=clock_in,
                        default_clock_in_grace_min=grace, is_active=active)
        db.add(u)
        users.append(u)
        return u

    # org 1: 48 full-time (7 of them with a legacy clock-in) + 11 part-time
    for i in range(48):
        add_user(orgs["big"], "full_time",
                 clock_in=LEGACY_CLOCK_INS[i] if i < len(LEGACY_CLOCK_INS) else None,
                 active=i >= 45 or True)
    for _ in range(11):
        add_user(orgs["big"], "part_time")
    add_user(orgs["internal"])
    for _ in range(5):
        add_user(orgs["harish"])
    for _ in range(3):
        add_user(orgs["indra"])
    add_user(None)                      # the org-less user

    for oid in orgs.values():
        db.add(models.Holiday(organization_id=oid, date=HOLIDAY_ALL,
                              name="Independence Day"))
    for oid in (orgs["internal"], orgs["harish"]):
        db.add(models.Holiday(organization_id=oid, date=HOLIDAY_SOME,
                              name="Janmashtami"))
    db.commit()
    return {"orgs": orgs, "users": users}


def _users_in(db, prod_like):
    ids = [u.id for u in prod_like["users"]]
    return db.query(models.User).filter(models.User.id.in_(ids)).order_by(models.User.id).all()


# The numbers the removed constants produced, per user, on a working Monday.
# Written out explicitly rather than derived, so the migration is measured
# against a stated expectation instead of against another piece of code that
# could drift with it.
LEGACY_GRACE = 20
LEGACY_ABSENT_HOUR_FULL_TIME = 12
LEGACY_ABSENT_HOUR_PART_TIME = 18        # the removed +6h rule
LEGACY_EXPECTED_FULL_TIME = 8 * 3600     # expected_work_durations default
LEGACY_EXPECTED_PART_TIME = 4 * 3600


def _snapshot(db, prod_like, on_date=None):
    """{user_id: resolved facts} for the fixture's users."""
    on_date = on_date or MONDAY
    out = {}
    for u in _users_in(db, prod_like):
        out[u.id] = bf.resolver_view(db, u, on_date)
    return out


def _legacy_expected_for(u):
    return (LEGACY_EXPECTED_PART_TIME
            if (u.employment_type or "").lower().strip() == "part_time"
            else LEGACY_EXPECTED_FULL_TIME)


def _legacy_absent_hour_for(u):
    return (LEGACY_ABSENT_HOUR_PART_TIME
            if (u.employment_type or "").lower().strip() == "part_time"
            else LEGACY_ABSENT_HOUR_FULL_TIME)


def test_fixture_matches_the_measured_production_shape(db, prod_like):
    users = _users_in(db, prod_like)
    assert len(users) == 69
    assert sum(1 for u in users if u.default_clock_in_time) == 7
    assert sum(1 for u in users if u.employment_type == "part_time") == 11
    assert all(u.default_clock_in_grace_min == 20 for u in users)
    assert sum(1 for u in users if u.organization_id is None) == 1


# ══════════════════════════════════════════════════════════════════════════════
# The plan
# ══════════════════════════════════════════════════════════════════════════════

def test_plan_writes_only_what_is_needed(db, prod_like):
    plan = bf.build_plan(db)
    orgs = set(prod_like["orgs"].values())

    grace_rows = [r for r in plan.org_settings
                  if r[0] in orgs and r[1] == K.SETTING_GRACE_MINUTES]
    assert len(grace_rows) == len(orgs)
    assert all(v == "20" for _, _, v in grace_rows)

    # Expected hours is migrated too — one row per org, carrying the number the
    # Early Clock Out gate already allowed people to leave on.
    exp_rows = [r for r in plan.org_settings
                if r[0] in orgs and r[1] == K.SETTING_EXPECTED_WORK_HOURS]
    assert len(exp_rows) == len(orgs)
    assert all(v == "8:00" for _, _, v in exp_rows)

    shifts = [s for s in plan.shifts if s[0] in orgs]
    assert len(shifts) == len(orgs), "exactly one default shift per organization"

    start_rows = [r for r in plan.user_settings
                  if r[0] in orgs and r[2] == K.SETTING_SHIFT_START_TIME]
    assert len(start_rows) == 7, "only the users who configured a legacy clock-in"

    # Every part-timer gets the two rows that replace the removed constants.
    cutoff_rows = [r for r in plan.user_settings
                   if r[0] in orgs and r[2] == K.SETTING_ABSENT_AFTER_HOUR]
    assert len(cutoff_rows) == 11
    assert all(v == "18" for _, _, _, v in cutoff_rows)


def test_plan_never_stores_an_inherited_value(db, prod_like):
    """Rule: persist only overrides. Anything the resolver already inherits
    correctly must not become a row.

    `expected_work_hours` and `absent_after_hour` left this list when the
    constants they replace were deleted: they are no longer inherited correctly,
    because there is nothing left to inherit them from.
    """
    plan = bf.build_plan(db)
    written = {k for _, k, _ in plan.org_settings} | {k for _, _, k, _ in plan.user_settings}
    for key in (K.SETTING_WORKING_DAYS,
                K.SETTING_ENGINE_ENABLED,
                K.SETTING_SHIFT_END_TIME, K.SETTING_VERY_LATE_AFTER_MIN,
                K.SETTING_MIN_HOURS_REQUIRED,
                K.SETTING_ALLOW_EARLY_CLOCK_IN, K.SETTING_ALLOW_LATE_CLOCK_OUT,
                K.SETTING_WORK_MODE, K.SETTING_SHIFT_ID,
                K.SETTING_EMPLOYMENT_TYPE, K.SETTING_LATE_AFTER_MINUTES):
        assert key not in written, f"{key} would be stored but is inherited correctly"


def test_expected_work_hours_is_written_from_the_permissive_number(db, prod_like):
    """Consolidating two live values into one means choosing.

    The risk engine measured against work_hours_threshold (8:45); the Early Clock
    Out gate let people leave at expected_work_durations (8:00). The migration
    writes 8:00 — the number employees were ALREADY allowed to leave on. Writing
    8:45 instead would start alerting people the system had just told they could
    go home."""
    plan = bf.build_plan(db)
    org_rows = {(o, v) for o, k, v in plan.org_settings
                if k == K.SETTING_EXPECTED_WORK_HOURS}
    assert org_rows, "expected hours must be migrated, not left to a new default"
    assert all(v == "8:00" for _, v in org_rows), org_rows


def test_part_time_expected_hours_becomes_an_employee_row(db, prod_like):
    """`expected_work_durations` keyed a duration by employment type, and the
    cascade has no employment-type tier — so a part-timer's 4h becomes an
    employee-scope row, addressable per person from now on."""
    plan = bf.build_plan(db)
    part_ids = {u.id for u in _users_in(db, prod_like)
                if (u.employment_type or "") == "part_time"}
    written = {uid for _, uid, k, v in plan.user_settings
               if k == K.SETTING_EXPECTED_WORK_HOURS and v == "4:00"}
    assert part_ids <= written, f"part-timers missing an expected-hours row: {part_ids - written}"


def test_no_employee_grace_rows_because_all_match_the_org_value(db, prod_like):
    plan = bf.build_plan(db)
    orgs = set(prod_like["orgs"].values())
    assert not any(k == K.SETTING_GRACE_MINUTES
                   for o, _, k, _ in plan.user_settings if o in orgs)


def test_default_shift_is_not_referenced_by_shift_id(db, prod_like):
    """The seeded shift must not change resolution — nothing points at it."""
    plan = bf.build_plan(db)
    bf.apply_plan(db, plan)
    db.commit()
    assert not any(k == K.SETTING_SHIFT_ID for _, k, _ in plan.org_settings)
    for u in _users_in(db, prod_like):
        assert bf.resolver_view(db, u, MONDAY)["start"] != "shift-derived"
    seeded = db.query(models.Shift).filter(
        models.Shift.organization_id.in_(prod_like["orgs"].values())).all()
    assert len(seeded) == len(prod_like["orgs"])
    assert all(s.is_default is True for s in seeded)
    assert all(s.expected_work_seconds is None for s in seeded), \
        "a seeded expected duration would override the cascade"


def test_plan_is_idempotent(db, prod_like):
    plan = bf.build_plan(db)
    bf.apply_plan(db, plan)
    db.commit()
    again = bf.build_plan(db)
    orgs = set(prod_like["orgs"].values())
    assert [r for r in again.org_settings if r[0] in orgs] == []
    assert [s for s in again.shifts if s[0] in orgs] == []
    assert [r for r in again.user_settings if r[0] in orgs] == []


def test_org_less_user_is_skipped_not_guessed(db, prod_like):
    plan = bf.build_plan(db)
    assert any("no organization_id" in s for s in plan.skipped) or \
        all(r[0] is not None for r in plan.user_settings)


# ══════════════════════════════════════════════════════════════════════════════
# Equivalence — the point of the phase
# ══════════════════════════════════════════════════════════════════════════════

def test_expected_hours_unchanged_for_every_user_after_backfill(db, prod_like):
    """The single most important migration outcome: the Early Clock Out gate
    must compare against exactly the number it compared against before."""
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    for u in _users_in(db, prod_like):
        if u.organization_id is None:
            continue
        assert bf.resolver_view(db, u, MONDAY)["expected"] == _legacy_expected_for(u), \
            f"user {u.id} expected hours changed"


def test_absent_cutoff_unchanged_for_every_user_after_backfill(db, prod_like):
    """The removed +6h part-time rule must survive as configuration."""
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    for u in _users_in(db, prod_like):
        if u.organization_id is None:
            continue
        assert bf.resolver_view(db, u, MONDAY)["absent_hr"] == _legacy_absent_hour_for(u), \
            f"user {u.id} absence cutoff changed"


def test_part_time_cutoff_is_now_an_addressable_setting(db, prod_like):
    """The point of the migration: what used to be unreachable code is now a row
    an admin can change. Deleting it returns that person to the org default."""
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    u = next(x for x in _users_in(db, prod_like)
             if (x.employment_type or "") == "part_time")
    assert bf.resolver_view(db, u, MONDAY)["absent_hr"] == 18
    db.query(models.AppSettings).filter(
        models.AppSettings.user_id == u.id,
        models.AppSettings.key == K.SETTING_ABSENT_AFTER_HOUR,
    ).delete(synchronize_session=False)
    db.commit()
    assert bf.resolver_view(db, u, MONDAY)["absent_hr"] == 12


def test_part_timers_keep_four_hours_after_backfill(db, prod_like):
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    part = [u for u in _users_in(db, prod_like) if u.employment_type == "part_time"]
    assert len(part) == 11
    for u in part:
        assert bf.resolver_view(db, u, MONDAY)["expected"] == LEGACY_EXPECTED_PART_TIME


def test_grace_is_20_everywhere_after_backfill(db, prod_like):
    """Rule 6. The system default is 15, so without the org rows this would
    silently narrow every org's grace window."""
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    for u in _users_in(db, prod_like):
        if u.organization_id is None:
            continue
        assert bf.resolver_view(db, u, MONDAY)["grace"] == 20, f"user {u.id}"


def test_grace_would_regress_to_15_without_the_backfill(db, prod_like):
    """Proves rule 6 is load-bearing rather than defensive: a user with no legacy
    clock-in inherits the 15 system default until the org row exists."""
    plain = [u for u in _users_in(db, prod_like)
             if u.organization_id and not u.default_clock_in_time][0]
    assert bf.resolver_view(db, plain, MONDAY)["grace"] == 15
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    assert bf.resolver_view(db, plain, MONDAY)["grace"] == 20


def test_the_seven_configured_users_keep_their_own_start_time(db, prod_like):
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    configured = [u for u in _users_in(db, prod_like) if u.default_clock_in_time]
    assert len(configured) == 7
    for u in configured:
        rv = bf.resolver_view(db, u, MONDAY)
        assert rv["start"] == u.default_clock_in_time
        assert rv["_sources"]["start_local"] == "employee", \
            "after the backfill the employee row, not the legacy column, is the source"


def _removed_test_legacy_view_models_the_part_time_absent_cutoff(db, prod_like):
    """The legacy side must encode the part-time carve-out that production has.

    This assertion is the one that was missing. `legacy_view` reported a flat
    SHIFT_NOON_HOUR for everyone, so the equivalence run compared 12 against 12
    and declared 345/345 identical — while the real legacy path
    (`/employees/team`) had been giving part-timers until 18:00 all along. The
    resolver then shipped a noon cutoff for them and the dashboard, which
    renders arrival_status, showed every part-timer Absent from midday.

    If someone flattens this again the equivalence proof silently stops covering
    part-timers, so assert both sides explicitly rather than only that they match.
    """
    users = _users_in(db, prod_like)
    part = [u for u in users if u.employment_type == "part_time"]
    full = [u for u in users if u.employment_type != "part_time"]
    assert part and full, "fixture must contain both types for this to mean anything"

    for u in part:
        assert bf.legacy_view(db, u, MONDAY)["absent_hr"] == 18
        assert bf.resolver_view(db, u, MONDAY)["absent_hr"] == 18
    for u in full:
        assert bf.legacy_view(db, u, MONDAY)["absent_hr"] == 12
        assert bf.resolver_view(db, u, MONDAY)["absent_hr"] == 12


def test_weekend_and_holiday_handling_is_unchanged(db, prod_like):
    """Mon-Fri plus the org's own holiday calendar - the same answer the removed
    `weekday() >= 5` produced, now resolved per organization."""
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    for u in _users_in(db, prod_like):
        if u.organization_id is None:
            continue
        assert bf.resolver_view(db, u, MONDAY)["working"] is True
        assert bf.resolver_view(db, u, SATURDAY)["working"] is False
        assert bf.resolver_view(db, u, SUNDAY)["working"] is False
        assert bf.resolver_view(db, u, HOLIDAY_ALL)["working"] is False


def test_holiday_observed_by_only_some_orgs_stays_scoped(db, prod_like):
    """2026-09-04 is a working Friday for orgs 1 and 7, a holiday for 3 and 6."""
    assert HOLIDAY_SOME.weekday() == 4
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    by_org = {}
    for u in _users_in(db, prod_like):
        if u.organization_id:
            by_org.setdefault(u.organization_id,
                              bf.resolver_view(db, u, HOLIDAY_SOME)["working"])
    o = prod_like["orgs"]
    assert by_org[o["big"]] is True
    assert by_org[o["indra"]] is True
    assert by_org[o["internal"]] is False
    assert by_org[o["harish"]] is False


def test_every_field_matches_the_removed_constants(db, prod_like):
    """The migration's whole job, asserted field by field.

    Reaching this required rejecting a 09:00 default for
    ``schedule.shift_start_time``. With it, 62 of 69 users would gain a scheduled
    start and become late-classifiable. Empty means "no scheduled shift", which
    is what those users have today.
    """
    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    for u in _users_in(db, prod_like):
        if u.organization_id is None:
            continue
        rv = bf.resolver_view(db, u, MONDAY)
        assert rv["expected"] == _legacy_expected_for(u), f"user {u.id} expected"
        assert rv["absent_hr"] == _legacy_absent_hour_for(u), f"user {u.id} cutoff"
        assert rv["working"] is True, f"user {u.id} working day"
        if u.default_clock_in_time:
            assert rv["start"] == u.default_clock_in_time, f"user {u.id} start"
            assert rv["grace"] == LEGACY_GRACE, f"user {u.id} grace"
        else:
            assert rv["start"] is None, f"user {u.id} gained a scheduled start"


def test_no_shift_start_means_arrival_is_never_late(db, prod_like):
    """Why the empty default is behaviour-neutral, stated as behaviour rather
    than as a config value: with no scheduled start, a clock-in at any hour is
    Present — which is exactly how attendance_alerts treats these users today
    (it skips anyone whose default_clock_in_time is unset)."""
    from datetime import datetime, timezone
    from app.schedule_policy.classify import ArrivalStatus, classify_arrival
    from app.schedule_policy.resolver import resolve_work_schedule

    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    plain = [u for u in _users_in(db, prod_like)
             if u.organization_id and not u.default_clock_in_time][0]
    sched = resolve_work_schedule(db, plain, MONDAY)
    assert sched.start_local is None
    for hour in (6, 9, 11, 15, 20):
        at = datetime(2026, 8, 3, hour, 0, tzinfo=timezone.utc)
        assert classify_arrival(at, sched, at).status == ArrivalStatus.PRESENT


def test_configuring_an_org_start_time_then_applies_to_everyone(db, prod_like):
    """The other half of the decision: opting in must actually work. Once an org
    sets a start time, employees without their own override inherit it."""
    from datetime import time as _time
    from app.schedule_policy.resolver import resolve_work_schedule

    bf.apply_plan(db, bf.build_plan(db))
    db.commit()
    org = prod_like["orgs"]["big"]
    db.add(models.AppSettings(organization_id=org,
                              key=K.SETTING_SHIFT_START_TIME, value="09:30"))
    db.commit()
    plain = [u for u in _users_in(db, prod_like)
             if u.organization_id == org and not u.default_clock_in_time][0]
    s = resolve_work_schedule(db, plain, MONDAY)
    assert s.start_local == _time(9, 30)
    assert s.sources["start_local"] == "org"
    # A user with their own configured time keeps it.
    configured = [u for u in _users_in(db, prod_like) if u.default_clock_in_time][0]
    s2 = resolve_work_schedule(db, configured, MONDAY)
    assert s2.start_local.strftime("%H:%M") == configured.default_clock_in_time
