"""
Holidays → leave day cost.

The audit found leave was the one consumer of the holiday calendar that ignored
it: attendance, the schedule resolver, reports and payroll all resolved working
days properly, while leave charged ``max(1, (to - from).days + 1)`` — every
calendar day in the span. An employee taking Mon-Fri across Diwali was debited
five days instead of four.

These tests pin the new cost function, including every fallback, because the
failure mode that matters is leave becoming accidentally FREE.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import date, datetime, timedelta

import pytest

from database import models
from database.database import Base
from app.leave import policy as leave_policy

_org_seq = itertools.count(880000)

# July 2026 reference week, asserted below so a wrong constant fails loudly.
MON = date(2026, 7, 6)
TUE = date(2026, 7, 7)
WED = date(2026, 7, 8)
THU = date(2026, 7, 9)
FRI = date(2026, 7, 10)
SAT = date(2026, 7, 11)
SUN = date(2026, 7, 12)


def test_reference_dates_are_the_weekdays_we_think():
    assert [d.strftime("%A") for d in (MON, TUE, WED, THU, FRI, SAT, SUN)] == [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@pytest.fixture(scope="module", autouse=True)
def _tables(engine):
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def session(engine):
    from sqlalchemy.orm import sessionmaker
    S = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def org_user(session):
    """A Mon-Fri employee in an organization whose primary country is IN."""
    oid = next(_org_seq)
    session.add(models.Organization(id=oid, name=f"LeaveOrg-{oid}"))
    session.add(models.OrgSettings(organization_id=oid, primary_country="IN"))
    session.commit()
    user = models.User(
        name="Leave Taker", email=f"leave_{uuid.uuid4().hex}@x.test",
        password="x", role="employee", is_active=True,
        organization_id=oid, employment_type="full_time")
    session.add(user)
    session.commit()
    session.refresh(user)
    return session, oid, user


def _holiday(session, org_id, on_date, name="Test Holiday", country="IN"):
    session.add(models.Holiday(
        name=name, date=on_date, holiday_type="Public",
        organization_id=org_id, country_code=country))
    session.commit()


# ── The headline behaviour ────────────────────────────────────────────────────
def test_a_full_working_week_costs_five_days(org_user):
    db, _, user = org_user
    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 5.0


def test_a_holiday_inside_the_span_is_not_charged(org_user):
    """Mon-Fri with Wednesday a public holiday costs four days, not five."""
    db, org_id, user = org_user
    _holiday(db, org_id, WED, name="Diwali")
    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 4.0


def test_multiple_holidays_in_one_span(org_user):
    db, org_id, user = org_user
    _holiday(db, org_id, TUE, name="H1")
    _holiday(db, org_id, THU, name="H2")
    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 3.0


def test_weekends_are_not_charged(org_user):
    """Mon-Sun spans seven calendar days but only five working ones."""
    db, _, user = org_user
    assert leave_policy.leave_day_cost(db, user, MON, SUN) == 5.0


def test_weekend_and_holiday_together(org_user):
    db, org_id, user = org_user
    _holiday(db, org_id, FRI, name="Long Weekend Holiday")
    assert leave_policy.leave_day_cost(db, user, MON, SUN) == 4.0


def test_a_span_entirely_on_non_working_days_costs_zero(org_user):
    """Correct: an employee owes nothing for days they were not going to work."""
    db, _, user = org_user
    assert leave_policy.leave_day_cost(db, user, SAT, SUN) == 0.0


def test_single_working_day_costs_one(org_user):
    db, _, user = org_user
    assert leave_policy.leave_day_cost(db, user, WED, WED) == 1.0


def test_single_day_that_is_a_holiday_costs_zero(org_user):
    db, org_id, user = org_user
    _holiday(db, org_id, WED)
    assert leave_policy.leave_day_cost(db, user, WED, WED) == 0.0


# ── Half day ──────────────────────────────────────────────────────────────────
def test_half_day_is_always_half(org_user):
    db, org_id, user = org_user
    _holiday(db, org_id, WED)
    assert leave_policy.leave_day_cost(db, user, WED, WED, is_half_day=True) == 0.5
    assert leave_policy.leave_day_cost(db, user, MON, MON, is_half_day=True) == 0.5


# ── Country scoping ───────────────────────────────────────────────────────────
def test_another_countrys_holiday_is_still_charged(org_user):
    """The organization runs on IN. A US-scoped holiday in its calendar must not
    give its Indian staff a free day."""
    db, org_id, user = org_user
    _holiday(db, org_id, WED, name="US Independence Day", country="US")
    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 5.0


def test_an_unscoped_holiday_is_not_charged(org_user):
    """Pre-migration rows have country_code NULL and must keep applying."""
    db, org_id, user = org_user
    _holiday(db, org_id, WED, name="Legacy Holiday", country=None)
    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 4.0


def test_another_tenants_holiday_never_counts(session):
    other_oid = next(_org_seq)
    session.add(models.Organization(id=other_oid, name="Other"))
    session.add(models.OrgSettings(organization_id=other_oid, primary_country="IN"))
    session.commit()
    session.add(models.Holiday(name="Foreign", date=WED, holiday_type="Public",
                               organization_id=other_oid, country_code="IN"))
    session.commit()

    oid = next(_org_seq)
    session.add(models.Organization(id=oid, name="Mine"))
    session.add(models.OrgSettings(organization_id=oid, primary_country="IN"))
    session.commit()
    user = models.User(name="Mine", email=f"m_{uuid.uuid4().hex}@x.test", password="x",
                       role="employee", is_active=True, organization_id=oid)
    session.add(user)
    session.commit()

    assert leave_policy.leave_day_cost(session, user, MON, FRI) == 5.0


# ── The setting ───────────────────────────────────────────────────────────────
def test_the_setting_defaults_on(org_user):
    db, _, user = org_user
    assert leave_policy.excludes_non_working_days(db, user) is True


def test_turning_the_setting_off_restores_the_raw_calendar_span(org_user):
    db, org_id, user = org_user
    _holiday(db, org_id, WED)
    db.add(models.AppSettings(
        organization_id=org_id,
        key=leave_policy.SETTING_EXCLUDE_NON_WORKING, value="false"))
    db.commit()
    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 5.0
    assert leave_policy.leave_day_cost(db, user, SAT, SUN) == 2.0


# ── Fallbacks: leave must never become accidentally free ──────────────────────
def test_a_resolver_failure_falls_back_to_the_raw_span(org_user, monkeypatch):
    db, org_id, user = org_user
    _holiday(db, org_id, WED)

    import app.schedule_policy.resolver as resolver_module

    def _boom(*a, **k):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(resolver_module, "resolve_work_schedules_over", _boom)

    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 5.0


def test_partial_resolution_falls_back_to_the_raw_span(org_user, monkeypatch):
    """An incomplete answer would undercount, so it is not trusted at all."""
    db, org_id, user = org_user
    import app.schedule_policy.resolver as resolver_module
    real = resolver_module.resolve_work_schedules_over

    def _partial(db_, users, dates):
        full = real(db_, users, dates)
        return {d: v for d, v in list(full.items())[:2]}
    monkeypatch.setattr(resolver_module, "resolve_work_schedules_over", _partial)

    assert leave_policy.leave_day_cost(db, user, MON, FRI) == 5.0


def test_a_span_longer_than_a_year_uses_the_raw_span(org_user):
    db, _, user = org_user
    start = date(2026, 1, 1)
    end = start + timedelta(days=leave_policy.MAX_COSTED_LEAVE_DAYS + 5)
    assert leave_policy.leave_day_cost(db, user, start, end) == float((end - start).days + 1)


def test_reversed_range_costs_one_day(org_user):
    db, _, user = org_user
    assert leave_policy.leave_day_cost(db, user, FRI, MON) == 1.0


def test_datetime_inputs_are_accepted(org_user):
    """The apply handler parses to datetime; the validation gate passes date."""
    db, org_id, user = org_user
    _holiday(db, org_id, WED)
    as_dt = leave_policy.leave_day_cost(
        db, user, datetime(2026, 7, 6), datetime(2026, 7, 10))
    assert as_dt == leave_policy.leave_day_cost(db, user, MON, FRI) == 4.0


# ── Consistency with the rest of the platform ─────────────────────────────────
def test_leave_cost_agrees_with_the_holiday_dates_helper(org_user):
    """The endpoint and the in-process calculation are backed by the same
    function, so they cannot disagree — which was the point of keeping
    /holidays/dates rather than deleting it."""
    from app.holidays import org_calendar
    db, org_id, user = org_user
    _holiday(db, org_id, WED, name="Shared Truth")

    helper_dates = org_calendar.holiday_dates(db, org_id, start=MON, end=FRI)
    assert WED in helper_dates

    span_days = (FRI - MON).days + 1
    assert leave_policy.leave_day_cost(db, user, MON, FRI) == span_days - len(helper_dates)


def test_leave_cost_matches_the_schedule_resolver(org_user):
    """Leave now uses the same engine as attendance and payroll, so the two
    answers must line up exactly."""
    from app.schedule_policy.resolver import resolve_work_schedules_over
    db, org_id, user = org_user
    _holiday(db, org_id, THU, name="Cross-check")

    days = [MON + timedelta(days=i) for i in range((FRI - MON).days + 1)]
    by_date = resolve_work_schedules_over(db, [user], days)
    working = sum(1 for d in days if by_date[d][user.id].is_working_day)

    assert leave_policy.leave_day_cost(db, user, MON, FRI) == float(working)
