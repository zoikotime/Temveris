"""Settings scope-tier isolation regression tests (P1 — Isolation A).

Two defects are covered.

**D8 — tier-blind reads.** Several org-level settings readers filtered on
``organization_id`` + ``key`` but not on the scope tier, then took
``.first()``. Row order is undefined, so a team- or employee-scope override
row could be returned as the ORGANISATION's value — applying one person's
override to everyone. Each test below writes an employee-scope (and/or
team-scope) override for a key, writes NO org-level row, and asserts the
reader still returns the documented default rather than the override.

**D9 — ``AppSettings.team_id``.** The column does not exist (the model has
``team_key``), so ``PUT /settings/update`` raised ``AttributeError``. The test
here asserts the org-level filter this endpoint uses is constructible and
selects only org-tier rows.

Readers covered: ``attendance_policy._get_setting_value`` (via
``resolve_expected_work_seconds`` — the live Early Clock Out gate),
``risk._threshold_secs`` / ``_risk_enabled``, ``employees._offline_caps``,
``breaks._get_threshold`` / ``_set_threshold``, and the two
``screenshot_interval`` tick readers.

Style follows tests/test_attendance_policy.py: direct function calls (no
auth/HTTP) and a fresh org id per test, because the session-scoped in-memory
SQLite keeps rows across tests.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import date

import pytest

from database import models
from database.database import Base

_org_seq = itertools.count(9000)


def _new_org() -> int:
    return next(_org_seq)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    """conftest creates only a subset of tables; these readers touch
    users and app_settings."""
    Base.metadata.create_all(engine)
    yield


def _mk_user(db, org_id, emp_type="full_time", team=None):
    u = models.User(
        name="Tier Worker",
        email=f"tier_{uuid.uuid4().hex}@example.com",
        role="employee",
        is_active=True,
        organization_id=org_id,
        employment_type=emp_type,
        team=team,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set(db, org_id, key, value, *, team_key=None, user_id=None):
    db.add(models.AppSettings(
        organization_id=org_id, team_key=team_key, user_id=user_id,
        key=key, value=value,
    ))
    db.commit()


# ── D8: expected work seconds (the live Early Clock Out gate) ─────────────────

def test_employee_override_does_not_leak_to_another_employee(db):
    """The exact production repro: an employee-scope expected-hours override
    must not become the value for a colleague who has no override."""
    from app.attendance_policy import (
        resolve_expected_work_seconds, DEFAULT_EXPECTED_WORK_SECONDS,
    )
    org = _new_org()
    priya = _mk_user(db, org)
    raj = _mk_user(db, org)

    # Priya alone is capped at 4h. No org-level row exists for this key.
    _set(db, org, "expected_work_durations",
         json.dumps({"full_time": "4:00"}), user_id=priya.id)

    assert resolve_expected_work_seconds(db, raj, date(2026, 8, 3)) == \
        DEFAULT_EXPECTED_WORK_SECONDS


def test_team_override_does_not_leak_to_org_level(db):
    from app.attendance_policy import (
        resolve_expected_work_seconds, DEFAULT_EXPECTED_WORK_SECONDS,
    )
    org = _new_org()
    _set(db, org, "expected_work_durations",
         json.dumps({"full_time": "2:00"}), team_key="Engineering")

    outsider = _mk_user(db, org, team="Sales")
    assert resolve_expected_work_seconds(db, outsider, date(2026, 8, 3)) == \
        DEFAULT_EXPECTED_WORK_SECONDS


def test_org_level_row_is_still_honoured(db):
    """The tier filter must not break the org-level read it is protecting."""
    from app.attendance_policy import resolve_expected_work_seconds
    org = _new_org()
    _set(db, org, "expected_work_durations", json.dumps({"full_time": "6:30"}))
    u = _mk_user(db, org)
    assert resolve_expected_work_seconds(db, u, date(2026, 8, 3)) == 6 * 3600 + 1800


def test_org_row_wins_over_an_employee_row_at_org_scope(db):
    """With both rows present the org-level reader must pick the org row,
    regardless of insertion order."""
    from app.attendance_policy import resolve_expected_work_seconds
    org = _new_org()
    u = _mk_user(db, org)
    # Employee row inserted FIRST so a tier-blind .first() would prefer it.
    _set(db, org, "expected_work_durations",
         json.dumps({"full_time": "1:00"}), user_id=u.id)
    _set(db, org, "expected_work_durations", json.dumps({"full_time": "7:00"}))
    assert resolve_expected_work_seconds(db, u, date(2026, 8, 3)) == 7 * 3600


def test_flexible_days_employee_override_does_not_leak(db):
    """is_flexible_clock_out_day reads through the same helper."""
    from app.attendance_policy import is_flexible_clock_out_day
    org = _new_org()
    u = _mk_user(db, org)
    # Employee-scope row claiming Monday is flexible.
    _set(db, org, "flexible_clock_out_days",
         json.dumps(["MONDAY"]), user_id=u.id)
    monday = date(2026, 7, 6)
    assert monday.weekday() == 0
    # Org has no row -> default Sat/Sun -> Monday is a working day.
    assert is_flexible_clock_out_day(db, org, monday) is False


# ── D8: risk engine ───────────────────────────────────────────────────────────

def test_risk_threshold_is_the_employees_expected_hours(db):
    """The risk threshold is no longer a setting of its own with its own default.
    It IS the employee's expected daily hours, so an employee-scope override is
    honoured — the opposite of the old org-tier-only read, which meant a person
    could be permitted to clock out at their own hours and then alerted against
    someone else's."""
    from app.risk.router import _threshold_secs
    from app.schedule_policy import keys as K
    org = _new_org()
    u = _mk_user(db, org)
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "8:00")
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "4:00", user_id=u.id)
    assert _threshold_secs(db, u) == 4 * 3600


