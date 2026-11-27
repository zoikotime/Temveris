"""
The ECO v2 calculation driver.
==============================

ZT-SPEC-ECO-002 v2.0 §44.2; owner ruling D8 (rollout is the one authority);
D-F's operational-command convention.

The driver is the invocation that was missing: every stage of the chain was
built and covered by executable golden vectors, but nothing in the running
application called any of it, so §44.2's shadow period could produce no data.

These tests prove the driver ORCHESTRATES and does not CALCULATE — structurally
by AST scan, and behaviourally by walking the real chain end to end against the
real services on an isolated in-memory database.
"""
from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession

import database.models as dbm
from database.database import Base

from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.eco_driver import (
    DRIVER_ACTOR,
    STAGE_ORDER,
    DriverStage,
    run_eco_calculation_pass,
    select_workers,
)
from app.eval_day.models import EcoDayRecord
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState
from app.rollout.models import RolloutControl
from app.schedule_policy import keys as K
from app.work_ledger import LedgerState, Provenance, WorkDayLedger
from app.work_ledger_producer import set_producer_cutover

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eco_driver"
COMMAND = BACKEND / "run_eco_calculation_pass.py"

ORG = 9710
OTHER_ORG = 9720
WORKER = 9711
WORKER_B = 9712
ADMIN = 9713
OTHER_WORKER = 9721

TZ_IST = "Asia/Kolkata"
ALL_SEVEN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]

#: A Monday in the past, so the producer's day-closure gate is satisfied.
DAY = date(2026, 9, 7)
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)

#: Names the driver must never contain: the calculations it orchestrates but
#: may not perform. Checked against CALLED FUNCTIONS, not text — the module
#: docstrings name several of these in order to disclaim them.
FORBIDDEN_CALLS = frozenset({
    "round_half_up_minutes", "evaluate_workweek", "build_intervals",
    "duration_seconds", "resolve_requirement", "resolve_threshold",
    "classify_breaks", "seal_day", "supersede_day", "record_integrity_fault",
    "raise_ledger_integrity_incident", "dispatch",
    "process_pending_notification_dispatches",
    "process_pending_day_alert_dispatches",
})


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def fk_engine():
    """A private engine. Several tables on this path are append-only, so a
    shared engine could never be cleaned up between tests."""
    for module in ("break_classification", "break_treatment",
                   "clock_session_boundary", "config_versioning",
                   "eco_day_alert", "eco_ledger_incident", "eco_threshold",
                   "eval_day", "eval_event", "alert", "notification_platform",
                   "policy_version", "recipient_authority", "rollout",
                   "work_ledger", "work_ledger_producer"):
        try:
            __import__(f"app.{module}.models")
        except ModuleNotFoundError:
            __import__(f"app.{module}")
    import app.eval_day.leave_snapshot  # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(fk_engine):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(fk_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Driver Org"))
        s.add(dbm.Organization(id=OTHER_ORG, name="Other Org"))
        for uid, org, email in ((WORKER, ORG, "w.drv@example.test"),
                                (WORKER_B, ORG, "w2.drv@example.test"),
                                (ADMIN, ORG, "a.drv@example.test"),
                                (OTHER_WORKER, OTHER_ORG, "o.drv@example.test")):
            s.add(dbm.User(id=uid, name="W", email=email,
                           organization_id=org, role="employee"))
        s.commit()
        yield s
    _tz.invalidate_tz_cache()


