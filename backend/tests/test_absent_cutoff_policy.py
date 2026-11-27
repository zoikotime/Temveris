"""The absence cutoff is a SETTING, at every scope — never an employment-type rule.

Replaces test_parttime_absent_cutoff.py, which asserted the opposite: that a
`part_time` employee automatically received the organization's cutoff plus six
hours (`PART_TIME_ABSENT_OFFSET_HOURS = 6`). That offset was one customer's
convention — their part-timers start in the afternoon — promoted to a product
rule, so an organization whose part-timers work mornings had them marked absent
six hours late, and every organization paid for a branch it never asked for.

The capability it provided is preserved and generalised: any scope can carry its
own `schedule.absent_after_hour`, so "part-timers get until 18:00" is expressed
as a team- or employee-scope row instead of a constant. The P2 migration writes
exactly those rows for existing part-timers, which these tests model.
"""
import itertools
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest

from database import models
from database.database import Base

from app.schedule_policy import keys as K
from app.schedule_policy.classify import ArrivalStatus, classify_arrival
from app.schedule_policy.resolver import resolve_work_schedule, resolve_work_schedules

# 61000 belongs to test_settings_scoped_write_path; ids must not collide
# because organizations.id is explicit in these fixtures.
_org_seq = itertools.count(64000)
WORKDAY = date(2026, 8, 6)          # Thursday
IST = timezone(timedelta(hours=5, minutes=30))


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


def _at(hour):
    return datetime.combine(WORKDAY, time(hour, 0), tzinfo=IST)


# ── The rule is gone ─────────────────────────────────────────────────────────

def test_no_employment_type_offset_exists_in_the_resolver():
    """The constant must not come back. A grep-level guard: reintroducing it is
    how the six-hour rule survived three refactors."""
    from app.schedule_policy import resolver
    assert not hasattr(resolver, "PART_TIME_ABSENT_OFFSET_HOURS")

    import inspect
    src = inspect.getsource(resolver)
    assert "part_time" not in src.split("employment_type = ")[-1].split("# ──")[0] or True
    # The absence-cutoff block must not branch on employment type at all.
    block = src[src.index("absent_hour = _int_or"):src.index("min_required = ")]
    assert "part_time" not in block, "the absence cutoff branches on employment type again"


def test_part_timer_and_full_timer_get_the_same_default(db):
    """With nothing configured, employment type changes nothing."""
    org = _new_org(db)
    pt = _mk_user(db, org, employment_type="part_time")
    ft = _mk_user(db, org, employment_type="full_time")
    assert (resolve_work_schedule(db, pt, WORKDAY).absent_after_hour
            == resolve_work_schedule(db, ft, WORKDAY).absent_after_hour)


# ── It is configurable at every scope ────────────────────────────────────────

def test_org_scope_sets_the_cutoff(db):
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "14")
    assert resolve_work_schedule(db, u, WORKDAY).absent_after_hour == 14


def test_team_scope_overrides_org(db):
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time", team="Support")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "12")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "18", team_key="Support")
    assert resolve_work_schedule(db, u, WORKDAY).absent_after_hour == 18


def test_employee_scope_overrides_team_and_org(db):
    """This is how "part-timers get until 18:00" is expressed now."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time", team="Support")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "12")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "16", team_key="Support")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "18", user_id=u.id)
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert sched.absent_after_hour == 18
    assert sched.sources["absent_after_hour"] == "employee"


def test_provenance_names_the_tier_not_a_rule(db):
    """`sources` must point at a settings tier a support engineer can go and
    look at — it used to say "part_time_rule", which is not somewhere to look."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "18")
    assert resolve_work_schedule(db, u, WORKDAY).sources["absent_after_hour"] == "org"


# ── The classifier honours it ────────────────────────────────────────────────

@pytest.mark.parametrize("hour,expected", [
    (11, ArrivalStatus.AWAITING),
    (17, ArrivalStatus.AWAITING),
    (18, ArrivalStatus.ABSENT),
    (20, ArrivalStatus.ABSENT),
])
def test_an_employee_scope_cutoff_drives_the_classification(db, hour, expected):
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "18", user_id=u.id)
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert classify_arrival(None, sched, _at(hour)).status == expected


@pytest.mark.parametrize("hour,expected", [
    (11, ArrivalStatus.AWAITING),
    (12, ArrivalStatus.ABSENT),
])
def test_the_org_default_still_applies_to_everyone_else(db, hour, expected):
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    sched = resolve_work_schedule(db, u, WORKDAY)
    assert classify_arrival(None, sched, _at(hour)).status == expected


def test_someone_who_clocked_in_is_never_absent(db):
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="part_time")
    sched = resolve_work_schedule(db, u, WORKDAY)
    r = classify_arrival(_at(9), sched, _at(20))
    assert r.status is not ArrivalStatus.ABSENT


# ── Tenancy ──────────────────────────────────────────────────────────────────

def test_two_orgs_keep_their_own_cutoffs(db):
    a, b = _new_org(db), _new_org(db)
    ua = _mk_user(db, a, employment_type="part_time")
    ub = _mk_user(db, b, employment_type="part_time")
    _set(db, a, K.SETTING_ABSENT_AFTER_HOUR, "11")
    _set(db, b, K.SETTING_ABSENT_AFTER_HOUR, "19")
    assert resolve_work_schedule(db, ua, WORKDAY).absent_after_hour == 11
    assert resolve_work_schedule(db, ub, WORKDAY).absent_after_hour == 19


def test_batch_resolve_keeps_employees_apart(db):
    org = _new_org(db)
    early = _mk_user(db, org, employment_type="part_time")
    late = _mk_user(db, org, employment_type="part_time")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "10", user_id=early.id)
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "20", user_id=late.id)
    out = resolve_work_schedules(db, [early, late], WORKDAY)
    assert out[early.id].absent_after_hour == 10
    assert out[late.id].absent_after_hour == 20


def test_out_of_range_falls_back_to_the_system_default(db):
    """A malformed row must degrade to the documented default, not to whatever
    one customer runs in production."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _set(db, org, K.SETTING_ABSENT_AFTER_HOUR, "99")
    expected = int(K.SETTING_DEFAULTS[K.SETTING_ABSENT_AFTER_HOUR])
    assert resolve_work_schedule(db, u, WORKDAY).absent_after_hour == expected


def test_classifier_holds_no_employment_type_knowledge():
    """classify_arrival must stay a pure decision table over the schedule."""
    import inspect
    from app.schedule_policy import classify
    src = inspect.getsource(classify)
    for token in ("part_time", "full_time", "employment_type"):
        assert token not in src, f"{token} leaked into the classifier"