def test_risk_threshold_falls_back_to_the_org_row(db):
    from app.risk.router import _threshold_secs
    from app.schedule_policy import keys as K
    org = _new_org()
    u = _mk_user(db, org)
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "8:45")
    assert _threshold_secs(db, u) == 8 * 3600 + 45 * 60


def test_risk_threshold_matches_the_clock_out_gate(db):
    """One number for "may I leave" and "are you under hours"."""
    from app.risk.router import _threshold_secs
    from app.attendance_policy import resolve_expected_work_seconds
    from app import _tz
    from app.schedule_policy import keys as K
    org = _new_org()
    u = _mk_user(db, org)
    _set(db, org, K.SETTING_EXPECTED_WORK_HOURS, "7:30")
    assert _threshold_secs(db, u) == resolve_expected_work_seconds(
        db, u, _tz.local_date(db, org))


def test_risk_enabled_honours_an_employee_override(db):
    """`risk_alert_enabled` is listed in the admin UI at every scope and
    PUT /settings/scoped stores an employee row for it, so reading it at the org
    tier only made a saved override silently inert."""
    from app.risk.router import _risk_enabled
    org = _new_org()
    u = _mk_user(db, org)
    other = _mk_user(db, org)
    _set(db, org, "risk_alert_enabled", "false", user_id=u.id)
    assert _risk_enabled(db, u) is False
    assert _risk_enabled(db, other) is True        # org default is enabled


def test_risk_enabled_team_override(db):
    from app.risk.router import _risk_enabled
    org = _new_org()
    u = _mk_user(db, org, team="NightOps")
    _set(db, org, "risk_alert_enabled", "false", team_key="NightOps")
    assert _risk_enabled(db, u) is False


# ── D8: offline caps (network-pause deduction bounds) ─────────────────────────

