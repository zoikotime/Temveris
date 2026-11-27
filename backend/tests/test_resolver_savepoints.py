"""Savepoint isolation in the Schedule Resolver.

A failing optional load (a model column the database does not have yet) must
degrade that ONE field, not abort the caller's transaction. On Postgres the
first failing statement aborts the whole transaction and every later statement
raises InFailedSqlTransaction until someone rolls back — which turned a missing
column into a request-wide outage once already. `resolver._safe_load` wraps each
optional load in a SAVEPOINT rather than calling db.rollback(), because rolling
the whole transaction back would discard uncommitted work the CALLER owns.

(The engine-gate and shadow-mode tests that used to live alongside these were
removed with the machinery they covered: the Schedule Resolver is now the only
policy path, so there is no gate to test.)
"""
import itertools
import logging
import uuid
from datetime import date

import pytest
from sqlalchemy import text

from database import models
from database.database import Base

from app.schedule_policy import keys as K
from app.schedule_policy.resolver import build_context, resolve_work_schedule

_org_seq = itertools.count(52000)
WORKDAY = date(2026, 8, 6)          # Thursday


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
    u = models.User(name="Savepoint", email=f"sp_{uuid.uuid4().hex}@x.com",
                    role="employee", is_active=True, organization_id=org_id, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ══════════════════════════════════════════════════════════════════════════
# Item 5 — savepoints
# ══════════════════════════════════════════════════════════════════════════

def _break_shift_assignments(db):
    """Reproduce production's shape: the model declares a column the DB lacks."""
    db.execute(text("ALTER TABLE shift_assignments DROP COLUMN expected_work_seconds"))
    db.commit()


def _restore_shift_assignments(db):
    db.execute(text("ALTER TABLE shift_assignments ADD COLUMN expected_work_seconds INTEGER"))
    db.commit()


def test_missing_column_does_not_poison_the_session(db):
    """The regression that matters. Before P10c the failed load left the
    transaction aborted, so the NEXT query in the same request raised
    InFailedSqlTransaction — a missing column became a request-wide outage."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _break_shift_assignments(db)
    try:
        build_context(db, org, [u], [WORKDAY])
        # The whole point: the session is still usable afterwards.
        assert db.query(models.User).filter(
            models.User.organization_id == org).count() == 1
    finally:
        _restore_shift_assignments(db)


def test_resolution_still_succeeds_with_a_missing_column(db):
    """Degrade, don't fail: the roster tier drops out, everything else resolves."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time", default_clock_in_time="09:00")
    _break_shift_assignments(db)
    try:
        sched = resolve_work_schedule(db, u, WORKDAY)
        assert sched.start_local is not None, "legacy column still applied"
        assert sched.absent_after_hour == 12
    finally:
        _restore_shift_assignments(db)


def test_the_failure_is_logged_not_swallowed(db, caplog):
    """A missing column is a deployment fault. Someone has to see it."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _break_shift_assignments(db)
    try:
        with caplog.at_level(logging.WARNING, logger="zoiko.schedule.resolver"):
            build_context(db, org, [u], [WORKDAY])
        assert any("load failed" in r.message or "load failed" in r.getMessage()
                   for r in caplog.records), "the failure must be logged"
        assert any("assignments" in r.getMessage() for r in caplog.records), \
            "the log must name which load failed"
    finally:
        _restore_shift_assignments(db)


def test_healthy_path_logs_nothing(db, caplog):
    """Noise discipline: the dashboard polls this endpoint every 30s."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    with caplog.at_level(logging.WARNING, logger="zoiko.schedule.resolver"):
        build_context(db, org, [u], [WORKDAY])
    assert [r for r in caplog.records if "load failed" in r.getMessage()] == []


def test_savepoint_preserves_the_callers_uncommitted_work(db):
    """Why a SAVEPOINT and not db.rollback(): the caller's pending work is not
    this helper's to discard. A plain rollback here would silently drop it."""
    org = _new_org(db)
    u = _mk_user(db, org, employment_type="full_time")
    _break_shift_assignments(db)
    try:
        pending = models.AppSettings(organization_id=org, key="zzz.pending",
                                     value="1")
        db.add(pending)
        db.flush()                       # caller's uncommitted work
        build_context(db, org, [u], [WORKDAY])
        assert db.query(models.AppSettings).filter(
            models.AppSettings.organization_id == org,
            models.AppSettings.key == "zzz.pending").count() == 1, \
            "the resolver discarded work it did not own"
    finally:
        db.rollback()
        _restore_shift_assignments(db)