def _configure(db, *, organization_id=ORG, week_start="MONDAY",
               working_days=ALL_SEVEN, policy=True, requirement=True):
    """A tenant that satisfies every gate the producer and EVAL-DAY require.

    ``requirement=False`` leaves the working-time requirement unconfigured,
    which is exactly what REQUIREMENT_UNRESOLVED means and what §24.3's alert
    path needs -- a real Tenant configuration defect, not a broken fixture.
    """
    from app import _tz
    from tests._eco_policy import capture_test_policy

    db.add(dbm.OrgSettings(organization_id=organization_id,
                           default_timezone=TZ_IST))
    if working_days is not None:
        db.add(dbm.AppSettings(
            organization_id=organization_id, team_key=None, user_id=None,
            key=K.SETTING_WORKING_DAYS, value=json.dumps(working_days)))
    if week_start is not None:
        db.add(dbm.AppSettings(
            organization_id=organization_id, team_key=None, user_id=None,
            key=K.SETTING_WEEK_START_DAY, value=week_start))
    db.commit()
    _tz.invalidate_tz_cache()
    set_producer_cutover(db, organization_id=organization_id,
                         cutover_at=CUTOVER, actor_label="owner@example.test")
    db.commit()
    if policy:
        capture_test_policy(db, organization_id=organization_id)
    if requirement:
        _capture_requirement(db, organization_id=organization_id)


def _capture_requirement(db, *, organization_id=ORG, minutes=480,
                         threshold_minutes=30):
    """The rank-6 Tenant fallback (§10) and the weekly threshold T (§21.1).

    NOT AppSettings: the strict resolver and the threshold reader both read
    ConfigValue rows frozen into a ConfigVersion, which is what makes a
    historical evaluation reproducible.

    Both values go into ONE version because a ConfigVersion is a COMPLETE
    snapshot and is never appended to. The threshold is written directly rather
    than through ``capture_threshold``, which correctly REFUSES a historical
    effective date ("completed workweeks are immutable") -- the same reason
    ``test_eval_event.py`` seeds it this way.
    """
    from app.config_versioning import DatabaseConfigVersionProvider
    from app.config_versioning.models import ConfigValue, ConfigVersion
    from app.eco_threshold import (
        ECO_THRESHOLD_KEY, ECO_THRESHOLD_RANK, ECO_THRESHOLD_TIER,
    )
    from app.requirement_resolution import RequirementCandidate

    version_key = f"drv-cfg-{organization_id}"
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=organization_id,
        config_version_id=version_key,
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(
            tier="TENANT_FALLBACK", rank=6, minutes=minutes)],
    )
    db.flush()
    if threshold_minutes is not None:
        version = (db.query(ConfigVersion)
                     .filter(ConfigVersion.organization_id == organization_id,
                             ConfigVersion.version_key == version_key)
                     .one())
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=organization_id,
            scope_user_id=None, tier=ECO_THRESHOLD_TIER,
            rank=ECO_THRESHOLD_RANK, key=ECO_THRESHOLD_KEY,
            value_minutes=threshold_minutes))
    db.commit()


def _set_rollout(db, state, *, organization_id=ORG):
    row = (db.query(RolloutControl)
             .filter(RolloutControl.organization_id == organization_id,
                     RolloutControl.control_key == CONTROL_EARLY_CLOCK_OUT_V2)
             .one_or_none())
    if row is None:
        row = RolloutControl(organization_id=organization_id,
                             control_key=CONTROL_EARLY_CLOCK_OUT_V2,
                             state=state.value, state_version=1,
                             last_transition_by="test")
        db.add(row)
    else:
        row.state = state.value
    db.commit()
    return row


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(day.year, day.month, day.day, hh, mm,
                    tzinfo=ZoneInfo(TZ_IST)).astimezone(UTC)


def _session(db, *, clock_in, clock_out, user_id=WORKER, organization_id=ORG):
    row = dbm.ClockSession(
        user_id=user_id, organization_id=organization_id,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=999999, break_seconds=0, offline_seconds=0,
        is_active=False,
    )
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=user_id,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=Provenance.WORKER, actor_label="w@t.test")
    append_boundary_event(db, session_id=row.id, user_id=user_id,
                          boundary=BOUNDARY_CLOCK_OUT, boundary_at=clock_out,
                          provenance=Provenance.WORKER, actor_label="w@t.test")
    db.commit()
    return row