def test_offline_caps_ignore_employee_override(db):
    from app.employees.router import _offline_caps
    org = _new_org()
    u = _mk_user(db, org)
    baseline = _offline_caps(db, org)
    _set(db, org, "disconnect_grace_minutes", "999", user_id=u.id)
    assert _offline_caps(db, org) == baseline


# ── D8: break governance thresholds ──────────────────────────────────────────

def test_break_threshold_ignores_employee_override(db):
    from app.breaks.router import _get_threshold
    org = _new_org()
    u = _mk_user(db, org)
    _set(db, org, "break.short_allowance_minutes", "99", user_id=u.id)
    assert _get_threshold(db, "break.short_allowance_minutes", 15, org_id=org) == 15


def test_set_threshold_does_not_overwrite_an_employee_row(db):
    """_set_threshold upserts; without the tier filter it would mutate the
    employee override instead of creating the org-level row."""
    from app.breaks.router import _set_threshold
    org = _new_org()
    u = _mk_user(db, org)
    key = "break.lunch_allowance_minutes"
    _set(db, org, key, "45", user_id=u.id)

    _set_threshold(db, key, 60, org_id=org)

    emp_row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org,
        models.AppSettings.key == key,
        models.AppSettings.user_id == u.id,
    ).one()
    org_row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org,
        models.AppSettings.key == key,
        models.AppSettings.team_key.is_(None),
        models.AppSettings.user_id.is_(None),
    ).one()
    assert emp_row.value == "45"        # untouched
    assert org_row.value == "60"        # newly created


# ── D8: screenshot_interval tick readers ─────────────────────────────────────

def test_activity_log_tick_ignores_employee_override(db):
    from app.activity_log.router import (
        _get_tick_seconds, _TICK_FALLBACK, _TICK_CACHE,
    )
    org = _new_org()
    u = _mk_user(db, org)
    _TICK_CACHE.pop(org, None)
    _set(db, org, "screenshot_interval", "1800", user_id=u.id)
    assert _get_tick_seconds(db, org) == _TICK_FALLBACK
    _TICK_CACHE.pop(org, None)


# ── D9: the org-level filter is constructible and tier-correct ───────────────

def test_org_level_filter_selects_only_org_rows(db):
    """Regression for AppSettings.team_id (which does not exist). Builds the
    exact filter PUT /settings/update uses and asserts it returns the org row
    only."""
    org = _new_org()
    u = _mk_user(db, org)
    _set(db, org, "work_hours_threshold", "9:00")
    _set(db, org, "work_hours_threshold", "5:00", user_id=u.id)
    _set(db, org, "work_hours_threshold", "6:00", team_key="Engineering")

    rows = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org,
        models.AppSettings.key.in_(["work_hours_threshold"]),
        models.AppSettings.team_key.is_(None),   # org level only
        models.AppSettings.user_id.is_(None),
    ).all()
    assert [r.value for r in rows] == ["9:00"]


def test_app_settings_has_team_key_not_team_id():
    """Guards against the D9 regression reappearing."""
    cols = {c.name for c in models.AppSettings.__table__.columns}
    assert "team_key" in cols
    assert "team_id" not in cols


# ── Cross-tenant isolation still holds ───────────────────────────────────────

def test_org_a_settings_invisible_to_org_b(db):
    from app.attendance_policy import (
        resolve_expected_work_seconds, DEFAULT_EXPECTED_WORK_SECONDS,
    )
    org_a, org_b = _new_org(), _new_org()
    _set(db, org_a, "expected_work_durations", json.dumps({"full_time": "3:00"}))
    b_user = _mk_user(db, org_b)
    assert resolve_expected_work_seconds(db, b_user, date(2026, 8, 3)) == \
        DEFAULT_EXPECTED_WORK_SECONDS


