"""Attendance policy resolver tests (Controlled Clock-Out — Phase 1).

Covers the two pure, multi-tenant resolvers in app.attendance_policy:

  * resolve_expected_work_seconds — precedence: shift → employment-type policy
    → work_hours_threshold → global default, plus malformed-config fallbacks
    and tenant isolation.
  * is_flexible_clock_out_day — weekend defaults, org custom days, malformed
    JSON fallback, holiday integration, and tenant isolation.

These call the resolver functions directly (no auth/HTTP). Each test uses a
fresh org_id so the session-scoped in-memory SQLite (rows persist across tests)
never collides on the AppSettings (organization_id, key) unique constraint.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import date

import pytest

from database import models
from database.database import Base

from app.attendance_policy import (
    resolve_expected_work_seconds,
    is_flexible_clock_out_day,
    parse_hhmm_to_seconds,
    normalize_employment_type,
    default_expected_seconds_for_type,
    DEFAULT_EXPECTED_WORK_SECONDS,
)

# 8h45m and 4h00m in seconds (part-time required worked time is 4:00)
FULL_TIME_SECS = 8 * 3600             # 28800 (full-time default, changed from 8h45m)
PART_TIME_SECS = 4 * 3600             # 14400

# Known reference dates (guarded by asserts inside tests so a wrong constant
# fails loudly rather than silently).
MONDAY = date(2026, 7, 6)
SATURDAY = date(2026, 7, 4)
SUNDAY = date(2026, 7, 5)
FRIDAY = date(2026, 7, 3)

_org_seq = itertools.count(5000)


def _new_org() -> int:
    return next(_org_seq)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    """conftest creates only a subset of tables; these resolvers touch
    users, app_settings, shifts, shift_assignments and holidays."""
    Base.metadata.create_all(engine)
    yield


def _mk_user(db, org_id, emp_type="full_time"):
    u = models.User(
        name="Policy Worker",
        email=f"pol_{uuid.uuid4().hex}@example.com",
        role="employee",
        is_active=True,
        organization_id=org_id,
        employment_type=emp_type,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value):
    db.add(models.AppSettings(organization_id=org_id, key=key, value=value))
    db.commit()


def _assign_shift(db, user, org_id, on_date, start_time, end_time,
                  status="scheduled", shift_id=None):
    a = models.ShiftAssignment(
        organization_id=org_id,
        user_id=user.id,
        shift_id=shift_id,
        date=on_date.isoformat(),
        start_time=start_time,
        end_time=end_time,
        status=status,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


# ── Reference-date sanity guards ──────────────────────────────────────────────
def test_reference_dates_are_correct():
    assert MONDAY.weekday() == 0
    assert FRIDAY.weekday() == 4
    assert SATURDAY.weekday() == 5
    assert SUNDAY.weekday() == 6


# ── parse_hhmm_to_seconds ─────────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("8:45", 8 * 3600 + 45 * 60),
    ("4:00", PART_TIME_SECS),
    ("4:30", 16200),
    ("08:45", 8 * 3600 + 45 * 60),
    ("0:30", 1800),
    ("24:00", 24 * 3600),
])
def test_parse_valid_durations(value, expected):
    assert parse_hhmm_to_seconds(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "abc", "8", "8:", ":45", "8:99", "-1:00", "8:-5",
    "0:00", "25:00", "8:45:00", "8.5", "eight:forty",
])
def test_parse_invalid_durations_return_none(value):
    assert parse_hhmm_to_seconds(value) is None


# ── Employment-type normalization (aliases) ───────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("full_time", "full_time"), ("Full Time", "full_time"), ("FULL-TIME", "full_time"),
    ("fulltime", "full_time"), ("FT", "full_time"), ("full", "full_time"),
    ("part_time", "part_time"), ("Part Time", "part_time"), ("PART-TIME", "part_time"),
    ("parttime", "part_time"), ("PT", "part_time"), ("pt", "part_time"), ("part", "part_time"),
    (None, "full_time"), ("", "full_time"), ("   ", "full_time"),
    ("contractor", "contractor"),   # unknown preserved for custom org policies
])
def test_normalize_employment_type_aliases(value, expected):
    assert normalize_employment_type(value) == expected


@pytest.mark.parametrize("value,secs", [
    ("part_time", PART_TIME_SECS), ("Part Time", PART_TIME_SECS), ("pt", PART_TIME_SECS),
    ("parttime", PART_TIME_SECS),
    ("full_time", FULL_TIME_SECS), ("FT", FULL_TIME_SECS),
    ("contractor", FULL_TIME_SECS),  # unknown → safe full-time default
    (None, FULL_TIME_SECS), ("", FULL_TIME_SECS),
])
def test_default_expected_seconds_for_type(value, secs):
    assert default_expected_seconds_for_type(value) == secs


# ── Expected duration: employment-type policy defaults ────────────────────────
def test_full_time_default_policy(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    assert resolve_expected_work_seconds(db, user, MONDAY) == FULL_TIME_SECS


def test_part_time_default_policy(db):
    org = _new_org()
    user = _mk_user(db, org, "part_time")
    assert resolve_expected_work_seconds(db, user, MONDAY) == PART_TIME_SECS


# ── Required worked duration must NOT come from the raw shift window ───────────
# Confirmed business rule: shift start/end is the scheduled window (may include
# break time) and must never override the employment-type required-work policy.
def test_ten_hour_shift_window_does_not_override_full_time_policy(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    # 9:00 AM → 7:00 PM = 10h scheduled window.
    _assign_shift(db, user, org, MONDAY, "09:00", "19:00")
    # Required worked duration stays 8h45m (window is ignored).
    assert resolve_expected_work_seconds(db, user, MONDAY) == FULL_TIME_SECS


def test_shift_window_does_not_override_part_time_policy(db):
    org = _new_org()
    user = _mk_user(db, org, "part_time")
    _assign_shift(db, user, org, MONDAY, "09:00", "18:00")  # 9h scheduled window
    # Required worked duration stays 4h00m (window is ignored).
    assert resolve_expected_work_seconds(db, user, MONDAY) == PART_TIME_SECS


def test_shift_template_link_does_not_override_policy(db):
    # Even a template-linked assignment with no explicit override times must not
    # inject a raw-window duration.
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    shift = models.Shift(
        organization_id=org, name="Long Day", start_time="08:00", end_time="20:00",
    )
    db.add(shift)
    db.commit()
    db.refresh(shift)
    _assign_shift(db, user, org, MONDAY, None, None, shift_id=shift.id)  # 12h window
    assert resolve_expected_work_seconds(db, user, MONDAY) == FULL_TIME_SECS


# ── Expected duration: precedence ─────────────────────────────────────────────
def test_policy_overrides_work_hours_threshold(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _set(db, org, "work_hours_threshold", "10:00")  # would be 36000
    # full_time policy default (8:45) wins over the threshold.
    assert resolve_expected_work_seconds(db, user, MONDAY) == FULL_TIME_SECS


def test_work_hours_threshold_fallback_for_unmapped_type(db):
    org = _new_org()
    user = _mk_user(db, org, "contractor")  # not in default policy
    _set(db, org, "work_hours_threshold", "07:30")  # 27000
    assert resolve_expected_work_seconds(db, user, MONDAY) == 7 * 3600 + 30 * 60


def test_global_default_fallback(db):
    org = _new_org()
    user = _mk_user(db, org, "contractor")  # not in policy, no threshold set
    assert resolve_expected_work_seconds(db, user, MONDAY) == DEFAULT_EXPECTED_WORK_SECONDS


def test_org_custom_policy_override(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _set(db, org, "expected_work_durations", json.dumps({"full_time": "9:15"}))
    assert resolve_expected_work_seconds(db, user, MONDAY) == 9 * 3600 + 15 * 60


def test_org_policy_missing_key_keeps_default(db):
    # Org overrides only full_time; part_time should keep the 4:00 default.
    org = _new_org()
    user = _mk_user(db, org, "part_time")
    _set(db, org, "expected_work_durations", json.dumps({"full_time": "9:00"}))
    assert resolve_expected_work_seconds(db, user, MONDAY) == PART_TIME_SECS


def test_invalid_duration_value_falls_through(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _set(db, org, "expected_work_durations", json.dumps({"full_time": "25:99"}))
    # Invalid → skip policy, no threshold → global default.
    assert resolve_expected_work_seconds(db, user, MONDAY) == DEFAULT_EXPECTED_WORK_SECONDS


def test_malformed_policy_json_falls_back_to_defaults(db):
    org = _new_org()
    user = _mk_user(db, org, "part_time")
    _set(db, org, "expected_work_durations", "{not valid json")
    assert resolve_expected_work_seconds(db, user, MONDAY) == PART_TIME_SECS


def test_unknown_employment_type_uses_global_default(db):
    org = _new_org()
    user = _mk_user(db, org, "alien_type")
    assert resolve_expected_work_seconds(db, user, MONDAY) == DEFAULT_EXPECTED_WORK_SECONDS


def test_legacy_employment_type_label_is_normalized(db):
    org = _new_org()
    user = _mk_user(db, org, "Part-Time")  # legacy label
    assert resolve_expected_work_seconds(db, user, MONDAY) == PART_TIME_SECS


# ── Expected duration: tenant isolation ───────────────────────────────────────
def test_expected_duration_tenant_isolation(db):
    org_a = _new_org()
    org_b = _new_org()
    _set(db, org_a, "expected_work_durations", json.dumps({"full_time": "12:00"}))
    user_b = _mk_user(db, org_b, "full_time")
    # org_b has no override — must NOT see org_a's 12:00.
    assert resolve_expected_work_seconds(db, user_b, MONDAY) == FULL_TIME_SECS


# ── Flexible clock-out days ───────────────────────────────────────────────────
def test_saturday_flexible_by_default(db):
    org = _new_org()
    assert is_flexible_clock_out_day(db, org, SATURDAY) is True


def test_sunday_flexible_by_default(db):
    org = _new_org()
    assert is_flexible_clock_out_day(db, org, SUNDAY) is True


def test_monday_not_flexible_by_default(db):
    org = _new_org()
    assert is_flexible_clock_out_day(db, org, MONDAY) is False


def test_org_custom_flexible_days(db):
    org = _new_org()
    _set(db, org, "flexible_clock_out_days", json.dumps(["FRIDAY"]))
    assert is_flexible_clock_out_day(db, org, FRIDAY) is True
    # Saturday no longer flexible under the custom config.
    assert is_flexible_clock_out_day(db, org, SATURDAY) is False


def test_empty_flexible_days_is_honoured(db):
    org = _new_org()
    _set(db, org, "flexible_clock_out_days", json.dumps([]))
    assert is_flexible_clock_out_day(db, org, SATURDAY) is False


def test_malformed_flexible_days_falls_back_to_weekend(db):
    org = _new_org()
    _set(db, org, "flexible_clock_out_days", "not json at all")
    assert is_flexible_clock_out_day(db, org, SATURDAY) is True
    assert is_flexible_clock_out_day(db, org, MONDAY) is False


def test_holiday_makes_day_flexible(db):
    org = _new_org()
    db.add(models.Holiday(name="Test Day", date=MONDAY, organization_id=org))
    db.commit()
    assert is_flexible_clock_out_day(db, org, MONDAY) is True
    # ...but only when holiday integration is enabled.
    assert is_flexible_clock_out_day(db, org, MONDAY, include_holidays=False) is False


def test_flexible_days_tenant_isolation(db):
    org_a = _new_org()
    org_b = _new_org()
    _set(db, org_a, "flexible_clock_out_days", json.dumps(["MONDAY"]))
    # org_b keeps defaults — Monday is not flexible for it.
    assert is_flexible_clock_out_day(db, org_b, MONDAY) is False


def test_holiday_tenant_isolation(db):
    org_a = _new_org()
    org_b = _new_org()
    db.add(models.Holiday(name="A only", date=MONDAY, organization_id=org_a))
    db.commit()
    # org_b has no holiday on Monday → not flexible.
    assert is_flexible_clock_out_day(db, org_b, MONDAY) is False