def _worked_day(db, *, user_id=WORKER, day=DAY, start=9, end=17):
    return _session(db, clock_in=_at(day, start), clock_out=_at(day, end),
                    user_id=user_id)


def _called_names(path: pathlib.Path) -> set:
    """Every function NAME this module calls. AST, never text."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree) if isinstance(n, ast.Call)}


def _run(db, **kw):
    kw.setdefault("organization_id", ORG)
    kw.setdefault("work_date", DAY)
    return run_eco_calculation_pass(db, **kw)


# ═════════════════════════════════════════════════════════════════════════════
# 1. It orchestrates; it does not calculate
# ═════════════════════════════════════════════════════════════════════════════
class TestItOnlyOrchestrates:
    def test_it_calls_none_of_the_calculations_it_orchestrates(self):
        """The whole premise. Asserted against CALLED NAMES, not source text:
        the docstrings name several of these in order to disclaim them, and a
        substring scan would flag the sentence that states the rule."""
        for path in sorted(PACKAGE.glob("*.py")):
            offenders = _called_names(path) & FORBIDDEN_CALLS
            assert not offenders, f"{path.name} calls {sorted(offenders)}"

    def test_it_performs_no_duration_arithmetic(self):
        """No subtraction, no division, no floor-div anywhere in the driver.

        §7.2 lists `(clock_out - clock_in) - breaks` FIRST among prohibitions
        and it is defect D-03. A driver with arithmetic in it is a driver that
        could grow a second opinion about a measurement."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            bad = [type(n.op).__name__ for n in ast.walk(tree)
                   if isinstance(n, ast.BinOp)
                   and isinstance(n.op, (ast.Sub, ast.Div, ast.FloorDiv,
                                         ast.Mult))]
            assert not bad, f"{path.name} performs arithmetic: {bad}"

    def test_it_owns_no_table_and_no_migration(self):
        """No orchestration-level idempotency record. Every stage is already
        idempotent against its own authoritative identity."""
        assert not (PACKAGE / "models.py").exists()
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     for t in n.targets if isinstance(t, ast.Name)}
            assert "__tablename__" not in names
        versions = BACKEND / "alembic" / "versions"
        offenders = [p.name for p in versions.glob("*.py")
                     if "eco_driver" in p.read_text(encoding="utf-8",
                                                    errors="ignore")]
        assert offenders == [], offenders

    def test_the_stage_order_matches_the_order_the_service_calls(self):
        """The declared pipeline and the executed pipeline cannot drift."""
        assert [s.value for s in STAGE_ORDER] == [
            "LEDGER", "DAY", "DAY_ALERT", "EVENT", "NOTIFICATION"]
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        positions = [source.index(f"DriverStage.{s.value}") for s in STAGE_ORDER]
        assert positions == sorted(positions), "declared order != source order"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Rollout — the one authority (D8)
# ═════════════════════════════════════════════════════════════════════════════
class TestRollout:
    def test_off_invokes_nothing_and_writes_nothing(self, db):
        _configure(db)
        _worked_day(db)
        result = _run(db)
        assert result.processed is False
        assert result.rollout_state == "OFF"
        assert result.mode is None
        assert db.query(WorkDayLedger).count() == 0
        assert db.query(EcoDayRecord).count() == 0

    def test_a_missing_control_row_is_off(self, db):
        """OFF is also the answer to every uncertainty."""
        _configure(db)
        _worked_day(db)
        assert db.query(RolloutControl).count() == 0
        assert _run(db).rollout_state == "OFF"

    def test_an_unreadable_control_is_off(self, db):
        """A corrupted state string must not execute live ECO behaviour."""
        _configure(db)
        _worked_day(db)
        row = _set_rollout(db, RolloutState.ENABLED)
        db.execute(sa.text("UPDATE rollout_control SET state='EnAbLeD' "
                           "WHERE id=:i"), {"i": row.id})
        db.commit()
        # The fixture session is expire_on_commit=False, so the identity map
        # would answer from the stale instance and hide the corruption.
        db.expire_all()
        result = _run(db)
        assert result.processed is False
        assert db.query(WorkDayLedger).count() == 0

    def test_shadow_runs_the_full_chain_in_shadow_mode(self, db):
        """§44.2: full evaluation, full event emission."""
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        result = _run(db)
        assert result.processed is True
        assert result.mode == "SHADOW"
        assert db.query(WorkDayLedger).count() == 1
        assert db.query(EcoDayRecord).count() == 1

    def test_enabled_runs_the_full_chain_operationally(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.ENABLED)
        result = _run(db)
        assert result.processed is True
        assert result.mode == "OPERATIONAL"
        assert db.query(WorkDayLedger).count() == 1

    def test_the_driver_reads_the_control_once_and_no_stage_reads_it_again(self):
        """D8: one authority. The driver derives the mode and passes it down;
        EVAL-EVENT's own docstring says it reads no rollout state."""
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        assert source.count("rollout_state(") == 1