# ══════════════════════════════════════════════════════════════════════════════
# P2 — Isolation B
# ══════════════════════════════════════════════════════════════════════════════
# D2  break governance read was filtered on `key` alone, with a process-global
#     cache keyed on `key` — one tenant's value served to every other tenant.
# D3  the attendance monitor's holiday check was unscoped and aborted the whole
#     tick, so one tenant's holiday silenced alerts for EVERY tenant.
# D13 POST /schedule/assign skipped shift-ownership validation when both times
#     were supplied, letting a cross-org shift_id be persisted.
# D9  (deferred from P1) active_time / tracking config readers took no org id,
#     so repairing their `team_id` crash had to wait for org threading.

def _mk_org(db, oid, name="Org"):
    if db.query(models.Organization).filter(models.Organization.id == oid).first() is None:
        db.add(models.Organization(id=oid, name=f"{name}{oid}"))
        db.commit()


def _no_schedule_email():
    """assign_shift sends a best-effort schedule email (fail-open). Stub it so the
    test does not wait on a real SMTP timeout — the notification is not under test."""
    import app.email_service as _es
    _es.send_schedule_notification = lambda *a, **k: True


# ── D2: break governance ─────────────────────────────────────────────────────

def test_break_allowance_is_org_scoped(db):
    from app.breaks import governance as gov
    gov.invalidate_settings_cache()
    org_a, org_b = _new_org(), _new_org()
    _set(db, org_a, gov.LUNCH_THRESHOLD_KEY, "90")
    # Org A's 90 must not reach Org B, which keeps the default.
    assert gov.allowance_minutes(db, gov.LUNCH_BREAK_TYPE, org_a) == 90
    assert gov.allowance_minutes(db, gov.LUNCH_BREAK_TYPE, org_b) == gov.DEFAULT_LUNCH_MINUTES


def test_break_grace_is_org_scoped(db):
    from app.breaks import governance as gov
    gov.invalidate_settings_cache()
    org_a, org_b = _new_org(), _new_org()
    _set(db, org_a, gov.SHORT_GRACE_KEY, "17")
    assert gov.grace_minutes(db, gov.SHORT_BREAK_TYPE, org_a) == 17
    assert gov.grace_minutes(db, gov.SHORT_BREAK_TYPE, org_b) == gov.DEFAULT_SHORT_GRACE_MINUTES


def test_break_governance_ignores_employee_override(db):
    """The governance reader is pinned to the org tier as well as the org."""
    from app.breaks import governance as gov
    gov.invalidate_settings_cache()
    org = _new_org()
    u = _mk_user(db, org)
    _set(db, org, gov.LUNCH_THRESHOLD_KEY, "5", user_id=u.id)
    assert gov.allowance_minutes(db, gov.LUNCH_BREAK_TYPE, org) == gov.DEFAULT_LUNCH_MINUTES


def test_settings_cache_is_keyed_by_org(db):
    """Two orgs read the same key in sequence; the second must not be served the
    first one's cached value, and re-reading the first must stay correct."""
    from app.breaks import governance as gov
    gov.invalidate_settings_cache()
    org_a, org_b = _new_org(), _new_org()
    _set(db, org_a, gov.SHORT_THRESHOLD_KEY, "42")
    _set(db, org_b, gov.SHORT_THRESHOLD_KEY, "7")
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_a) == 42
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_b) == 7
    assert gov.allowance_minutes(db, gov.SHORT_BREAK_TYPE, org_a) == 42


def test_entitlement_and_policy_version_are_org_scoped(db):
    from app.breaks import governance as gov
    gov.invalidate_settings_cache()
    org_a, org_b = _new_org(), _new_org()
    _set(db, org_a, "break.entitlement.full_time.lunch", "9")
    assert gov.entitlement_for(db, "full_time", "lunch", org_a) == 9
    assert gov.entitlement_for(db, "full_time", "lunch", org_b) != 9
    _set(db, org_a, gov.LUNCH_THRESHOLD_KEY, "77")
    assert "77m" in gov.policy_version_id(db, org_a)
    assert "77m" not in gov.policy_version_id(db, org_b)


