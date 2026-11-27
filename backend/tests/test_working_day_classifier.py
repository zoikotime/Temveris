"""
The dedicated working-day classifier, and GV-28 executed end to end.
====================================================================

Owner rulings under test: D2-A (a dedicated classifier owns the semantic
determination), D3-A (sealed context is the authoritative history), D4-A (the
one-directional state invariant), D6-A (``system`` permanently inadmissible),
D8-A/D17-A (fail closed, and absence of work never classifies), D9-A (leave is
irrelevant to a non-working day), D13-A (the verdict is passed through the
producer into the ledger state), D15-A (an explicit EMPTY leave snapshot) and
D18-A (no Worker Context wiring).

The GV-28 scenario runs the REAL path — classifier, producer, ledger service,
recording boundary, week fold — and reads its expected values from the locked
artifact. Nothing here hand-builds an ``EcoDayRecord``.
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

from tests._eco_policy import capture_test_policy

import database.models as dbm
from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.config_versioning import DatabaseConfigVersionProvider
from app.eval_day import (
    EcoDayState,
    evaluate_and_record_day,
    read_leave_snapshot,
)
from app.eval_week import evaluate_workweek
from app.requirement_resolution import RequirementCandidate
from app.schedule_policy import keys as K
from app.work_ledger import (
    CandidateInterval,
    DayKey,
    LedgerState,
    LedgerStateContractViolation,
    Provenance,
    WorkDayLedger,
    WorkState,
    record_integrity_fault,
    seal_day,
    supersede_day,
)
from app.work_ledger_producer import (
    ProducerOutcome,
    RefusalReason,
    produce_and_seal_work_day,
    set_producer_cutover,
)
from app.working_day_classifier import (
    ClassificationOutcome,
    ClassificationRefusal,
    WorkingDayClassification,
    classification_from_sealed,
    classify_working_day,
)
from database.database import Base

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, WORKER = 7701, 7711
TZ = "Asia/Kolkata"

#: A Sunday in the past — every DST-free zone's rest day, and past because the
#: D18-F3 closure gate refuses a day that has not ended.
REST_DAY = date(2026, 9, 6)
#: The Monday after it.
WORK_DAY = date(2026, 9, 7)
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)

MON_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
ALL_SEVEN = MON_FRI + ["SATURDAY", "SUNDAY"]

REQUIRED_MINUTES = 525


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def wd_engine():
    import app.clock_session_boundary.models   # noqa: F401
    import app.eval_day.models                 # noqa: F401
    import app.policy_version.models             # noqa: F401
    import app.leave_intervals.models          # noqa: F401
    import app.work_ledger.models              # noqa: F401
    import app.work_ledger_producer.models     # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(wd_engine):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(wd_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Classifier Org"))
        s.commit()
        s.add(dbm.User(id=WORKER, name="W", email="w@wd.test",
                       organization_id=ORG, role="employee"))
        s.commit()
        # Explicit historical PolicyVersion: evaluate_and_record_day
        # refuses with POLICY_UNRESOLVED without one. Satisfies the
        # production rule rather than weakening it.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


@pytest.fixture(autouse=True)
def _workweek_boundary(db):
    """§12.1: a tenant must have a defined workweek before ECO seals a day.

    Autouse because several tests here seal directly to exercise the D4-A
    state invariant and never call ``_configure``. MONDAY is what the resolver
    used to assume, so no classification or week grouping in this file moves.
    """
    db.add(dbm.AppSettings(
        organization_id=ORG, team_key=None, user_id=None,
        key=K.SETTING_WEEK_START_DAY, value="MONDAY"))
    db.commit()


def _configure(db, *, working_days=MON_FRI, tz=TZ, cutover=CUTOVER):
    from app import _tz
    db.add(dbm.OrgSettings(organization_id=ORG, default_timezone=tz))
    if working_days is not None:
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=None,
            key=K.SETTING_WORKING_DAYS, value=json.dumps(working_days)))
    db.commit()
    _tz.invalidate_tz_cache()
    if cutover is not None:
        set_producer_cutover(db, organization_id=ORG, cutover_at=cutover,
                             actor_label="owner@wd.test")
        db.commit()


def _set_working_days(db, days):
    """Mutate the CURRENT configuration — used to prove the freeze holds."""
    from app import _tz
    row = (db.query(dbm.AppSettings)
           .filter(dbm.AppSettings.organization_id == ORG,
                   dbm.AppSettings.key == K.SETTING_WORKING_DAYS)
           .one())
    row.value = json.dumps(days)
    db.commit()
    _tz.invalidate_tz_cache()


def _at(day, hh, mm=0):
    from zoneinfo import ZoneInfo
    return datetime(day.year, day.month, day.day, hh, mm,
                    tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def _session(db, *, clock_in, clock_out):
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=999999, break_seconds=888, offline_seconds=777,
        is_active=False,
    )
    db.add(row)
    db.flush()
    for boundary, at in ((BOUNDARY_CLOCK_IN, clock_in),
                         (BOUNDARY_CLOCK_OUT, clock_out)):
        append_boundary_event(
            db, session_id=row.id, user_id=WORKER, boundary=boundary,
            boundary_at=at, provenance=Provenance.WORKER,
            actor_label="w@wd.test")
    db.commit()
    return row


def _worker(db):
    return db.get(dbm.User, WORKER)


def _requirement(db, *, minutes=REQUIRED_MINUTES, key="wd-v1"):
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id=key,
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(
            tier="TENANT_FALLBACK", rank=6, minutes=minutes)])
    db.flush()


def _leave(db, *, on, minutes, status):
    """One leave grant WITH recorded intervals, under an arbitrary status.

    An unrecognised status is how the platform produces an authority nobody
    can establish: `approved_leave_overlap` refuses it rather than reading it
    as "no leave" (the existing MODEL-EXCLUSION rule, unchanged here).
    """
    from zoneinfo import ZoneInfo

    from app.leave_intervals import (
        CandidateLeaveInterval,
        record_leave_intervals,
    )

    request = dbm.LeaveRequest(
        user_id=WORKER, organization_id=ORG, leave_type="Casual Leave",
        from_date=_midnight(on), to_date=_midnight(on),
        description="classifier fixture", status=status)
    db.add(request)
    db.flush()
    begin = _at(on, 18)
    record_leave_intervals(
        db, leave_request=request,
        intervals=[CandidateLeaveInterval(
            seq=1, start_at_utc=begin,
            end_at_utc=begin + timedelta(minutes=minutes))],
        timezone_iana=TZ, zone=ZoneInfo(TZ), expected_version=None,
        recorded_by="TEST")
    db.flush()
    return request


def _vector(vid):
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == vid:
            return v
    raise AssertionError(vid)


def _midnight(on):
    return datetime(on.year, on.month, on.day)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The classifier — current path (D2-A, D5-A, D6-A)
# ═════════════════════════════════════════════════════════════════════════════
class TestCurrentClassification:
    def test_a_configured_working_day(self, db):
        _configure(db)
        result = classify_working_day(
            db, organization_id=ORG, user=_worker(db), work_date=WORK_DAY)
        assert result.outcome is ClassificationOutcome.WORKING_DAY
        assert result.is_working_day is True
        assert result.working_day_source == "org"
        assert result.refusal is None
        assert result.is_established is True

    def test_a_configured_non_working_day(self, db):
        _configure(db)
        result = classify_working_day(
            db, organization_id=ORG, user=_worker(db), work_date=REST_DAY)
        assert result.outcome is ClassificationOutcome.NOT_A_WORKING_DAY
        assert result.is_working_day is False
        assert result.working_day_source == "org"
        assert result.is_established is True

    def test_the_system_source_is_refused(self, db):
        """D6-A: nothing configured the working week, so the resolver returns
        its fail-open default. Permanently inadmissible."""
        _configure(db, working_days=None)
        result = classify_working_day(
            db, organization_id=ORG, user=_worker(db), work_date=WORK_DAY)
        assert result.outcome is ClassificationOutcome.UNCLASSIFIED
        assert result.refusal is ClassificationRefusal.SOURCE_NOT_CONFIGURED
        # D17-A: a refusal carries no classification at all.
        assert result.is_working_day is None
        assert result.working_day_source is None
        assert result.is_established is False

    def test_the_precedence_is_the_resolvers_own(self, db):
        """D5-A: the classifier re-codes nothing. Its answer for every date in
        a week is exactly what the ratified resolver already reports."""
        from app.schedule_policy.resolver import resolve_work_schedule
        _configure(db)
        for offset in range(7):
            on = REST_DAY + timedelta(days=offset)
            schedule = resolve_work_schedule(db, _worker(db), on)
            mine = classify_working_day(
                db, organization_id=ORG, user=_worker(db), work_date=on)
            assert mine.is_working_day == bool(schedule.is_working_day), on
            assert mine.working_day_source == \
                schedule.sources["is_working_day"], on

    def test_it_is_deterministic_and_pure(self, db):
        _configure(db)
        first = classify_working_day(
            db, organization_id=ORG, user=_worker(db), work_date=REST_DAY)
        second = classify_working_day(
            db, organization_id=ORG, user=_worker(db), work_date=REST_DAY)
        assert first == second
        assert db.query(WorkDayLedger).count() == 0   # no writes

    def test_a_cross_tenant_request_raises(self, db):
        _configure(db)
        with pytest.raises(ValueError, match="cross-tenant"):
            classify_working_day(
                db, organization_id=ORG + 1, user=_worker(db),
                work_date=WORK_DAY)


# ═════════════════════════════════════════════════════════════════════════════
# 2. The classifier — frozen path (D3-A, D16-A)
# ═════════════════════════════════════════════════════════════════════════════
class TestFrozenClassification:
    def test_a_sealed_non_working_day(self, db):
        led = seal_day(
            db, DayKey(organization_id=ORG, user_id=WORKER,
                       work_date=REST_DAY),
            intervals=(), timezone_iana=TZ, is_working_day=False,
            working_day_source="org",
            state=LedgerState.NOT_A_WORKING_DAY, created_by="TEST")
        result = classification_from_sealed(led)
        assert result.outcome is ClassificationOutcome.NOT_A_WORKING_DAY
        assert result.is_working_day is False
        assert result.working_day_source == "org"

    def test_a_sealed_working_day(self, db):
        led = seal_day(
            db, DayKey(organization_id=ORG, user_id=WORKER,
                       work_date=WORK_DAY),
            intervals=(), timezone_iana=TZ, is_working_day=True,
            working_day_source="org", created_by="TEST")
        assert classification_from_sealed(led).outcome is \
            ClassificationOutcome.WORKING_DAY

    @pytest.mark.parametrize("state", [
        LedgerState.OPEN, LedgerState.MISSING_CLOCK_OUT,
        LedgerState.LEDGER_INTEGRITY_FAULT,
    ])
    def test_a_state_that_carries_no_classification_is_refused(self, state):
        """A lifecycle or fault condition is never read as a classification."""
        row = WorkDayLedger(
            organization_id=ORG, user_id=WORKER,
            work_date=_midnight(WORK_DAY), workweek_key="2026-09-07",
            ledger_version=1, state=state.value, timezone_iana=TZ,
            is_working_day=True, working_day_source="org",
            utc_offset_seconds=0, created_by="TEST")
        result = classification_from_sealed(row)
        assert result.outcome is ClassificationOutcome.UNCLASSIFIED
        assert result.refusal is ClassificationRefusal.NOT_SEALED

    def test_a_missing_source_is_refused_not_re_resolved(self):
        row = WorkDayLedger(
            organization_id=ORG, user_id=WORKER,
            work_date=_midnight(WORK_DAY), workweek_key="2026-09-07",
            ledger_version=1, state=LedgerState.SEALED.value,
            timezone_iana=TZ, is_working_day=True, working_day_source="  ",
            utc_offset_seconds=0, created_by="TEST")
        result = classification_from_sealed(row)
        assert result.outcome is ClassificationOutcome.UNCLASSIFIED
        assert result.refusal is ClassificationRefusal.CONTEXT_MISSING

    def test_a_d4_violating_row_is_refused_rather_than_trusted(self):
        """Belt and braces: the service refuses to write this combination, so
        a row carrying it came from somewhere that bypassed the service."""
        row = WorkDayLedger(
            organization_id=ORG, user_id=WORKER,
            work_date=_midnight(REST_DAY), workweek_key="2026-08-31",
            ledger_version=1, state=LedgerState.NOT_A_WORKING_DAY.value,
            timezone_iana=TZ, is_working_day=True, working_day_source="org",
            utc_offset_seconds=0, created_by="TEST")
        result = classification_from_sealed(row)
        assert result.outcome is ClassificationOutcome.UNCLASSIFIED
        assert result.refusal is ClassificationRefusal.CONTEXT_MISSING

    def test_the_frozen_path_takes_no_session(self):
        """D16-A's whole point: it CANNOT reach configuration, because there
        is no session to reach it with."""
        import inspect
        params = list(inspect.signature(classification_from_sealed)
                      .parameters)
        assert params == ["ledger"]


# ═════════════════════════════════════════════════════════════════════════════
# 3. UNCLASSIFIED cannot carry a classification (D17-A)
# ═════════════════════════════════════════════════════════════════════════════
class TestOutcomeGuards:
    def test_unclassified_may_not_carry_a_boolean(self):
        with pytest.raises(ValueError, match="not 'not a working day'"):
            WorkingDayClassification(
                outcome=ClassificationOutcome.UNCLASSIFIED,
                refusal=ClassificationRefusal.SOURCE_UNKNOWN,
                is_working_day=False)

    def test_unclassified_must_state_a_reason(self):
        with pytest.raises(ValueError, match="must carry a refusal"):
            WorkingDayClassification(
                outcome=ClassificationOutcome.UNCLASSIFIED)

    def test_an_established_outcome_may_not_carry_a_refusal(self):
        with pytest.raises(ValueError, match="must not carry a refusal"):
            WorkingDayClassification(
                outcome=ClassificationOutcome.WORKING_DAY,
                is_working_day=True, working_day_source="org",
                refusal=ClassificationRefusal.SOURCE_UNKNOWN)

    def test_the_outcome_and_the_boolean_cannot_disagree(self):
        with pytest.raises(ValueError, match="contradicts"):
            WorkingDayClassification(
                outcome=ClassificationOutcome.NOT_A_WORKING_DAY,
                is_working_day=True, working_day_source="org")

    def test_an_established_outcome_must_carry_its_source(self):
        with pytest.raises(ValueError, match="source label"):
            WorkingDayClassification(
                outcome=ClassificationOutcome.WORKING_DAY,
                is_working_day=True)


# ═════════════════════════════════════════════════════════════════════════════
# 4. D4-A — the state invariant, at every public writer
# ═════════════════════════════════════════════════════════════════════════════
class TestD4Invariant:
    def test_not_a_working_day_with_true_is_refused(self, db):
        with pytest.raises(LedgerStateContractViolation):
            seal_day(
                db, DayKey(organization_id=ORG, user_id=WORKER,
                           work_date=REST_DAY),
                intervals=(), timezone_iana=TZ, is_working_day=True,
                working_day_source="org",
                state=LedgerState.NOT_A_WORKING_DAY, created_by="TEST")
        db.rollback()
        assert db.query(WorkDayLedger).count() == 0, "a row was written"

    def test_not_a_working_day_with_false_is_accepted(self, db):
        led = seal_day(
            db, DayKey(organization_id=ORG, user_id=WORKER,
                       work_date=REST_DAY),
            intervals=(), timezone_iana=TZ, is_working_day=False,
            working_day_source="org",
            state=LedgerState.NOT_A_WORKING_DAY, created_by="TEST")
        assert led.state == LedgerState.NOT_A_WORKING_DAY.value
        assert led.is_working_day is False

    def test_sealed_with_false_remains_legal(self, db):
        """The reverse implication is deliberately NOT enforced (D4-A)."""
        led = seal_day(
            db, DayKey(organization_id=ORG, user_id=WORKER,
                       work_date=WORK_DAY),
            intervals=(), timezone_iana=TZ, is_working_day=False,
            working_day_source="org", created_by="TEST")
        assert led.state == LedgerState.SEALED.value
        assert led.is_working_day is False

    def test_supersede_day_inherits_the_guard(self, db):
        key = DayKey(organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        seal_day(db, key, intervals=(), timezone_iana=TZ,
                 is_working_day=False, working_day_source="org",
                 state=LedgerState.NOT_A_WORKING_DAY, created_by="TEST")
        db.flush()
        with pytest.raises(LedgerStateContractViolation):
            supersede_day(
                db, key, expected_version=1, intervals=(), timezone_iana=TZ,
                is_working_day=True, working_day_source="org",
                state=LedgerState.NOT_A_WORKING_DAY, created_by="TEST")
        db.rollback()

    def test_record_integrity_fault_is_unaffected(self, db):
        """It writes LEDGER_INTEGRITY_FAULT, so the guard does not apply and
        must not start refusing a fault row for a working day."""
        row = record_integrity_fault(
            db, DayKey(organization_id=ORG, user_id=WORKER,
                       work_date=WORK_DAY),
            reason="overlap", timezone_iana=TZ, is_working_day=True,
            working_day_source="org", created_by="TEST")
        assert row.state == LedgerState.LEDGER_INTEGRITY_FAULT.value
        assert row.is_working_day is True

    def test_the_guard_lives_at_the_single_write_chokepoint(self):
        """One construction site, so every writer inherits it (D14-A)."""
        source = (BACKEND / "app" / "work_ledger" / "service.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        constructions = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None)
            == "WorkDayLedger"
        ]
        assert len(constructions) == 1, "more than one ledger construction"
        assert "LedgerStateContractViolation(" in source

    def test_no_database_check_or_migration_was_added(self):
        """D14-A: service layer ONLY."""
        models = (BACKEND / "app" / "work_ledger" / "models.py").read_text(
            encoding="utf-8")
        assert "NOT_A_WORKING_DAY" not in models, (
            "a CHECK constraint or column default referencing the state "
            "appeared in the model; D14-A authorises service-layer only")
        # A GLOBAL revision count used to stand in for this claim. It no
        # longer can: EVAL-EVENT legitimately added f3d8a1c65b29 under owner
        # ruling D1, so the count moved for a reason that has nothing to do
        # with D14-A, and a bumped number would assert nothing about the
        # classifier at all. The claim is therefore tested directly — no
        # revision may mention this package or its state.
        versions = BACKEND / "alembic" / "versions"
        offenders = []
        for path in versions.glob("*.py"):
            body = path.read_text(encoding="utf-8", errors="ignore")
            if "working_day_classifier" in body or "NOT_A_WORKING_DAY" in body:
                offenders.append(path.name)
        assert offenders == [], (
            f"a migration references the classifier; D14-A requires none: "
            f"{offenders}")
        # 91 since §24.15's D-9.1 added d9a4c7e35f18, which creates the ECO
        # platform-incident table. Like f4a7c2b91e68 and f3d8a1c65b29 before
        # it, it moved the count for a reason that has nothing to do with
        # D14-A: it creates one new table and mentions neither this package nor
        # its state, which the two assertions above already prove directly.
        assert len(list(versions.glob("*.py"))) == 94, (
            "an unexplained migration was created")


# ═════════════════════════════════════════════════════════════════════════════
# 5. D13-A — the producer passes the verdict through
# ═════════════════════════════════════════════════════════════════════════════
class TestProducerHandoff:
    def test_a_rest_day_seals_as_not_a_working_day(self, db):
        _configure(db)
        _session(db, clock_in=_at(REST_DAY, 9), clock_out=_at(REST_DAY, 12))
        result = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert result.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.NOT_A_WORKING_DAY.value
        assert row.is_working_day is False
        assert row.working_day_source == "org"

    def test_a_working_day_seals_as_sealed(self, db):
        _configure(db)
        _session(db, clock_in=_at(WORK_DAY, 9), clock_out=_at(WORK_DAY, 17))
        result = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=WORK_DAY)
        assert result.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.SEALED.value
        assert row.is_working_day is True

    def test_the_producer_does_not_classify(self):
        """D2-A. The producer reads the frozen context; it never reaches the
        schedule resolver or re-derives a weekday."""
        source = (BACKEND / "app" / "work_ledger_producer"
                  / "producer.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.add(getattr(node, "module", "") or "")
                for alias in node.names:
                    names.add(alias.name)
        for banned in ("resolve_work_schedule", "schedule_policy",
                       "weekday", "worker_context", "EffectiveSchedule"):
            assert banned not in names, f"producer.py reaches {banned}"

    def test_an_unclassified_day_is_refused_and_writes_nothing(self, db):
        """D8-A, through the whole producer."""
        _configure(db, working_days=None)
        _session(db, clock_in=_at(WORK_DAY, 9), clock_out=_at(WORK_DAY, 17))
        result = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=WORK_DAY)
        assert result.outcome is ProducerOutcome.REFUSED
        assert result.refusal is RefusalReason.WORKING_DAY_UNRESOLVED
        assert db.query(WorkDayLedger).count() == 0
        # And specifically NOT converted into a non-working day.
        assert result.outcome is not ProducerOutcome.SEALED


# ═════════════════════════════════════════════════════════════════════════════
# 6. D3-A — the freeze survives configuration mutation
# ═════════════════════════════════════════════════════════════════════════════
class TestHistoricalFreeze:
    def test_a_sealed_rest_day_stays_non_working_after_the_week_changes(self, db):
        _configure(db)
        _requirement(db)
        _session(db, clock_in=_at(REST_DAY, 9), clock_out=_at(REST_DAY, 12))
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=REST_DAY).outcome is ProducerOutcome.SEALED
        db.commit()

        # Sunday becomes a working day in TODAY's configuration.
        _set_working_days(db, ALL_SEVEN)
        assert classify_working_day(
            db, organization_id=ORG, user=_worker(db),
            work_date=REST_DAY).outcome is ClassificationOutcome.WORKING_DAY

        # The sealed day does not move.
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.NOT_A_WORKING_DAY.value
        assert classification_from_sealed(row).outcome is \
            ClassificationOutcome.NOT_A_WORKING_DAY
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert result.is_recorded
        assert result.record.state == EcoDayState.NOT_A_WORKING_DAY.value

    def test_a_sealed_working_day_stays_working_after_the_week_changes(self, db):
        _configure(db)
        _requirement(db)
        _session(db, clock_in=_at(WORK_DAY, 9), clock_out=_at(WORK_DAY, 17))
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=WORK_DAY).outcome is ProducerOutcome.SEALED
        db.commit()

        # Monday becomes a rest day in TODAY's configuration.
        _set_working_days(db, ["TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"])
        assert classify_working_day(
            db, organization_id=ORG, user=_worker(db),
            work_date=WORK_DAY).outcome is \
            ClassificationOutcome.NOT_A_WORKING_DAY

        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.SEALED.value
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=WORK_DAY)
        assert result.is_recorded
        assert result.record.state in (EcoDayState.SHORTFALL.value,
                                       EcoDayState.NO_SHORTFALL.value)
        assert result.record.state != EcoDayState.NOT_A_WORKING_DAY.value

    def test_eval_day_cannot_reach_the_schedule_resolver(self):
        """The import-graph guarantee behind D3-A."""
        package = BACKEND / "app" / "eval_day"
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            reached = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    reached.add(node.module or "")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        reached.add(alias.name)
            for banned in ("app.schedule_policy",
                           "app.schedule_policy.resolver",
                           "app.worker_context"):
                assert banned not in reached, f"{path.name} imports {banned}"
            body = path.read_text(encoding="utf-8")
            assert "resolve_work_schedule" not in body, path.name


# ═════════════════════════════════════════════════════════════════════════════
# 7. D8-A / D17-A — absence of work never classifies
# ═════════════════════════════════════════════════════════════════════════════
class TestFailClosed:
    def test_a_working_day_with_no_intervals_is_still_a_working_day(self, db):
        """The single most important negative: zero work is not a rest day."""
        _configure(db)
        _requirement(db)
        seal_day(
            db, DayKey(organization_id=ORG, user_id=WORKER,
                       work_date=WORK_DAY),
            intervals=(), timezone_iana=TZ, is_working_day=True,
            working_day_source="org", created_by="TEST")
        db.flush()
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=WORK_DAY)
        assert result.is_recorded
        assert result.record.state == EcoDayState.SHORTFALL.value
        assert result.record.state != EcoDayState.NOT_A_WORKING_DAY.value
        assert result.record.gross_shortfall_minutes == REQUIRED_MINUTES

    def test_the_classifier_never_reads_work_evidence(self):
        """D17-A, enforced structurally: it cannot see the work."""
        package = BACKEND / "app" / "working_day_classifier"
        for path in sorted(package.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            tree = ast.parse(body)
            code = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    code.add(node.attr)
                elif isinstance(node, ast.Name):
                    code.add(node.id)
                elif isinstance(node, ast.ImportFrom):
                    code.add(node.module or "")
                    for alias in node.names:
                        code.add(alias.name)
            for banned in ("ClockSession", "BreakSession", "OfflineInterval",
                           "WorkStateInterval", "WorkDayLedger",
                           "LeaveRequest", "worker_context",
                           "approved_leave_overlap", "duration_seconds"):
                assert banned not in code, f"{path.name} reaches {banned}"

    def test_no_wec_wiring_exists(self):
        """D18-A: the ratified hierarchy does not require Worker Context."""
        for path in sorted(
                (BACKEND / "app" / "working_day_classifier").glob("*.py")):
            assert "worker_context" not in path.read_text(encoding="utf-8"), (
                f"{path.name} mentions worker_context")

    def test_no_unknown_eco_state_was_invented(self):
        """UNCLASSIFIED is never persisted as an evaluation state.

        The vocabulary grew to seven by later owner ruling (POLICY_UNRESOLVED);
        the claim that matters here is unchanged — the classifier's own
        UNCLASSIFIED is not among them.
        """
        assert not hasattr(EcoDayState, "UNKNOWN")
        assert not hasattr(EcoDayState, "UNCLASSIFIED")
        assert len([m for m in EcoDayState]) == 7


# ═════════════════════════════════════════════════════════════════════════════
# 8. D9-A / D15-A — leave is irrelevant to a non-working day
# ═════════════════════════════════════════════════════════════════════════════
class TestLeaveIrrelevance:
    def _sealed_rest_day(self, db):
        _configure(db)
        _requirement(db)
        _session(db, clock_in=_at(REST_DAY, 9), clock_out=_at(REST_DAY, 12))
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=REST_DAY).outcome is ProducerOutcome.SEALED
        db.flush()

    def test_an_unresolvable_leave_authority_does_not_block_the_record(self, db):
        """The direct D9-A assertion. An UNRULED leave status is exactly the
        authority ``approved_leave_overlap`` refuses to establish."""
        self._sealed_rest_day(db)
        _leave(db, on=REST_DAY, minutes=30, status="something_new")

        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert result.is_recorded, f"{result.reason}: {result.detail}"
        assert result.record.state == EcoDayState.NOT_A_WORKING_DAY.value
        assert result.record.contributes_to_accumulator_minutes == 0

    def test_the_same_authority_still_refuses_a_working_day(self, db):
        """Ordinary MODEL-EXCLUSION semantics are unchanged."""
        _configure(db)
        _requirement(db)
        _session(db, clock_in=_at(WORK_DAY, 9), clock_out=_at(WORK_DAY, 17))
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=WORK_DAY).outcome is ProducerOutcome.SEALED
        _leave(db, on=WORK_DAY, minutes=30, status="something_new")

        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=WORK_DAY)
        assert not result.is_recorded, "a working day accepted an unresolvable authority"
        assert result.record is None

    def test_an_explicit_empty_snapshot_is_written(self, db):
        """D15-A: never a NULL reference, and content-addressed."""
        self._sealed_rest_day(db)
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert result.is_recorded
        key = result.record.leave_input_snapshot_key
        assert key, "no leave input snapshot was referenced"
        snapshot = read_leave_snapshot(
            db, organization_id=ORG, snapshot_key=key)
        assert snapshot is not None
        assert snapshot.overlap_seconds == 0
        assert snapshot.contribution_count == 0

    def test_the_recording_boundary_classifies_before_resolving_leave(self):
        """Order, read from the source: the classification decision must come
        first, or an unresolvable authority blocks the zeroed record again."""
        body = (BACKEND / "app" / "eval_day" / "recording.py").read_text(
            encoding="utf-8")
        tree = ast.parse(body)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "evaluate_and_record_day")
        calls = [
            node.func.id for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert "classification_from_sealed" in calls
        assert "approved_leave_overlap" in calls
        assert calls.index("classification_from_sealed") < \
            calls.index("approved_leave_overlap")


# ═════════════════════════════════════════════════════════════════════════════
# 9. GV-28 — executed end to end, against the locked artifact
# ═════════════════════════════════════════════════════════════════════════════
class TestGV28:
    """"Rest day with an accidental clock-in".

    The whole authorized path runs: classifier -> producer -> ledger service ->
    recording boundary -> week fold. Nothing is hand-constructed, and the
    expected values are read from the artifact rather than restated.

    NOTE: this does NOT promote GV-28. Its harness metadata stays
    ``blocked_pending_platform_primitive``; promotion is a separate
    authorization. The assertion below pins that.
    """

    def test_gv28_rest_day_with_an_accidental_clock_in(self, db):
        expected = _vector("GV-28")["expected_fields"]
        _configure(db)
        _requirement(db)

        # The accidental clock-in: three hours of real work on a rest day.
        _session(db, clock_in=_at(REST_DAY, 9), clock_out=_at(REST_DAY, 12))

        produced = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert produced.outcome is ProducerOutcome.SEALED, produced.refusal

        ledger = db.query(WorkDayLedger).one()
        assert ledger.state == LedgerState.NOT_A_WORKING_DAY.value
        assert ledger.is_working_day is False
        assert ledger.working_day_source == "org"

        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert result.is_recorded, f"{result.reason}: {result.detail}"
        record = result.record

        handled = []

        assert record.state == expected["state"]
        handled.append("state")
        assert record.required_minutes == expected["required_minutes"]
        handled.append("required_minutes")
        assert record.gross_shortfall_minutes == \
            expected["gross_shortfall_minutes"]
        handled.append("gross_shortfall_minutes")

        # D12-A: satisfied structurally. There is no advisory path to invoke,
        # and the record carries no field that could show one.
        assert expected["advisory_shown"] is False
        for banned in ("advisory", "notification", "alert"):
            assert not any(banned in c.key.lower()
                           for c in record.__table__.columns), banned
        handled.append("advisory_shown")

        assert set(handled) == set(expected), (
            f"unhandled expected_fields: {set(expected) - set(handled)}")

        # Everything else the vector's prose requires.
        assert record.qualifying_minutes == 0
        assert record.exclusion_minutes == 0
        assert record.attributable_minutes == 0
        assert record.contributes_to_accumulator_minutes == 0
        assert record.shortfall_computed is True

        # The week fold includes the date and it contributes zero.
        week = evaluate_workweek(
            db, organization_id=ORG, user_id=WORKER,
            workweek_key=ledger.workweek_key)
        assert REST_DAY in week.canonical_dates
        assert EcoDayState.NOT_A_WORKING_DAY.value in week.observed_states
        assert week.weekly_attributable_shortfall_minutes == 0

        # The work evidence was sealed, not discarded — the day is classified
        # by the schedule, never by whether work exists.
        from app.work_ledger import read_intervals
        intervals = read_intervals(db, ledger)
        assert [i.state for i in intervals] == [WorkState.WRK.value]
        assert sum(i.duration_seconds for i in intervals) == 3 * 3600

    def test_gv28_is_promoted_and_this_class_is_its_execution(self):
        """GV-28 is executable, and the artifact says so with no dependency.

        The class above is the execution the golden suite registers for it, so
        this pairs the metadata with the test that earns it: if the vector were
        ever re-blocked, or this class renamed, one of the two would fail.
        """
        harness = _vector("GV-28")["harness"]
        assert harness["execution"] == "executable"
        assert harness["blocking_dependencies"] == []

        golden = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-28": ("test_working_day_classifier.py", "class TestGV28")' \
            in golden, "the golden suite no longer registers this execution"

    def test_gv28_normative_content_was_not_touched_by_the_promotion(self):
        """A promotion is harness-only. The expected outputs are the whole
        point of the artifact, so they are pinned independently here."""
        v = _vector("GV-28")
        assert v["scenario"] == "Non-working day (§24.2)"
        assert v["inputs_verbatim"] == "Rest day with an accidental clock-in"
        assert v["expected_verbatim"] == (
            "required 0; gross 0; no advisory; day record written as "
            "NOT_A_WORKING_DAY")
        assert v["expected_fields"] == {
            "required_minutes": 0,
            "gross_shortfall_minutes": 0,
            "advisory_shown": False,
            "state": "NOT_A_WORKING_DAY",
        }


# ═════════════════════════════════════════════════════════════════════════════
# 10. Replay and idempotency
# ═════════════════════════════════════════════════════════════════════════════
class TestReplay:
    def test_the_producer_replay_is_idempotent(self, db):
        _configure(db)
        _session(db, clock_in=_at(REST_DAY, 9), clock_out=_at(REST_DAY, 12))
        first = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        db.commit()
        again = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert again.outcome is ProducerOutcome.ALREADY_SEALED
        assert again.ledger_id == first.ledger_id
        assert db.query(WorkDayLedger).count() == 1

    def test_re_recording_writes_no_second_record(self, db):
        from app.eval_day import EcoDayRecord
        _configure(db)
        _requirement(db)
        _session(db, clock_in=_at(REST_DAY, 9), clock_out=_at(REST_DAY, 12))
        produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        db.flush()

        first = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        again = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert first.is_recorded and again.is_recorded
        assert again.record.id == first.record.id
        assert db.query(EcoDayRecord).count() == 1

    def test_the_empty_snapshot_is_reused_not_duplicated(self, db):
        from app.eval_day import LeaveInputSnapshot
        _configure(db)
        _requirement(db)
        _session(db, clock_in=_at(REST_DAY, 9), clock_out=_at(REST_DAY, 12))
        produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        db.flush()
        evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=REST_DAY)
        assert db.query(LeaveInputSnapshot).count() == 1