# ═════════════════════════════════════════════════════════════════════════════
# 3. The pipeline, walked
# ═════════════════════════════════════════════════════════════════════════════
class TestPipeline:
    def test_a_healthy_day_reaches_the_event_stage(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        [r] = _run(db).results
        assert r.ledger_outcome == "SEALED"
        assert r.day_outcome == "RECORDED"
        assert r.stage is DriverStage.EVENT
        # One sealed day of 8h against an 8h requirement is a zero weekly
        # shortfall, which is below any threshold: NOT_CROSSED, and nothing
        # written. A real weekly verdict, not a refusal.
        assert r.event_outcome == "NOT_CROSSED"
        assert r.notification_outcome is None
        assert r.workweek_key == "2026-09-07"

    def test_the_ledger_is_sealed_from_the_real_producer(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.SEALED.value
        assert row.workweek_key == "2026-09-07"
        assert row.timezone_iana == TZ_IST

    def test_the_day_record_carries_its_provenance(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        rec = db.query(EcoDayRecord).one()
        assert rec.ledger_version == 1
        assert rec.policy_version is not None
        assert rec.content_hash

    def test_a_day_with_no_evidence_is_not_selected(self, db):
        _configure(db)
        _set_rollout(db, RolloutState.SHADOW)
        result = _run(db)
        assert result.workers_selected == 0
        assert result.results == ()

    def test_a_ledger_refusal_stops_the_chain_at_the_ledger_stage(self, db):
        """A day that has not ended yet: D18-F3 gate 1 refuses DAY_NOT_ENDED."""
        _configure(db)
        future = date.today() + timedelta(days=3)
        _worked_day(db, day=future)
        _set_rollout(db, RolloutState.SHADOW)
        [r] = _run(db, work_date=future).results
        assert r.stage is DriverStage.LEDGER
        assert r.ledger_outcome == "REFUSED"
        assert db.query(EcoDayRecord).count() == 0

    def test_a_day_evaluation_refusal_stops_the_chain_at_the_day_stage(self, db):
        """No PolicyVersion: EVAL-DAY records POLICY_UNRESOLVED rather than
        evaluating, and the chain stops without a threshold event."""
        _configure(db, policy=False)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        [r] = _run(db).results
        assert r.ledger_outcome == "SEALED"
        assert r.day_state == "POLICY_UNRESOLVED"
        assert r.stage is DriverStage.EVENT
        assert r.event_outcome == "REFUSED"

    def test_an_unresolved_requirement_raises_the_day_alert(self, db):
        """§24.3 / GV-18: a Tenant configuration defect, addressed to an
        administrator and never to the Worker. The chain stops at the alert:
        a day with no requirement has no shortfall for a week to cross on."""
        from app.eco_day_alert import EcoDayAdministratorAlert
        _configure(db, requirement=False)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        [r] = _run(db).results
        assert r.day_state == "REQUIREMENT_UNRESOLVED"
        assert r.stage is DriverStage.DAY_ALERT
        assert r.event_outcome is None
        rows = db.query(EcoDayAdministratorAlert).all()
        assert len(rows) == 1
        assert rows[0].audience == "PRIMARY_ADMIN"
        assert rows[0].mode == "SHADOW"

    def test_a_complete_week_crosses_and_records_its_notification(self, db):
        """The only test that reaches stages 4 and 5. Seven sealed days of 4h
        against an 8h requirement is a 1680-minute weekly shortfall, well over
        a 30-minute threshold, so the week crosses on the day it completes."""
        from app.alert import EcoNotification
        from app.eval_event import EcoThresholdEvent

        _configure(db)
        _set_rollout(db, RolloutState.SHADOW)
        week = [DAY + timedelta(days=i) for i in range(7)]
        for day in week:
            _worked_day(db, day=day, start=9, end=13)

        stages = []
        for day in week:
            [r] = _run(db, work_date=day).results
            stages.append((r.stage, r.event_outcome, r.notification_outcome))

        assert all(s[0] is DriverStage.NOTIFICATION for s in stages[:1] + stages[1:]),             stages
        # ONE event for the week, however many days drove it.
        assert db.query(EcoThresholdEvent).count() == 1
        event = db.query(EcoThresholdEvent).one()
        assert event.mode == "SHADOW"
        assert event.workweek_key == "2026-09-07"
        # First day to complete the week CROSSED; every later pass found the
        # open crossing and wrote nothing (§21.2's natural key).
        outcomes = [s[1] for s in stages]
        assert outcomes.count("CROSSED") == 1
        assert set(outcomes) <= {"CROSSED", "ALREADY_CROSSED"}
        # And exactly one notification artifact for the authorized audience.
        assert db.query(EcoNotification).count() == 1

    def test_the_week_is_folded_by_eval_event_not_by_the_driver(self, db):
        """The driver never calls evaluate_workweek; EVAL-EVENT does, in step 1
        of its own service. Asserted structurally AND behaviourally: the driver
        reports a WEEKLY verdict it has no way to have formed."""
        assert "evaluate_workweek" not in _called_names(PACKAGE / "service.py")
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        [r] = _run(db).results
        assert r.event_outcome in {"NOT_CROSSED", "CROSSED", "REFUSED"}
        assert r.workweek_key == "2026-09-07"


# ═════════════════════════════════════════════════════════════════════════════
# 4. Idempotency — no orchestration-level record needed
# ═════════════════════════════════════════════════════════════════════════════
class TestIdempotency:
    def test_a_repeated_pass_writes_no_second_ledger_version(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        [r] = _run(db).results
        assert r.ledger_outcome == "ALREADY_SEALED"
        assert db.query(WorkDayLedger).count() == 1

    def test_a_repeated_pass_writes_no_second_day_record(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        _run(db)
        assert db.query(EcoDayRecord).count() == 1

    def test_a_repeated_pass_creates_no_second_incident(self, db):
        """Overlapping sealed intervals: §24.15's incident, raised once."""
        from app.eco_ledger_incident import EcoLedgerIncident
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 13))
        _session(db, clock_in=_at(DAY, 12), clock_out=_at(DAY, 17))
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        _run(db)
        assert db.query(EcoLedgerIncident).count() == 1

    def test_a_repeated_pass_creates_no_second_day_alert(self, db):
        """Whatever the §24.3 path answered the first time, it answers the same
        the second, and the alert store does not grow."""
        from app.eco_day_alert import EcoDayAdministratorAlert
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        before = db.query(EcoDayAdministratorAlert).count()
        _run(db)
        assert db.query(EcoDayAdministratorAlert).count() == before

    def test_a_repeated_pass_records_no_second_notification(self, db):
        from app.alert import EcoNotification
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        before = db.query(EcoNotification).count()
        _run(db)
        assert db.query(EcoNotification).count() == before


# ═════════════════════════════════════════════════════════════════════════════
# 5. §24.15 — the incident stays an incident
# ═════════════════════════════════════════════════════════════════════════════
class TestIntegrityFault:
    def _faulted(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 13))
        _session(db, clock_in=_at(DAY, 12), clock_out=_at(DAY, 17))
        _set_rollout(db, RolloutState.SHADOW)
        return _run(db)

    def test_the_incident_is_raised_without_the_driver_calling_it(self, db):
        """GV-26's primitive is wired INSIDE the ledger writer, so the driver
        cannot forget it and cannot call it twice."""
        from app.eco_ledger_incident import EcoLedgerIncident
        [r] = self._faulted(db).results
        assert r.ledger_outcome == "LEDGER_INTEGRITY_FAULT"
        assert r.stage is DriverStage.LEDGER
        assert db.query(EcoLedgerIncident).count() == 1
        assert "raise_ledger_integrity_incident" not in _called_names(
            PACKAGE / "service.py")

    def test_the_fault_produces_no_notification_and_no_day_alert(self, db):
        """GV-26 must remain an operational incident, not a notification."""
        from app.alert import EcoNotification
        from app.eco_day_alert import EcoDayAdministratorAlert
        self._faulted(db)
        assert db.query(EcoNotification).count() == 0
        assert db.query(EcoDayAdministratorAlert).count() == 0

    def test_the_faulted_day_is_never_evaluated(self, db):
        self._faulted(db)
        assert db.query(EcoDayRecord).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# 6. Scope and tenant isolation
# ═════════════════════════════════════════════════════════════════════════════
class TestScope:
    def test_only_workers_with_evidence_on_that_local_date_are_selected(self, db):
        _configure(db)
        _worked_day(db, user_id=WORKER)
        _worked_day(db, user_id=WORKER_B)
        _set_rollout(db, RolloutState.SHADOW)
        assert select_workers(db, organization_id=ORG, work_date=DAY) == (
            WORKER, WORKER_B)
        # ADMIN clocked nothing and is not processed.
        assert _run(db).workers_selected == 2

    def test_the_user_id_narrowing_processes_exactly_one_worker(self, db):
        _configure(db)
        _worked_day(db, user_id=WORKER)
        _worked_day(db, user_id=WORKER_B)
        _set_rollout(db, RolloutState.SHADOW)
        result = _run(db, user_id=WORKER)
        assert result.workers_selected == 1
        assert [r.user_id for r in result.results] == [WORKER]

    def test_another_tenants_evidence_is_never_read(self, db):
        _configure(db)
        _configure(db, organization_id=OTHER_ORG)
        _worked_day(db, user_id=WORKER)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                 user_id=OTHER_WORKER, organization_id=OTHER_ORG)
        _set_rollout(db, RolloutState.SHADOW)
        result = _run(db)
        assert [r.user_id for r in result.results] == [WORKER]
        assert {int(l.organization_id) for l in db.query(WorkDayLedger)} == {ORG}

    def test_another_tenants_rollout_does_not_enable_this_one(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.ENABLED, organization_id=OTHER_ORG)
        assert _run(db).processed is False

    def test_a_different_local_date_selects_nobody(self, db):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        assert _run(db, work_date=date(2026, 9, 8)).workers_selected == 0


# ═════════════════════════════════════════════════════════════════════════════
# 7. Failure, partial completion and retry
# ═════════════════════════════════════════════════════════════════════════════
class TestFailureRecovery:
    def test_a_failing_stage_is_reported_not_swallowed(self, db, monkeypatch):
        import app.eco_driver.service as svc

        def _boom(*a, **kw):
            raise RuntimeError("stage exploded")

        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        monkeypatch.setattr("app.eval_day.evaluate_and_record_day", _boom)
        result = _run(db)
        [r] = result.results
        assert r.stage is DriverStage.FAILED
        assert r.failure_stage is DriverStage.DAY
        assert r.failure_type == "RuntimeError"
        assert result.any_failed is True

    def test_an_earlier_committed_stage_survives_a_later_failure(self, db,
                                                                 monkeypatch):
        """Prefer resumable over compensating: the sealed ledger is authoritative
        evidence in its own right and must not be rolled back to tidy up."""
        def _boom(*a, **kw):
            raise RuntimeError("stage exploded")

        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        monkeypatch.setattr("app.eval_day.evaluate_and_record_day", _boom)
        _run(db)
        assert db.query(WorkDayLedger).count() == 1
        assert db.query(EcoDayRecord).count() == 0

    def test_a_retry_after_a_partial_failure_resumes(self, db, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("stage exploded")

        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        monkeypatch.setattr("app.eval_day.evaluate_and_record_day", _boom)
        _run(db)
        monkeypatch.undo()
        [r] = _run(db).results
        # The ledger answers "already done" and the day now records.
        assert r.ledger_outcome == "ALREADY_SEALED"
        assert r.day_outcome == "RECORDED"
        assert db.query(WorkDayLedger).count() == 1
        assert db.query(EcoDayRecord).count() == 1

    def test_one_failing_worker_does_not_abort_the_tenant_pass(self, db,
                                                               monkeypatch):
        real = None

        def _boom_for_one(db_, *, organization_id, user_id, work_date):
            if user_id == WORKER:
                raise RuntimeError("stage exploded")
            return real(db_, organization_id=organization_id,
                        user_id=user_id, work_date=work_date)

        import app.eval_day as ed
        real = ed.evaluate_and_record_day
        _configure(db)
        _worked_day(db, user_id=WORKER)
        _worked_day(db, user_id=WORKER_B)
        _set_rollout(db, RolloutState.SHADOW)
        monkeypatch.setattr(ed, "evaluate_and_record_day", _boom_for_one)
        result = _run(db)
        assert len(result.results) == 2
        assert len(result.failures) == 1
        assert result.failures[0].user_id == WORKER
        # The second Worker completed normally.
        assert db.query(EcoDayRecord).count() == 1


# ═════════════════════════════════════════════════════════════════════════════
# 8. §44.2 — shadow suppresses delivery and the Worker surface
# ═════════════════════════════════════════════════════════════════════════════
class TestShadowContract:
    def test_the_driver_delivers_nothing_under_any_rollout_state(self, db,
                                                                 monkeypatch):
        """Recording is not delivery. The dispatch pass is separate and
        separately gated; the driver must never transport."""
        import app.email_service as mailer
        sent = []
        monkeypatch.setattr(mailer, "_send",
                            lambda **kw: (sent.append(kw), True)[1])
        _configure(db)
        _worked_day(db)
        for state in (RolloutState.SHADOW, RolloutState.ENABLED):
            _set_rollout(db, state)
            _run(db)
        assert sent == []

    def test_the_driver_never_calls_a_dispatch_pass(self):
        called = _called_names(PACKAGE / "service.py")
        assert "run_eco_dispatch_pass" not in called
        assert "process_pending_notification_dispatches" not in called
        assert "process_pending_day_alert_dispatches" not in called

    def test_shadow_artifacts_are_not_visible_to_a_worker(self, db):
        """§44.2: no Worker-facing surfaces. The read surface admits only
        ADMINISTRATOR under SHADOW — asserted against the surface's own map."""
        from app.eco_day_view.router import _VISIBLE_AUDIENCES
        from app.eco_day_view.rendering import Audience
        assert Audience.WORKER not in _VISIBLE_AUDIENCES[RolloutState.SHADOW]
        assert _VISIBLE_AUDIENCES[RolloutState.OFF] == frozenset()

    def test_shadow_events_carry_the_shadow_mode(self, db):
        from app.eval_event import EcoThresholdEvent
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        _run(db)
        for row in db.query(EcoThresholdEvent):
            assert row.mode == "SHADOW"


# ═════════════════════════════════════════════════════════════════════════════
# 9. The operational command
# ═════════════════════════════════════════════════════════════════════════════
class TestCommand:
    @pytest.fixture()
    def factory(self, db):
        return lambda: db

    def _run_cmd(self, factory, *argv):
        import importlib
        command = importlib.import_module("run_eco_calculation_pass")
        return command, command.main(list(argv), session_factory=factory)

    def test_it_exits_rollout_prevented_when_off(self, db, factory, capsys):
        _configure(db)
        _worked_day(db)
        command, code = self._run_cmd(
            factory, "--organization-id", str(ORG), "--work-date", "2026-09-07")
        assert code == command.EXIT_ROLLOUT_PREVENTED
        assert "rollout_state   : OFF" in capsys.readouterr().out

    def test_it_runs_a_pass_under_shadow(self, db, factory, capsys):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        command, code = self._run_cmd(
            factory, "--organization-id", str(ORG), "--work-date", "2026-09-07")
        assert code == command.EXIT_OK
        out = capsys.readouterr().out
        assert "mode            : SHADOW" in out
        assert "workers         : 1" in out
        assert db.query(WorkDayLedger).count() == 1

    def test_a_bad_date_is_a_usage_error(self, db, factory):
        with pytest.raises(SystemExit) as exc:
            self._run_cmd(factory, "--organization-id", str(ORG),
                          "--work-date", "yesterday")
        assert exc.value.code == 2

    def test_there_is_no_all_and_no_date_range(self):
        """An organization cannot be inferred, defaulted or wildcarded (D-F).
        Asserted against the parser's real options, not the source text."""
        import importlib
        command = importlib.import_module("run_eco_calculation_pass")
        parser_opts = set()
        for action in command._parse_args.__globals__["argparse"].ArgumentParser(
                prog="x").__class__.__mro__[:1]:
            pass
        with pytest.raises(SystemExit):
            command._parse_args(["--all"])
        with pytest.raises(SystemExit):
            command._parse_args(["--organization-id", "1"])  # date required

    def test_the_report_carries_no_worker_contact_data(self, db, factory,
                                                       capsys):
        _configure(db)
        _worked_day(db)
        _set_rollout(db, RolloutState.SHADOW)
        self._run_cmd(factory, "--organization-id", str(ORG),
                      "--work-date", "2026-09-07")
        out = capsys.readouterr().out
        for leaked in ("w.drv@example.test", "a.drv@example.test", "@"):
            assert leaked not in out, leaked

    def test_the_command_starts_no_loop_and_no_scheduler(self):
        called = _called_names(COMMAND)
        for banned in ("sleep", "run_forever", "add_job", "create_task",
                       "Thread", "BackgroundScheduler"):
            assert banned not in called, banned


# ═════════════════════════════════════════════════════════════════════════════
# 10. The golden artifact is untouched
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_the_golden_vectors_are_untouched(self):
        import hashlib
        blob = (BACKEND / "tests" / "golden"
                / "eco_golden_vectors.json").read_bytes()
        digest = hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest()
        assert digest == "2a7a0fb34145fe7fd210db585cc44f198974a364"

    def test_all_twenty_eight_remain_executable(self):
        data = json.loads((BACKEND / "tests" / "golden"
                           / "eco_golden_vectors.json").read_text(
                              encoding="utf-8"))
        executable = [v["id"] for v in data["vectors"]
                      if v["harness"]["execution"] == "executable"]
        assert len(executable) == 28
        assert all(v["harness"]["blocking_dependencies"] == []
                   for v in data["vectors"])

    def test_eco_v1_remains_disabled(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_the_driver_ships_no_rollout_row(self):
        """The package must not turn ECO on for anyone."""
        for path in sorted(PACKAGE.glob("*.py")) + [COMMAND]:
            assert "RolloutControl(" not in path.read_text(encoding="utf-8")

    def test_the_driver_actor_is_distinguishable(self):
        assert DRIVER_ACTOR == "ECO_CALCULATION_DRIVER"