# ── D3: holiday scoping in the attendance monitor ────────────────────────────

def test_one_orgs_holiday_does_not_reach_another(db):
    """The monitor used to ask "does ANY holiday exist today?" and abort the
    whole tick, so one tenant's holiday silenced every tenant's alerts. Holidays
    are now resolved inside each organization's own schedule."""
    from app.schedule_policy.resolver import resolve_work_schedule
    org_a, org_b = _new_org(), _new_org()
    _mk_org(db, org_a)
    _mk_org(db, org_b)
    d = date(2026, 8, 5)                       # a Wednesday
    db.add(models.Holiday(name="A-only", date=d, organization_id=org_a))
    db.commit()
    ua, ub = _mk_user(db, org_a), _mk_user(db, org_b)
    assert resolve_work_schedule(db, ua, d).is_holiday is True
    assert resolve_work_schedule(db, ua, d).is_working_day is False
    assert resolve_work_schedule(db, ub, d).is_holiday is False
    assert resolve_work_schedule(db, ub, d).is_working_day is True


def test_the_monitor_has_no_global_holiday_or_weekend_gate():
    """Guard against the global short-circuits coming back."""
    import ast
    import inspect
    from app import attendance_alerts
    src = inspect.getsource(attendance_alerts)
    # Strip the module docstring — it NAMES the removed constants on purpose,
    # to record why they went. We are checking the code, not the prose.
    tree = ast.parse(src)
    body = tree.body[1:] if (tree.body and isinstance(tree.body[0], ast.Expr)
                             and isinstance(tree.body[0].value, ast.Constant)) else tree.body
    code = "\n".join(ast.unparse(node) for node in body)
    for banned in ("SHIFT_NOON_HOUR", "DEFAULT_GRACE_MIN", "_is_weekend_ist",
                   "weekday() >= 5", "minutes=30"):
        assert banned not in code, f"{banned} is back in the attendance monitor"


# ── D13: cross-org shift_id on assignment ────────────────────────────────────

def test_assign_shift_rejects_cross_org_shift_even_with_both_times(db):
    """The old guard was `if shift_id and (not start or not end)`, so supplying
    both times skipped ownership validation entirely."""
    from fastapi import HTTPException
    from app.schedule.router import assign_shift, AssignIn
    _no_schedule_email()
    org_a, org_b = _new_org(), _new_org()
    _mk_org(db, org_a)
    _mk_org(db, org_b)
    admin_a = _mk_user(db, org_a)
    admin_a.role = "admin"
    worker_a = _mk_user(db, org_a)
    foreign = models.Shift(organization_id=org_b, name="B shift",
                           start_time="06:00", end_time="14:00")
    db.add(foreign)
    db.commit()
    db.refresh(foreign)

    body = AssignIn(user_id=worker_a.id, shift_id=foreign.id, date="2026-08-10",
                    start_time="09:00", end_time="18:00")
    with pytest.raises(HTTPException) as ei:
        assign_shift(body, db=db, current_user=admin_a)
    assert ei.value.status_code == 404
    assert db.query(models.ShiftAssignment).filter(
        models.ShiftAssignment.shift_id == foreign.id).count() == 0


def test_assign_shift_accepts_own_org_shift(db):
    from app.schedule.router import assign_shift, AssignIn
    _no_schedule_email()
    org = _new_org()
    _mk_org(db, org)
    admin = _mk_user(db, org)
    admin.role = "admin"
    worker = _mk_user(db, org)
    own = models.Shift(organization_id=org, name="Own", start_time="09:00", end_time="18:00")
    db.add(own)
    db.commit()
    db.refresh(own)
    out = assign_shift(
        AssignIn(user_id=worker.id, shift_id=own.id, date="2026-08-11"),
        db=db, current_user=admin)
    row = db.query(models.ShiftAssignment).filter(
        models.ShiftAssignment.id == out["id"]).one()
    assert row.organization_id == org
    assert row.start_time == "09:00" and row.end_time == "18:00"   # inherited from shift


