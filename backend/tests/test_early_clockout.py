"""Controlled Clock-Out (Phase 2) — Early Clock Out gate tests.

Exercises the reason gate inside POST /employees/clock-out by calling the
router functions directly (no auth/HTTP), matching test_attendance_lifecycle.

"Today" for the gate is the real IST date, so flexible-day behaviour is tested
deterministically by configuring flexible_clock_out_days to include (or exclude)
today's weekday rather than by mocking the clock. Saturday/Sunday defaults are
covered at the resolver level in test_attendance_policy.py.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import BackgroundTasks

from database import models
from database.database import Base

from fastapi import HTTPException

from app.employees import router as _emp_router
from app.employees.router import (
    clock_in,
    clock_out,
    ClockOutBody,
    early_clock_outs,
)
from app.email_service import send_early_clockout_alert

FULL_TIME_SECS = 8 * 3600             # 28800 (full-time default, changed from 8h45m)
PART_TIME_SECS = 4 * 3600             # 14400
IST = timezone(timedelta(hours=5, minutes=30))
_WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

_org_seq = itertools.count(7000)


def _new_org() -> int:
    return next(_org_seq)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _feature_enabled(monkeypatch):
    """The Early Clock Out feature is TEMPORARILY DISABLED in production
    (`EARLY_CLOCK_OUT_FEATURE_ENABLED = False` in app/employees/router.py).

    The implementation is preserved for a future re-enable, so this module keeps
    covering it by switching the flag back on for the duration of each test.
    `test_feature_flag_off_clocks_out_normally` re-disables it to pin the
    shipped behaviour. Delete this fixture when the feature is re-enabled.
    """
    monkeypatch.setattr(_emp_router, "EARLY_CLOCK_OUT_FEATURE_ENABLED", True)


def _mk_user(db, org_id, emp_type="full_time"):
    u = models.User(
        name="Clockout Worker",
        email=f"co_{uuid.uuid4().hex}@example.com",
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


def _make_today_flexible(db, org_id):
    today_name = _WEEKDAYS[datetime.now(IST).date().weekday()]
    _set(db, org_id, "flexible_clock_out_days", json.dumps([today_name]))


def _make_today_non_flexible(db, org_id):
    # Empty list = no flexible days (org works every day).
    _set(db, org_id, "flexible_clock_out_days", json.dumps([]))


def _open_session(db, user):
    return (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id == user.id,
            models.ClockSession.clock_out_time.is_(None),
        )
        .first()
    )


def _backdate_clock_in(db, session, seconds_ago):
    session.clock_in_time = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    db.commit()


def _audit_rows(db, org_id):
    return (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.organization_id == org_id,
            models.AuditLog.action == "EarlyClockOut",
        )
        .all()
    )


# ── Flexible day → normal clock out, no gate ─────────────────────────────────
def test_flexible_day_clocks_out_even_with_zero_hours(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_flexible(db, org)
    clock_in(db=db, user=user)
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is True
    assert res.get("early_clock_out") is False
    assert _open_session(db, user) is None            # session closed
    assert _audit_rows(db, org) == []                 # no early-clockout audit


# ── Feature flag OFF (the shipped default) → no gate at all ──────────────────
def test_feature_flag_off_clocks_out_normally(db, monkeypatch):
    """Early Clock Out is temporarily disabled: a worker who is well below the
    required hours on a NON-flexible day still clocks out normally — no refusal
    code, no reason, no audit row (and therefore no notification email)."""
    monkeypatch.setattr(_emp_router, "EARLY_CLOCK_OUT_FEATURE_ENABLED", False)
    org = _new_org()
    user = _mk_user(db, org, "part_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    _backdate_clock_in(db, _open_session(db, user), 2 * 3600)  # 2h < 4h required
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is True
    assert "code" not in res
    assert res.get("early_clock_out") is False
    assert _open_session(db, user) is None            # session actually closed
    assert _audit_rows(db, org) == []                 # no EarlyClockOut audit


# ── Non-flexible + insufficient → reason required ────────────────────────────
def test_insufficient_hours_requires_reason(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)  # ~0s worked
    session = _open_session(db, user)
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is False
    assert res["code"] == "EARLY_CLOCK_OUT_REASON_REQUIRED"
    assert res["required_seconds"] == FULL_TIME_SECS
    assert res["attendance_id"] == session.id
    assert res["difference_seconds"] == res["required_seconds"] - res["worked_seconds"]
    # Session must remain OPEN — we did not clock out.
    assert _open_session(db, user) is not None


# ── Non-flexible + sufficient → normal clock out ─────────────────────────────
def test_sufficient_hours_clocks_out_normally(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    _backdate_clock_in(db, _open_session(db, user), FULL_TIME_SECS + 600)  # >8h
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is True
    assert res.get("early_clock_out") is False
    assert _open_session(db, user) is None
    assert _audit_rows(db, org) == []


def test_part_time_requires_only_four_hours(db):
    org = _new_org()
    user = _mk_user(db, org, "part_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    # 5h worked — below full-time 8h45m but above part-time 4h → must clock out.
    _backdate_clock_in(db, _open_session(db, user), 5 * 3600)
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is True
    assert res.get("early_clock_out") is False


def test_day_total_across_sessions_counts_toward_required(db):
    # A prior completed session (6h) + the current one (3h) together exceed
    # 8h45m, even though neither alone does. Clock-out must succeed WITHOUT a
    # reason — the gate compares the DAY total, not just the current session.
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    now = datetime.now(timezone.utc)
    db.add(models.ClockSession(
        user_id=user.id, organization_id=org,
        clock_in_time=now, clock_out_time=now,
        total_seconds=6 * 3600, break_seconds=0,
    ))
    db.commit()
    clock_in(db=db, user=user)                        # current open session
    _backdate_clock_in(db, _open_session(db, user), 3 * 3600)  # ~3h worked now
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is True                     # 6h + 3h = 9h ≥ 8h45m
    assert res.get("early_clock_out") is False


def test_part_time_below_four_hours_requires_reason(db):
    org = _new_org()
    user = _mk_user(db, org, "part_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    _backdate_clock_in(db, _open_session(db, user), 2 * 3600)  # 2h < 4h
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user)
    assert res["success"] is False
    assert res["code"] == "EARLY_CLOCK_OUT_REASON_REQUIRED"
    assert res["required_seconds"] == PART_TIME_SECS


# ── Reason validation ────────────────────────────────────────────────────────
def test_reason_too_short_is_rejected(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    res = clock_out(
        background_tasks=BackgroundTasks(), db=db, user=user,
        body=ClockOutBody(reason="too short"),  # 9 chars < 15
    )
    assert res["success"] is False
    assert res["code"] == "EARLY_CLOCK_OUT_REASON_REQUIRED"
    assert "15" in res.get("error", "")
    assert _open_session(db, user) is not None  # still open


def test_no_system_bypass_extra_fields_ignored(db):
    # SECURITY: a malicious caller cannot bypass the reason gate by adding a
    # bogus "system"/"system_clock_out" flag. The request model has no such
    # field and extra keys are ignored — the gate fires purely on
    # worked < required + no valid reason. (No backend bypass was introduced by
    # the regression fix, which is Electron-only.)
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    body = ClockOutBody.model_validate(
        {"reason": None, "system_clock_out": True, "system": True, "trusted": True}
    )
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user, body=body)
    assert res["success"] is False
    assert res["code"] == "EARLY_CLOCK_OUT_REASON_REQUIRED"
    assert _open_session(db, user) is not None  # session NOT closed → no bypass


def test_whitespace_only_reason_is_rejected(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    res = clock_out(
        background_tasks=BackgroundTasks(), db=db, user=user,
        body=ClockOutBody(reason="              "),
    )
    assert res["success"] is False
    assert res["code"] == "EARLY_CLOCK_OUT_REASON_REQUIRED"


def test_valid_reason_completes_audits_and_queues_email(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    session = _open_session(db, user)
    bg = BackgroundTasks()
    reason = "Leaving early for a medical appointment this afternoon."
    res = clock_out(background_tasks=bg, db=db, user=user, body=ClockOutBody(reason=reason))
    assert res["success"] is True
    assert res["early_clock_out"] is True
    assert _open_session(db, user) is None  # session closed

    rows = _audit_rows(db, org)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["reason"] == reason
    assert payload["attendance_id"] == session.id
    assert payload["required_seconds"] == FULL_TIME_SECS
    assert payload["difference_seconds"] == payload["required_seconds"] - payload["worked_seconds"]

    # Email was queued (not sent) as a background task.
    assert any(t.func is send_early_clockout_alert for t in bg.tasks)


def test_reason_is_truncated_at_max_length(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    res = clock_out(
        background_tasks=BackgroundTasks(), db=db, user=user,
        body=ClockOutBody(reason="x" * 1500),
    )
    assert res["success"] is True
    rows = _audit_rows(db, org)
    assert len(rows) == 1
    assert len(rows[0].payload["reason"]) == 1000


# ── Email recipient resolution + tenant isolation ────────────────────────────
# Recipient resolution moved to app/notifications/recipients.py, which every
# alert now shares. The behaviour asserted here is stricter than before: there
# is NO global fallback address, because falling back to a platform inbox is how
# one tenant's employee records reached another party.
def test_recipient_uses_org_hr_email(db):
    from app.notifications.recipients import alert_recipients
    org = _new_org()
    db.add(models.OrgSettings(organization_id=org, hr_email="hr-team@acme.test"))
    db.commit()
    assert alert_recipients(db, org) == ["hr-team@acme.test"]


def test_recipient_has_no_global_fallback(db):
    """An organization with nothing configured and no admin/HR user gets an
    EMPTY list, and the caller sends nothing."""
    from app.notifications.recipients import alert_recipients
    org = _new_org()  # no OrgSettings row, no users
    assert alert_recipients(db, org) == []


def test_recipient_falls_back_to_the_orgs_own_admins(db):
    """Never to a platform address — always to someone inside the tenant."""
    from app.notifications.recipients import alert_recipients
    org = _new_org()
    db.add(models.User(name="Boss", email="boss@acme.test", role="admin",
                       is_active=True, organization_id=org))
    db.commit()
    assert alert_recipients(db, org) == ["boss@acme.test"]


def test_recipient_tenant_isolation(db):
    from app.notifications.recipients import alert_recipients
    org_a = _new_org()
    org_b = _new_org()
    db.add(models.OrgSettings(organization_id=org_a, hr_email="hr-a@acme.test"))
    db.commit()
    # org_b must NOT see org_a's configured inbox.
    assert alert_recipients(db, org_b) == []
    assert alert_recipients(db, org_a) == ["hr-a@acme.test"]


def test_recipient_setting_cascade_beats_the_profile_inbox(db):
    from app.notifications.recipients import (
        alert_recipients, SETTING_ALERT_RECIPIENTS,
    )
    org = _new_org()
    db.add(models.OrgSettings(organization_id=org, hr_email="hr@acme.test"))
    db.add(models.AppSettings(organization_id=org, key=SETTING_ALERT_RECIPIENTS,
                              value="ops@acme.test, duty@acme.test"))
    db.commit()
    assert alert_recipients(db, org) == ["ops@acme.test", "duty@acme.test"]


def test_early_clockout_audit_tenant_isolation(db):
    org_a = _new_org()
    org_b = _new_org()
    user_a = _mk_user(db, org_a, "full_time")
    _make_today_non_flexible(db, org_a)
    clock_in(db=db, user=user_a)
    clock_out(
        background_tasks=BackgroundTasks(), db=db, user=user_a,
        body=ClockOutBody(reason="Personal emergency, leaving now."),
    )
    assert len(_audit_rows(db, org_a)) == 1
    assert _audit_rows(db, org_b) == []  # org_b sees nothing


# ── Admin visibility endpoint: GET /employees/early-clock-outs ───────────────
def _mk_admin(db, org_id):
    a = models.User(
        name="Admin", email=f"adm_{uuid.uuid4().hex}@example.com",
        role="admin", is_active=True, organization_id=org_id,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _trigger_early_clockout(db, user, reason):
    _make_today_non_flexible(db, user.organization_id)
    clock_in(db=db, user=user)
    clock_out(background_tasks=BackgroundTasks(), db=db, user=user, body=ClockOutBody(reason=reason))


def test_admin_sees_early_clockout_reason(db):
    org = _new_org()
    worker = _mk_user(db, org, "full_time")
    _trigger_early_clockout(db, worker, "Doctor appointment this afternoon.")
    admin = _mk_admin(db, org)
    res = early_clock_outs(date=None, db=db, user=admin)
    assert res["by_user"][str(worker.id)]["reason"] == "Doctor appointment this afternoon."
    assert res["by_user"][str(worker.id)]["required_seconds"] == FULL_TIME_SECS
    assert len(res["items"]) == 1


def test_non_admin_cannot_read_early_clockouts(db):
    org = _new_org()
    worker = _mk_user(db, org, "full_time")
    with pytest.raises(HTTPException) as ei:
        early_clock_outs(date=None, db=db, user=worker)
    assert ei.value.status_code == 403


def test_badge_hidden_when_a_newer_session_exists(db):
    # Early clock-out first, then the worker clocks in again — a NEWER session
    # now exists, so the early clock-out is no longer their latest. The badge
    # must NOT show.
    org = _new_org()
    worker = _mk_user(db, org, "full_time")
    _trigger_early_clockout(db, worker, "Stepped out earlier, will return.")
    clock_in(db=db, user=worker)                       # newer session
    newer = _open_session(db, worker)
    newer.clock_in_time = datetime.now(timezone.utc) + timedelta(minutes=1)  # unambiguously latest
    db.commit()

    admin = _mk_admin(db, org)
    res = early_clock_outs(date=None, db=db, user=admin)
    assert str(worker.id) not in res["by_user"]   # a newer session supersedes the early one


def test_badge_shown_when_early_is_the_latest_clockout(db):
    org = _new_org()
    worker = _mk_user(db, org, "full_time")
    _trigger_early_clockout(db, worker, "Leaving early, family commitment today.")
    admin = _mk_admin(db, org)
    res = early_clock_outs(date=None, db=db, user=admin)
    assert str(worker.id) in res["by_user"]        # latest clock-out was early


def test_early_clockout_endpoint_tenant_isolation(db):
    org_a = _new_org()
    org_b = _new_org()
    worker_a = _mk_user(db, org_a, "full_time")
    _trigger_early_clockout(db, worker_a, "Personal emergency, leaving early.")
    admin_b = _mk_admin(db, org_b)
    res = early_clock_outs(date=None, db=db, user=admin_b)
    assert res["items"] == []          # org_b admin sees nothing from org_a
    assert res["by_user"] == {}


# ── No regression: a normal clock-out queues nothing and audits nothing ──────
def test_normal_clockout_has_no_side_effects(db):
    org = _new_org()
    user = _mk_user(db, org, "full_time")
    _make_today_non_flexible(db, org)
    clock_in(db=db, user=user)
    _backdate_clock_in(db, _open_session(db, user), FULL_TIME_SECS + 600)
    bg = BackgroundTasks()
    res = clock_out(background_tasks=bg, db=db, user=user)
    assert res["success"] is True
    assert res["early_clock_out"] is False
    assert "worked_seconds" in res and "clock_out_time" in res  # existing contract intact
    assert bg.tasks == []
    assert _audit_rows(db, org) == []
