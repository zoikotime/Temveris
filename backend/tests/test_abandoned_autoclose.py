"""Safe heartbeat-based auto-close tests (production-safety guardrails).

Verifies `_auto_close_abandoned_sessions`:
  * closes a genuinely silent session at its LAST HEARTBEAT (accurate hours,
    never inflated to now/midnight);
  * does NOT close sessions with a recent heartbeat, no heartbeat, or an active
    break;
  * the CIRCUIT BREAKER skips the whole sweep when a large fraction of an org's
    monitored sessions are silent at once (server/network outage), but still
    closes a few isolated silent ones (real laptop deaths);
  * stays strictly org-scoped (tenant isolation).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from database import models
from database.database import Base

from app.employees.router import _auto_close_abandoned_sessions

_org_seq = itertools.count(9000)


def _new_org() -> int:
    return next(_org_seq)


@pytest.fixture(scope="session", autouse=True)
def _all_tables(engine):
    Base.metadata.create_all(engine)
    yield


def _mk_user(db, org_id):
    u = models.User(
        name="AC Worker", email=f"ac_{uuid.uuid4().hex}@example.com",
        role="employee", is_active=True, organization_id=org_id,
        employment_type="full_time",
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _mk_session(db, user, org_id, clock_in_ago_min):
    now = datetime.now(timezone.utc)
    s = models.ClockSession(
        user_id=user.id, organization_id=org_id,
        clock_in_time=now - timedelta(minutes=clock_in_ago_min),
        clock_out_time=None, total_seconds=0, break_seconds=0, is_active=True,
    )
    db.add(s); db.commit(); db.refresh(s)
    return s


def _heartbeat(db, user, org_id, ago_min):
    now = datetime.now(timezone.utc)
    db.add(models.LiveStatus(
        user_id=user.id, organization_id=org_id,
        updated_at=now - timedelta(minutes=ago_min),
    ))
    db.commit()


def _is_open(db, sid):
    s = db.query(models.ClockSession).filter(models.ClockSession.id == sid).first()
    return s.clock_out_time is None


# ── Accurate close at last heartbeat ──────────────────────────────────────────
def test_silent_session_closed_at_last_heartbeat(db):
    org = _new_org()
    u = _mk_user(db, org)
    s = _mk_session(db, u, org, clock_in_ago_min=120)  # clocked in 2h ago
    _heartbeat(db, u, org, ago_min=20)                 # last beat 20m ago (> 15m grace)
    _auto_close_abandoned_sessions(db, org_id=org)
    db.refresh(s)
    assert s.clock_out_time is not None                # closed
    # Worked ≈ 100m (clock-in 120m ago → last beat 20m ago). NOT inflated to
    # ~120m (now) or to end-of-day.
    assert 95 * 60 <= s.total_seconds <= 105 * 60


# ── Must NOT close ────────────────────────────────────────────────────────────
def test_recent_heartbeat_not_closed(db):
    org = _new_org(); u = _mk_user(db, org)
    s = _mk_session(db, u, org, 120)
    _heartbeat(db, u, org, ago_min=5)                  # within grace
    _auto_close_abandoned_sessions(db, org_id=org)
    assert _is_open(db, s.id)


def test_no_heartbeat_not_closed(db):
    org = _new_org(); u = _mk_user(db, org)
    s = _mk_session(db, u, org, 120)                   # no LiveStatus row
    _auto_close_abandoned_sessions(db, org_id=org)
    assert _is_open(db, s.id)


def test_active_break_not_closed(db):
    org = _new_org(); u = _mk_user(db, org)
    s = _mk_session(db, u, org, 120)
    _heartbeat(db, u, org, ago_min=20)                 # silent...
    db.add(models.BreakSession(                         # ...but on an active break
        user_id=u.id, organization_id=org, session_id=s.id,
        break_start=datetime.now(timezone.utc) - timedelta(minutes=25),
        break_end=None, is_active=True,
    ))
    db.commit()
    _auto_close_abandoned_sessions(db, org_id=org)
    assert _is_open(db, s.id)


# ── Circuit breaker ───────────────────────────────────────────────────────────
def test_circuit_breaker_skips_mass_silence(db):
    org = _new_org()
    sessions = []
    for _ in range(5):                                  # 5 silent = mass outage
        u = _mk_user(db, org)
        s = _mk_session(db, u, org, 120)
        _heartbeat(db, u, org, ago_min=20)
        sessions.append(s)
    _auto_close_abandoned_sessions(db, org_id=org)
    for s in sessions:
        assert _is_open(db, s.id)                       # NONE closed


def test_few_silent_among_active_are_closed(db):
    org = _new_org()
    silent, active = [], []
    for _ in range(2):                                  # 2 silent (real deaths)
        u = _mk_user(db, org)
        s = _mk_session(db, u, org, 120)
        _heartbeat(db, u, org, ago_min=20)
        silent.append(s)
    for _ in range(8):                                  # 8 healthy
        u = _mk_user(db, org)
        s = _mk_session(db, u, org, 120)
        _heartbeat(db, u, org, ago_min=2)
        active.append(s)
    _auto_close_abandoned_sessions(db, org_id=org)
    for s in silent:
        assert not _is_open(db, s.id)                   # closed
    for s in active:
        assert _is_open(db, s.id)                       # untouched


# ── Tenant isolation ──────────────────────────────────────────────────────────
def test_tenant_isolation(db):
    org_a, org_b = _new_org(), _new_org()
    ua = _mk_user(db, org_a)
    sa = _mk_session(db, ua, org_a, 120)
    _heartbeat(db, ua, org_a, ago_min=20)
    _auto_close_abandoned_sessions(db, org_id=org_b)    # sweeping B must not touch A
    assert _is_open(db, sa.id)
    _auto_close_abandoned_sessions(db, org_id=org_a)    # sweeping A closes it
    assert not _is_open(db, sa.id)
