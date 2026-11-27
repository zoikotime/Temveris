"""Bounded + audited connectivity-gap deduction (POST /employees/network-pause).

Regression cover for the production incident where this endpoint added a raw
client-supplied interval straight onto ``ClockSession.break_seconds``:

  * **unbounded** — one call could erase a whole day. 73 sessions in 90 days
    lost up to 8h20m each; one credited 3 seconds of work for a 7-hour session.
  * **raw ``+=``** — a retrying client re-posting the same window compounded the
    deduction without limit.
  * **untraceable** — nothing recorded what was claimed or applied, so a
    deduction could not be explained or reversed.
  * **conflated with breaks** — stored in ``break_seconds``, so break analytics
    counted it, and every dashboard (which renders elapsed time labelled
    "Worked for") hid it completely.

Router functions are called directly, matching test_early_clockout.py.
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

from app.employees.router import (
    clock_in,
    clock_out,
    network_pause,
    NetworkPausePayload,
    get_day_summary,
    get_status as get_me_status,
    _OFFLINE_SINGLE_CAP_MIN,
    _OFFLINE_SESSION_CAP_MIN,
    _CAP_SINGLE,
    _CAP_SESSION,
)

IST = timezone(timedelta(hours=5, minutes=30))
SINGLE_CAP_SECS  = _OFFLINE_SINGLE_CAP_MIN * 60    # 15 min
SESSION_CAP_SECS = _OFFLINE_SESSION_CAP_MIN * 60   # 60 min

_org_seq = itertools.count(9100)


def _new_org() -> int:
    return next(_org_seq)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_user(db, org_id, emp_type="full_time"):
    u = models.User(
        name="Offline Worker",
        email=f"np_{uuid.uuid4().hex}@example.com",
        role="employee",
        is_active=True,
        organization_id=org_id,
        employment_type=emp_type,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _open_session(db, user):
    return (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id == user.id,
            models.ClockSession.clock_out_time.is_(None),
        )
        .first()
    )


def _backdate(db, session, seconds_ago):
    session.clock_in_time = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    db.commit()
    db.refresh(session)
    return session


def _pause(db, user, *, start_ago, end_ago):
    """Report an offline window [now-start_ago, now-end_ago)."""
    now = datetime.now(timezone.utc)
    return network_pause(
        payload=NetworkPausePayload(
            offline_start=now - timedelta(seconds=start_ago),
            offline_end=now - timedelta(seconds=end_ago),
        ),
        db=db,
        user=user,
    )


def _intervals(db, session_id):
    return (
        db.query(models.OfflineInterval)
        .filter(models.OfflineInterval.session_id == session_id)
        .all()
    )


# ══════════════════════════════════════════════════════════════════════════════
# BOUND — a single window can never erase a day
# ══════════════════════════════════════════════════════════════════════════════
def test_oversized_window_is_capped_not_trusted(db):
    """The incident case: an 8-hour "outage" reported against a 9-hour session."""
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 9 * 3600)

    res = _pause(db, user, start_ago=8 * 3600, end_ago=0)

    assert res["success"] is True
    assert res["capped"] is True
    assert res["cap_reason"] == _CAP_SINGLE
    assert res["claimed_seconds"] >= 8 * 3600      # what the client asked for
    assert res["seconds"] == SINGLE_CAP_SECS       # what was actually applied
    db.refresh(session)
    assert session.offline_seconds == SINGLE_CAP_SECS
    # The pre-fix bug: offline time must NOT land in break_seconds.
    assert (session.break_seconds or 0) == 0


def test_window_within_cap_is_applied_in_full(db):
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 3600)

    res = _pause(db, user, start_ago=600, end_ago=300)   # 5 min

    assert res["capped"] is False
    assert res["seconds"] == 300
    db.refresh(session)
    assert session.offline_seconds == 300


def test_session_total_is_capped_across_many_windows(db):
    """Distinct non-overlapping windows still cannot exceed the session cap."""
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 12 * 3600)

    # 8 separate 15-min windows = 2h claimed, above the 1h session cap.
    for i in range(8):
        base = (i + 1) * 3600
        _pause(db, user, start_ago=base, end_ago=base - SINGLE_CAP_SECS)

    db.refresh(session)
    assert session.offline_seconds == SESSION_CAP_SECS


# ══════════════════════════════════════════════════════════════════════════════
# IDEMPOTENCY — a retrying client cannot inflate the deduction
# ══════════════════════════════════════════════════════════════════════════════
def test_duplicate_post_does_not_double_deduct(db):
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 3600)

    now = datetime.now(timezone.utc)
    payload = NetworkPausePayload(
        offline_start=now - timedelta(seconds=600),
        offline_end=now - timedelta(seconds=300),
    )
    network_pause(payload=payload, db=db, user=user)
    first = (db.refresh(session), session.offline_seconds)[1]

    # Same window, three more times — this is what a retry storm looks like.
    for _ in range(3):
        network_pause(payload=payload, db=db, user=user)

    db.refresh(session)
    assert session.offline_seconds == first == 300
    # Every attempt is still audited, even though only one was counted.
    assert len(_intervals(db, session.id)) == 4


def test_overlapping_windows_are_unioned_not_summed(db):
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 3600)

    # [900s ago, 600s ago) and [750s ago, 450s ago) overlap by 150s.
    # Sum would be 600s; the union is 450s.
    _pause(db, user, start_ago=900, end_ago=600)
    _pause(db, user, start_ago=750, end_ago=450)

    db.refresh(session)
    assert session.offline_seconds == 450


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT — every deduction is explainable
# ══════════════════════════════════════════════════════════════════════════════
def test_deduction_is_audited_with_claimed_and_applied(db):
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 9 * 3600)

    _pause(db, user, start_ago=4 * 3600, end_ago=0)

    rows = _intervals(db, session.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user.id
    assert row.organization_id == org
    assert row.claimed_seconds >= 4 * 3600         # the full client claim
    assert row.applied_seconds == SINGLE_CAP_SECS  # what was honoured
    assert row.capped is True
    assert row.cap_reason == _CAP_SINGLE
    assert row.offline_end > row.offline_start


def test_window_is_clamped_to_the_session(db):
    """A window starting before clock-in cannot subtract un-clocked time."""
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 300)   # clocked in 5 min ago

    _pause(db, user, start_ago=3600, end_ago=0)            # claims a full hour

    db.refresh(session)
    assert session.offline_seconds <= 300
    rows = _intervals(db, session.id)
    assert rows[0].offline_start >= session.clock_in_time


def test_not_clocked_in_is_rejected(db):
    org = _new_org()
    user = _mk_user(db, org)
    res = _pause(db, user, start_ago=600, end_ago=0)
    assert res["success"] is False
    assert res["seconds"] == 0


def test_inverted_window_is_rejected(db):
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    res = _pause(db, user, start_ago=300, end_ago=600)   # end before start
    assert res["success"] is False
    assert res["seconds"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# WORKED-TIME MATH — the gate and the dashboards must agree
# ══════════════════════════════════════════════════════════════════════════════
def _set(db, org_id, key, value):
    db.add(models.AppSettings(organization_id=org_id, key=key, value=value))
    db.commit()


def test_clock_out_deducts_offline_separately_from_break(db):
    org = _new_org()
    user = _mk_user(db, org)
    _set(db, org, "flexible_clock_out_days", json.dumps([]))
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 9 * 3600)
    _pause(db, user, start_ago=3600, end_ago=3600 - 600)   # 10 min offline

    sid = session.id
    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user, body=None)

    closed = db.query(models.ClockSession).filter(models.ClockSession.id == sid).first()
    assert closed.clock_out_time is not None or res["success"] is False
    assert closed.offline_seconds == 600
    assert (closed.break_seconds or 0) == 0
    # Worked time excludes the offline window.
    elapsed = int((closed.clock_out_time - closed.clock_in_time).total_seconds())
    assert closed.total_seconds == elapsed - 600


def test_offline_time_makes_a_full_day_fall_short(db, monkeypatch):
    """The exact production scenario: elapsed clears the requirement, worked
    time does not — and the prompt now explains why.

    Early Clock Out is temporarily disabled in production; this test covers the
    preserved gate, so it turns the flag back on. What it really pins — offline
    time being excluded from worked time — is unaffected by the flag.
    """
    from app.employees import router as _emp_router
    monkeypatch.setattr(_emp_router, "EARLY_CLOCK_OUT_FEATURE_ENABLED", True)
    org = _new_org()
    user = _mk_user(db, org)
    _set(db, org, "flexible_clock_out_days", json.dumps([]))
    clock_in(db=db, user=user)
    # 8h05m elapsed — above the 8h requirement on wall-clock alone.
    _backdate(db, _open_session(db, user), 8 * 3600 + 300)
    _pause(db, user, start_ago=3600, end_ago=3600 - SINGLE_CAP_SECS)   # 15 min

    res = clock_out(background_tasks=BackgroundTasks(), db=db, user=user, body=None)

    assert res["success"] is False
    assert res["code"] == "EARLY_CLOCK_OUT_REASON_REQUIRED"
    # The breakdown that lets the worker see WHY, instead of being told their
    # 8h05m dashboard number doesn't count.
    assert res["offline_seconds"] == SINGLE_CAP_SECS
    assert res["break_seconds"] == 0
    assert res["worked_seconds"] == res["required_seconds"] - res["difference_seconds"]


def test_day_summary_reports_offline_and_excludes_it_from_worked(db):
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    _backdate(db, _open_session(db, user), 3600)
    _pause(db, user, start_ago=1800, end_ago=1800 - 600)   # 10 min

    today = datetime.now(IST).date().isoformat()
    out = get_day_summary(date=today, db=db, user=user)

    assert out["offline_seconds"] == 600
    assert out["break_seconds"] == 0
    # elapsed = worked + break + offline must reconcile exactly.
    assert out["elapsed_seconds"] == out["worked_seconds"] + out["break_seconds"] + out["offline_seconds"]


def test_me_status_reports_offline_for_the_day(db):
    org = _new_org()
    user = _mk_user(db, org)
    clock_in(db=db, user=user)
    _backdate(db, _open_session(db, user), 3600)
    _pause(db, user, start_ago=1800, end_ago=1800 - 900)   # 15 min

    out = get_me_status(db=db, user=user)

    assert out["offline_today_seconds"] == 900
    assert out["break_today_seconds"] == 0


def test_caps_are_configurable_per_org(db):
    org = _new_org()
    user = _mk_user(db, org)
    _set(db, org, "disconnect_grace_minutes", "45")
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 9 * 3600)

    res = _pause(db, user, start_ago=3600, end_ago=0)   # 1h claimed

    assert res["seconds"] == 45 * 60      # honoured the org's wider grace
    db.refresh(session)
    assert session.offline_seconds == 45 * 60


def test_malformed_cap_setting_does_not_widen_the_bound(db):
    """A bad setting must fail closed — never grant an unbounded deduction."""
    org = _new_org()
    user = _mk_user(db, org)
    _set(db, org, "disconnect_grace_minutes", "not-a-number")
    clock_in(db=db, user=user)
    session = _backdate(db, _open_session(db, user), 9 * 3600)

    _pause(db, user, start_ago=4 * 3600, end_ago=0)

    db.refresh(session)
    assert session.offline_seconds == SINGLE_CAP_SECS