# ── D9 (deferred from P1): active_time + tracking config readers ─────────────

def test_load_config_is_org_scoped_and_now_applies(db):
    """Two things at once: the org's stored tuning is finally honoured (the
    team_id crash made this silently return defaults for every tenant), and it
    does not leak to another org."""
    from app.active_time.config import load_config, ScoringConfig
    org_a, org_b = _new_org(), _new_org()
    _set(db, org_a, "active_time.weight_keyboard", "0.99")
    assert load_config(db, org_a).w_keyboard == 0.99
    assert load_config(db, org_b).w_keyboard == ScoringConfig().w_keyboard


def test_load_config_ignores_employee_override(db):
    from app.active_time.config import load_config, ScoringConfig
    org = _new_org()
    u = _mk_user(db, org)
    _set(db, org, "active_time.daily_cap", "12", user_id=u.id)
    assert load_config(db, org).daily_cap == ScoringConfig().daily_cap


def test_load_config_without_org_returns_defaults(db):
    """Omitting org_id must fall back to defaults, never to another tenant."""
    from app.active_time.config import load_config, ScoringConfig
    org = _new_org()
    _set(db, org, "active_time.weight_mouse", "0.77")
    assert load_config(db).w_mouse == ScoringConfig().w_mouse


def test_screenshot_interval_and_poll_period_are_org_scoped(db):
    from app.tracking.router import (
        _get_configured_screenshot_interval, _get_active_poll_period,
        _SCREENSHOT_INTERVAL_CACHE, _ACTIVE_POLL_PERIOD_CACHE,
    )
    org_a, org_b = _new_org(), _new_org()
    _SCREENSHOT_INTERVAL_CACHE.clear()
    _ACTIVE_POLL_PERIOD_CACHE.clear()
    _set(db, org_a, "screenshot_interval", "900")
    _set(db, org_a, "active_time.active_poll_period_seconds", "5")
    assert _get_configured_screenshot_interval(db, org_a) == 900
    assert _get_configured_screenshot_interval(db, org_b) == 30     # default
    assert _get_active_poll_period(db, org_a) == 5
    assert _get_active_poll_period(db, org_b) == 3                  # default
    _SCREENSHOT_INTERVAL_CACHE.clear()
    _ACTIVE_POLL_PERIOD_CACHE.clear()


def test_no_appsettings_team_id_references_remain():
    """D9 guard: the attribute does not exist, so any reference raises at query
    build time and is swallowed by the surrounding except — a silent fallback."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"AppSettings\.team_id\b", line) and "``" not in line
    ]
    assert not offenders, f"AppSettings.team_id still referenced: {offenders}"


# ── D10: provisioning stamps organization_id ─────────────────────────────────

def test_provisioning_seeds_carry_organization_id():
    """Org provisioning must stamp organization_id on its two seeded
    AppSettings rows, not only suffix the key.

    Without the column these rows land in the shared NULL-organization bucket,
    where any org-less read can pick them up — the exposure repaired by
    migration c4a7e2b915d3.

    The seeding code now lives in app/org/provisioning_service.py, which both
    the super-admin portal and self-serve signup call, so this source check
    follows it there. The behavioural equivalent (asserting on the rows that
    actually land in the database, for both provisioning paths) is
    tests/test_super_admin_provisioning_characterization.py and
    tests/test_self_service_tenant_provisioning.py.
    """
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "org" / "provisioning_service.py").read_text(encoding="utf-8")
    for key in ("shared_pool_enabled", "leave_waiting_period_months"):
        m = re.search(r"models\.AppSettings\(([^)]*?" + key + r"[^)]*?)\)", src, re.S)
        assert m, f"seed for {key} not found"
        assert "organization_id" in m.group(1), f"{key} seed is missing organization_id"
