"""
EVAL-DAY — day-level ECO evaluation tests.
==========================================

The property under test: a day either evaluates to an exact, reproducible
result, or it refuses. There is no third outcome, and no path produces a number
the evaluator lacks the authority to produce.

Expected values are literals a reader can check by hand — minutes, seconds,
state strings and vector expectations are written out, never imported from the
implementation. The two golden vectors executed here (GV-10, GV-21) are read
from the locked artifact and asserted against its own recorded values.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config_versioning import InMemoryConfigVersionProvider
from app.eval_day import (
    DayEvaluation,
    DayRecordConflict,
    DayRecordImmutable,
    EcoDayRecord,
    EcoDayState,
    EvaluationRefused,
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_MISSING_CLOCK_OUT,
    REASON_NON_WRK_STATE_PRESENT,
    evaluate_day,
    get_day_record,
    record_evaluation,
    round_half_up_minutes,
)
from app.requirement_resolution import RequirementCandidate
from app.work_ledger import LedgerState, WorkDayLedger, WorkState, WorkStateInterval

UTC = timezone.utc
ORG = 51
OTHER_ORG = 52

#: Literals, never imported.
WRK, BRK_M, BRK_R, OFF, LVE = "WRK", "BRK_M", "BRK_R", "OFF", "LVE"
SHORTFALL, NO_SHORTFALL = "SHORTFALL", "NO_SHORTFALL"
NOT_A_WORKING_DAY, REQUIREMENT_UNRESOLVED = "NOT_A_WORKING_DAY", "REQUIREMENT_UNRESOLVED"

DAY = date(2026, 9, 4)
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eval_day"
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
MIGRATION = BACKEND / "alembic" / "versions" / "c9d4a7e2f581_eco_day_record.py"
EXPECTED_REVISION = "c9d4a7e2f581"
EXPECTED_PARENT_REVISION = "b8c3f6a1d472"

#: Revisions AFTER this one that also change ``eco_day_record``, applied in
#: order by the parity test below. Naming them here — rather than loosening the
#: comparison — keeps an unexplained column a failure.
LATER_MIGRATIONS = (
    BACKEND / "alembic" / "versions"
    / "d3a1c8b57f92_leave_input_snapshot_and_day_authority.py",
    BACKEND / "alembic" / "versions"
    / "c5a9f2d38e61_eco_day_record_grace_applied.py",
    BACKEND / "alembic" / "versions"
    / "d7f3b8c41a92_break_classification_foundation.py",
    BACKEND / "alembic" / "versions"
    / "e2b6d4f89c35_eco_day_record_break_treatment_profile.py",
    # MODEL-NETTING: §18 step 10's bank credit, recorded on the day that
    # spent it. A DERIVED credit, never a balance (§15.1 / INV-3).
    BACKEND / "alembic" / "versions"
    / "b6d4f8a12c93_eco_day_record_bank_credit.py",
    # AC-16: the provenance the release gate requires on every day record,
    # plus §35.1's pre-rounding value. Additive and nullable -- the table is
    # append-only, so a row written before them can never acquire them.
    BACKEND / "alembic" / "versions"
    / "a4d9e7b3c216_eco_day_record_ac16_provenance.py",
    # INV-4: observed and qualifying seconds per break state. The arithmetic
    # always honoured the distinction; this is what lets the RECORD show it.
    BACKEND / "alembic" / "versions"
    / "b5f2a8c47d93_eco_day_record_break_evidence.py",
)

#: Column -> the revision that adds it (Owner Ruling 2; MODEL-GRACE).
LATER_REVISION_COLUMNS = {
    "leave_input_snapshot_key": "d3a1c8b57f92",
    "evaluation_generation": "d3a1c8b57f92",
    "supersedes": "d3a1c8b57f92",
    "superseded_by": "d3a1c8b57f92",
    "grace_minutes_applied": "c5a9f2d38e61",
    "evaluation_deferral_reason": "d7f3b8c41a92",
    "classification_snapshot_key": "d7f3b8c41a92",
    "break_treatment_profile_ref": "e2b6d4f89c35",
    "bank_credit_minutes": "b6d4f8a12c93",
    # AC-16 provenance + §35.1 pre-rounding value.
    "qualifying_seconds": "a4d9e7b3c216",
    "requirement_source": "a4d9e7b3c216",
    "requirement_source_rank": "a4d9e7b3c216",
    "requirement_source_ref": "a4d9e7b3c216",
    "rounding_profile": "a4d9e7b3c216",
    "netting_mode": "a4d9e7b3c216",
    "timezone_iana": "a4d9e7b3c216",
    "utc_offset_seconds": "a4d9e7b3c216",
    "calculation_version": "a4d9e7b3c216",
    # INV-4 break evidence.
    "brk_m_seconds": "b5f2a8c47d93",
    "brk_m_qualifying_seconds": "b5f2a8c47d93",
    "brk_r_seconds": "b5f2a8c47d93",
    "brk_r_qualifying_seconds": "b5f2a8c47d93",
}


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    EcoDayRecord.__table__.create(engine, checkfirst=True)
    from app.eval_day import LeaveInputContribution, LeaveInputSnapshot

    LeaveInputSnapshot.__table__.create(engine, checkfirst=True)
    LeaveInputContribution.__table__.create(engine, checkfirst=True)


_uid = [8000]


def _ledger(db, *, org=ORG, on=DAY, state=LedgerState.SEALED.value, version=1,
            tz="Asia/Kolkata", fault=None, user=None):
    _uid[0] += 1
    row = WorkDayLedger(
        organization_id=org, user_id=user if user is not None else _uid[0],
        work_date=datetime(on.year, on.month, on.day),
        # Canonical form (W4-3): the local workweek START DATE. ISO week
        # notation was used here before and is unsafe for a non-Monday start.
        workweek_key=(on - timedelta(days=on.weekday())).isoformat(),
        # D4-A: NOT_A_WORKING_DAY implies is_working_day=False. Derived
        # from the state so a fixture cannot assert both that no work was
        # expected and that the day was a working day.
        ledger_version=version, state=state, timezone_iana=tz, is_working_day=(state != LedgerState.NOT_A_WORKING_DAY.value), working_day_source="org",
        utc_offset_seconds=0, integrity_fault_reason=fault,
        created_at=NOW, created_by="TEST",
    )
    db.add(row)
    db.flush()
    return row


def _interval(db, ledger, *, seq, state, seconds, start=None):
    begin = start or datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
    row = WorkStateInterval(
        ledger_id=ledger.id, organization_id=ledger.organization_id, seq=seq,
        state=state, start_at_utc=begin,
        end_at_utc=begin + timedelta(seconds=seconds),
        duration_seconds=seconds, provenance="WORKER", created_at=NOW,
    )
    db.add(row)
    db.flush()
    return row


def _record_with_empty_leave(db, evaluation):
    """Persist an evaluation with an explicit EMPTY leave input snapshot.

    Owner Ruling 2: the leave input snapshot key is part of the snapshot
    identity, and ``record_evaluation`` refuses an evaluation without one — a
    day with no contributing leave references an explicit empty snapshot rather
    than NULL. These tests seed no leave, so the empty snapshot is the truthful
    input, and saying so here keeps that visible at every call site.
    """
    from app.eval_day import get_or_create_leave_snapshot

    snapshot = get_or_create_leave_snapshot(
        db,
        organization_id=evaluation.organization_id,
        user_id=evaluation.user_id,
        work_date=evaluation.work_date,
        overlap_seconds=0,
        contributions=(),
    )
    return record_evaluation(
        db, evaluation, leave_input_snapshot_key=snapshot.snapshot_key)


def _provider(minutes, *, org=ORG, effective=date(2026, 1, 1), key="v1"):
    p = InMemoryConfigVersionProvider()
    p.capture_version(
        organization_id=org, config_version_id=key, effective_from=effective,
        candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=minutes)],
    )
    return p


# ═════════════════════════════════════════════════════════════════════════════
# Rounding — the single site (R11)
# ═════════════════════════════════════════════════════════════════════════════
class TestRounding:
    @pytest.mark.parametrize("seconds,minutes", [
        (0, 0), (1, 0), (29, 0),        # below half
        (30, 1), (31, 1),               # R11's stated half-up boundary
        (59, 1), (60, 1), (89, 1),
        (90, 2), (91, 2),               # R11's stated example
        (28800, 480),                   # 8h
        (31500, 525),                   # the requirement in several vectors
    ])
    def test_half_up_boundaries(self, seconds, minutes):
        assert round_half_up_minutes(seconds) == minutes

    def test_it_is_not_bankers_rounding(self):
        """Python's round(0.5) is 0. R11 forbids that: 30s must become 1 minute."""
        assert round_half_up_minutes(30) == 1
        assert round_half_up_minutes(90) == 2      # round(1.5) would give 2
        assert round_half_up_minutes(150) == 3     # round(2.5) would give 2

    def test_a_negative_duration_is_refused(self):
        with pytest.raises(ValueError):
            round_half_up_minutes(-1)

    @pytest.mark.parametrize("bad", [1.5, "60", None, True])
    def test_non_integer_seconds_are_refused(self, bad):
        with pytest.raises(TypeError):
            round_half_up_minutes(bad)

    def test_there_is_exactly_one_conversion_site(self):
        """No other module in the package divides by 60."""
        offenders = []
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name == "rounding.py":
                continue
            body = path.read_text(encoding="utf-8")
            for token in ("/ 60", "// 60", "/60", "//60", "seconds / 60"):
                if token in body:
                    offenders.append(f"{path.name}: {token}")
        assert offenders == [], f"second rounding site: {offenders}"


# ═════════════════════════════════════════════════════════════════════════════
# The WRK-only fold (R6, R12)
# ═════════════════════════════════════════════════════════════════════════════
class TestQualifyingFold:
    def test_a_shortfall_day_computes_exactly(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)   # 8h = 480 min
        ev = evaluate_day(ledger=led, intervals=[
            *db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()],
            provider=_provider(525))

        assert ev.state.value == SHORTFALL
        assert ev.qualifying_minutes == 480
        assert ev.required_minutes == 525
        assert ev.gross_shortfall_minutes == 45
        assert ev.exclusion_minutes == 0
        assert ev.attributable_minutes == 45
        assert ev.contributes_to_accumulator_minutes == 45
        assert ev.shortfall_computed is True

    def test_requirement_met_is_no_shortfall(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=31500)   # 525 min exactly
        ev = evaluate_day(ledger=led, intervals=[
            *db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()],
            provider=_provider(525))
        assert ev.state.value == NO_SHORTFALL
        assert ev.gross_shortfall_minutes == 0
        assert ev.attributable_minutes == 0

    def test_over_requirement_never_produces_a_negative(self, db):
        """R12 floors the shortfall at zero — no credit is created."""
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=36000)   # 600 min
        ev = evaluate_day(ledger=led, intervals=[
            *db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()],
            provider=_provider(525))
        assert ev.gross_shortfall_minutes == 0
        assert ev.attributable_minutes == 0

    def test_multiple_intervals_sum_before_rounding(self, db):
        """One rounding site: 29s + 31s is 60s -> 1 minute, not 0 + 1."""
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=29)
        _interval(db, led, seq=2, state=WRK, seconds=31,
                  start=datetime(2026, 9, 4, 12, 0, tzinfo=UTC))
        ev = evaluate_day(ledger=led, intervals=[
            *db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()],
            provider=_provider(10))
        assert ev.qualifying_minutes == 1

    def test_the_fold_is_order_independent(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=3600)
        _interval(db, led, seq=2, state=WRK, seconds=7200,
                  start=datetime(2026, 9, 4, 14, 0, tzinfo=UTC))
        rows = [*db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all()]
        a = evaluate_day(ledger=led, intervals=rows, provider=_provider(525))
        b = evaluate_day(ledger=led, intervals=list(reversed(rows)),
                         provider=_provider(525))
        assert a.qualifying_minutes == b.qualifying_minutes == 180

    def test_a_day_with_no_intervals_qualifies_zero(self, db):
        led = _ledger(db)
        ev = evaluate_day(ledger=led, intervals=[], provider=_provider(525))
        assert ev.qualifying_minutes == 0
        assert ev.gross_shortfall_minutes == 525


class TestNonWrkFailsClosed:
    @pytest.mark.parametrize("state", [BRK_M, BRK_R, OFF, LVE, "TRV", "ONC", "UNK"])
    def test_any_non_wrk_state_refuses(self, db, state):
        """R6: only WRK is unconditionally qualifying. Even OFF is refused —
        'obviously zero' is still a semantic nobody ruled."""
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=3600)
        _interval(db, led, seq=2, state=state, seconds=1800,
                  start=datetime(2026, 9, 4, 13, 0, tzinfo=UTC))
        rows = [*db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all()]
        with pytest.raises(EvaluationRefused) as excinfo:
            evaluate_day(ledger=led, intervals=rows, provider=_provider(525))
        assert excinfo.value.reason == REASON_NON_WRK_STATE_PRESENT
        assert state in excinfo.value.detail

    def test_the_refusal_writes_no_record(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=BRK_M, seconds=2700)
        rows = [*db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all()]
        with pytest.raises(EvaluationRefused):
            evaluate_day(ledger=led, intervals=rows, provider=_provider(525))
        assert get_day_record(db, organization_id=ORG, user_id=led.user_id,
                              work_date=DAY) == ()

    def test_no_classifier_exists_in_the_package(self):
        """Guard: nothing here DECIDES BRK_M vs BRK_R.

        The claim narrowed when owner rulings P-1/P-2 landed. EVAL-DAY now
        CONSUMES a classification — ``classification.py`` reads the immutable
        assertions ``app.break_classification`` recorded post-seal — so the
        word "classification" appears here legitimately. What must never appear
        is the DETERMINATION: no jurisdiction, no legal matrix, no meal-versus-
        rest rule, and above all no reading of the worker-selected break label.
        Those are the tokens still banned, and the import guard below keeps the
        decision in the package that owns it.
        """
        decided_here = ("meal", "rest_break", "jurisdiction", "legal",
                        "classify", "determine_break")
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
            offenders = {n for n in names
                         if any(t in n.lower() for t in decided_here)}
            assert not offenders, f"{path.name} defines {offenders}"

    def test_the_worker_selected_break_label_is_never_read(self):
        """P-1: ``break_sessions.break_type`` is OBSERVATIONAL only.

        Inferring a legal classification from a worker-chosen UI label is
        exactly what P-1 forbids, and EVAL-DAY is where the temptation would
        be cheapest. Asserted as source text because the failure it prevents is
        a single attribute access.
        """
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # Attribute ACCESS, not the word: the module docstrings name the
            # column to explain why it is not read, and a text scan would flag
            # the explanation as the offence.
            reads = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)}
            assert "break_type" not in reads, (
                f"{path.name} reads break_type; P-1 makes the worker-selected "
                f"label observational only")


# ═════════════════════════════════════════════════════════════════════════════
# Ledger boundaries (R10)
# ═════════════════════════════════════════════════════════════════════════════
class TestLedgerBoundary:
    def test_integrity_fault_refuses_and_writes_nothing(self, db):
        led = _ledger(db, state=LedgerState.LEDGER_INTEGRITY_FAULT.value,
                      fault="OVERLAPPING_INTERVALS")
        with pytest.raises(EvaluationRefused) as excinfo:
            evaluate_day(ledger=led, intervals=[], provider=_provider(525))
        assert excinfo.value.reason == REASON_LEDGER_INTEGRITY_FAULT
        assert get_day_record(db, organization_id=ORG, user_id=led.user_id,
                              work_date=DAY) == ()

    def test_missing_clock_out_defers_rather_than_refusing(self, db):
        """S3: the ledger keeps MISSING_CLOCK_OUT; ECO records the deferral."""
        led = _ledger(db, state=LedgerState.MISSING_CLOCK_OUT.value)
        ev = evaluate_day(ledger=led, intervals=[], provider=_provider(525))
        assert ev.state.value == "DEFERRED_PENDING_LEDGER"

    def test_an_open_day_is_not_evaluable(self, db):
        led = _ledger(db, state=LedgerState.OPEN.value)
        with pytest.raises(EvaluationRefused):
            evaluate_day(ledger=led, intervals=[], provider=_provider(525))

    def test_the_ledger_vocabulary_is_unchanged(self):
        """R4: no ECO state was added to LedgerState."""
        assert sorted(s.value for s in LedgerState) == [
            "LEDGER_INTEGRITY_FAULT", "MISSING_CLOCK_OUT", "NOT_A_WORKING_DAY",
            "OPEN", "SEALED",
        ]

    def test_the_eco_vocabulary_is_exactly_seven(self):
        """R4's six, plus POLICY_UNRESOLVED by later owner ruling.

        The seventh was added only after an inspection established that none of
        R4's six could represent a policy-resolution failure without either
        misusing an existing state or refusing to record the day at all.
        """
        assert sorted(s.value for s in EcoDayState) == [
            "DEFERRED_PENDING_LEDGER", "EVALUABLE", "NOT_A_WORKING_DAY",
            "NO_SHORTFALL", "POLICY_UNRESOLVED", "REQUIREMENT_UNRESOLVED",
            "SHORTFALL",
        ]


# ═════════════════════════════════════════════════════════════════════════════
# Non-working day short-circuit (R5)
# ═════════════════════════════════════════════════════════════════════════════
class TestDeferredPendingLedger:
    """S2 / S3 — the ledger→ECO bridge for an unavailable departure."""

    def _deferred(self, db):
        led = _ledger(db, state=LedgerState.MISSING_CLOCK_OUT.value)
        return led, evaluate_day(ledger=led, intervals=[],
                                 provider=_provider(525))

    def test_state_is_the_eco_deferred_value(self, db):
        _, ev = self._deferred(db)
        assert ev.state.value == "DEFERRED_PENDING_LEDGER"

    def test_shortfall_is_not_computed(self, db):
        _, ev = self._deferred(db)
        assert ev.shortfall_computed is False

    @pytest.mark.parametrize("field", [
        "required_minutes", "qualifying_minutes", "gross_shortfall_minutes",
        "exclusion_minutes", "attributable_minutes", "schedule_variance_minutes",
    ])
    def test_no_measurement_is_fabricated(self, db, field):
        _, ev = self._deferred(db)
        assert getattr(ev, field) is None, f"{field} was invented"

    def test_it_contributes_exactly_zero(self, db):
        """Truthfully computable: a deferred day contributes nothing. GV-16 and
        GV-17 independently state 'contributes 0'."""
        _, ev = self._deferred(db)
        assert ev.contributes_to_accumulator_minutes == 0

    def test_a_deferred_record_persists(self, db):
        """S2: a persisted evaluation result, not a refusal."""
        led, ev = self._deferred(db)
        row = _record_with_empty_leave(db, ev)
        assert row.state == "DEFERRED_PENDING_LEDGER"
        assert row.shortfall_computed is False
        assert row.required_minutes is None
        assert row.contributes_to_accumulator_minutes == 0

    def test_no_requirement_arithmetic_is_performed(self, db):
        """Deferral is decided before requirement resolution, so a provider
        with no version at all changes nothing."""
        led = _ledger(db, state=LedgerState.MISSING_CLOCK_OUT.value)
        ev = evaluate_day(ledger=led, intervals=[],
                          provider=InMemoryConfigVersionProvider())
        assert ev.state.value == "DEFERRED_PENDING_LEDGER"
        assert ev.requirement_outcome == "NOT_APPLICABLE"

    def test_a_later_sealed_version_creates_a_new_snapshot(self, db):
        """S2: a subsequent explicit evaluation of a resolved ledger writes a
        new record; the deferred one is not rewritten."""
        uid = _uid[0] + 1
        deferred = _ledger(db, state=LedgerState.MISSING_CLOCK_OUT.value,
                           version=1, user=uid)
        _record_with_empty_leave(db, evaluate_day(ledger=deferred, intervals=[],
                                           provider=_provider(525)))

        resolved = _ledger(db, state=LedgerState.SEALED.value, version=2,
                           user=uid)
        _interval(db, resolved, seq=1, state=WRK, seconds=28800)
        _record_with_empty_leave(db, _evaluate(db, resolved, _provider(525)))

        records = get_day_record(db, organization_id=ORG, user_id=uid,
                                 work_date=DAY)
        assert [r.ledger_version for r in records] == [1, 2]
        assert [r.state for r in records] == ["DEFERRED_PENDING_LEDGER", SHORTFALL]

    def test_the_dataclass_refuses_a_fabricated_measurement(self):
        with pytest.raises(ValueError):
            DayEvaluation(
                organization_id=ORG, user_id=1, work_date=DAY, ledger_version=1,
                state=EcoDayState.DEFERRED_PENDING_LEDGER,
                requirement_outcome="NOT_APPLICABLE", shortfall_computed=False,
                contributes_to_accumulator_minutes=0, qualifying_minutes=480,
            )

    def test_the_dataclass_requires_zero_contribution(self):
        with pytest.raises(ValueError):
            DayEvaluation(
                organization_id=ORG, user_id=1, work_date=DAY, ledger_version=1,
                state=EcoDayState.DEFERRED_PENDING_LEDGER,
                requirement_outcome="NOT_APPLICABLE", shortfall_computed=False,
                contributes_to_accumulator_minutes=5,
            )

    #: Names that contain a banned token but are not what the token is looking
    #: for. ONE entry, and it is a §18 measurement rather than a mechanism:
    #: `schedule_variance_minutes` computes step 14's
    #: `actual_end_local - scheduled_end_local`. The word "schedule" appears
    #: because the quantity is about the Worker's SCHEDULE, not because
    #: anything is scheduled. The compensating assertion below proves that
    #: directly, so this is a narrowing with evidence, not an exemption.
    _NOT_A_SCHEDULER = {"schedule_variance_minutes"}

    def test_no_recomputation_scheduler_exists(self):
        """S2 forbids background recomputation in this increment."""
        banned = ("retry", "schedul", "recompute", "background", "poll", "cron")
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
            }
            offenders = {
                n for n in names
                if any(b in n.lower() for b in banned)
                and n not in self._NOT_A_SCHEDULER
            }
            assert not offenders, f"{path.name} defines {offenders}"

    def test_the_one_allowed_schedule_name_is_pure(self):
        """The compensating guard for `_NOT_A_SCHEDULER`.

        A scheduler needs something to schedule against: a session, a clock, a
        queue. `schedule_variance_minutes` takes one sealed ledger row and
        returns an integer, so it cannot be one however it is named. Asserted
        rather than assumed, because the exemption above is only honest while
        this stays true.
        """
        import inspect as _inspect

        from app.eval_day.evaluator import schedule_variance_minutes

        params = list(
            _inspect.signature(schedule_variance_minutes).parameters)
        assert params == ["ledger"], params

        src = _inspect.getsource(schedule_variance_minutes)
        for forbidden in ("db.", "Session", "datetime.now", "utcnow",
                          "sleep", "queue", "task"):
            assert forbidden not in src, (
                f"{forbidden!r} appears in the one function exempted from the "
                f"scheduler guard; the exemption's premise no longer holds")


class TestEvaluableReadiness:
    """S1 — readiness is internal and never persisted."""

    def test_a_sealed_resolved_day_is_evaluable(self, db):
        from app.eval_day import readiness
        from app.requirement_resolution import resolve_requirement_strict

        req = resolve_requirement_strict(
            organization_id=ORG, user_id=1, as_of=DAY, provider=_provider(525))
        assert readiness(LedgerState.SEALED.value, req) is EcoDayState.EVALUABLE

    @pytest.mark.parametrize("ledger_state", [
        "OPEN", "MISSING_CLOCK_OUT", "NOT_A_WORKING_DAY", "LEDGER_INTEGRITY_FAULT",
    ])
    def test_an_unsealed_day_is_not_evaluable(self, ledger_state):
        from app.eval_day import readiness
        from app.requirement_resolution import resolve_requirement_strict

        req = resolve_requirement_strict(
            organization_id=ORG, user_id=1, as_of=DAY, provider=_provider(525))
        assert readiness(ledger_state, req) is None

    def test_an_unresolved_requirement_is_not_evaluable(self):
        from app.eval_day import readiness
        from app.requirement_resolution import resolve_requirement_strict

        req = resolve_requirement_strict(
            organization_id=ORG, user_id=1, as_of=DAY,
            provider=InMemoryConfigVersionProvider())
        assert readiness(LedgerState.SEALED.value, req) is None

    def test_evaluable_resolves_to_shortfall(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        assert _evaluate(db, led, _provider(525)).state.value == SHORTFALL

    def test_evaluable_resolves_to_no_shortfall(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=31500)
        assert _evaluate(db, led, _provider(525)).state.value == NO_SHORTFALL

    def test_evaluable_is_never_a_result(self):
        with pytest.raises(ValueError):
            DayEvaluation(
                organization_id=ORG, user_id=1, work_date=DAY, ledger_version=1,
                state=EcoDayState.EVALUABLE, requirement_outcome="RESOLVED",
                shortfall_computed=False,
            )

    def test_no_persisted_record_ever_carries_evaluable(self, db):
        from app.eval_day import PERSISTABLE_STATES

        assert EcoDayState.EVALUABLE not in PERSISTABLE_STATES
        rows = db.execute(sa.select(EcoDayRecord.state).where(
            EcoDayRecord.state == "EVALUABLE")).scalars().all()
        assert rows == []


class TestNotAWorkingDay:
    def test_everything_zeroes(self, db):
        led = _ledger(db, state=LedgerState.NOT_A_WORKING_DAY.value)
        ev = evaluate_day(ledger=led, intervals=[], provider=_provider(525))
        assert ev.state.value == NOT_A_WORKING_DAY
        assert ev.required_minutes == 0
        assert ev.qualifying_minutes == 0
        assert ev.gross_shortfall_minutes == 0
        assert ev.exclusion_minutes == 0
        assert ev.attributable_minutes == 0
        assert ev.schedule_variance_minutes == 0
        assert ev.contributes_to_accumulator_minutes == 0
        assert ev.shortfall_computed is True

    def test_it_short_circuits_before_requirement_resolution(self, db):
        """The zero means 'not a working day', not 'a resolved requirement of
        zero' — so a provider with no version at all must not change it."""
        led = _ledger(db, state=LedgerState.NOT_A_WORKING_DAY.value)
        ev = evaluate_day(ledger=led, intervals=[],
                          provider=InMemoryConfigVersionProvider())
        assert ev.required_minutes == 0
        assert ev.state.value == NOT_A_WORKING_DAY

    def test_step_5_still_refuses_zero_as_a_requirement(self):
        """R5: the strict resolver was NOT relaxed to permit zero."""
        from app.requirement_resolution import resolve_requirement_strict

        p = InMemoryConfigVersionProvider()
        p.capture_version(
            organization_id=ORG, config_version_id="z", effective_from=DAY,
            candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=0)])
        out = resolve_requirement_strict(
            organization_id=ORG, user_id=1, as_of=DAY, provider=p)
        assert out.outcome.value == REQUIREMENT_UNRESOLVED
        assert out.required_minutes is None


# ═════════════════════════════════════════════════════════════════════════════
# Requirement outcomes (R13)
# ═════════════════════════════════════════════════════════════════════════════
class TestRequirementOutcomes:
    def test_unresolved_requirement_carries_no_minutes(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        rows = [*db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all()]
        p = InMemoryConfigVersionProvider()
        p.capture_version(organization_id=ORG, config_version_id="empty",
                          effective_from=date(2026, 1, 1))
        ev = evaluate_day(ledger=led, intervals=rows, provider=p)
        assert ev.state.value == REQUIREMENT_UNRESOLVED
        assert ev.required_minutes is None
        assert ev.qualifying_minutes is None
        assert ev.gross_shortfall_minutes is None
        assert ev.attributable_minutes is None
        assert ev.shortfall_computed is False

    def test_no_version_also_carries_no_minutes(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        rows = [*db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all()]
        ev = evaluate_day(ledger=led, intervals=rows,
                          provider=InMemoryConfigVersionProvider())
        assert ev.state.value == REQUIREMENT_UNRESOLVED
        assert ev.required_minutes is None
        assert ev.requirement_outcome == "NO_VERSION"

    def test_the_step_5_outcome_is_preserved(self, db):
        """R4 gives one unresolved state; the distinction must not be lost."""
        led = _ledger(db)
        p = InMemoryConfigVersionProvider()
        p.capture_version(organization_id=ORG, config_version_id="empty",
                          effective_from=date(2026, 1, 1))
        ev = evaluate_day(ledger=led, intervals=[], provider=p)
        assert ev.requirement_outcome == REQUIREMENT_UNRESOLVED

    def test_the_legacy_eight_hour_default_is_never_reachable(self, db):
        """28800 seconds must not appear as a fabricated requirement."""
        led = _ledger(db)
        ev = evaluate_day(ledger=led, intervals=[],
                          provider=InMemoryConfigVersionProvider())
        assert ev.required_minutes is None
        assert ev.required_minutes != 480


class TestUncomputedCannotCarryNumbers:
    def test_the_dataclass_refuses_a_number_on_an_unresolved_state(self):
        with pytest.raises(ValueError):
            DayEvaluation(
                organization_id=ORG, user_id=1, work_date=DAY, ledger_version=1,
                state=EcoDayState.REQUIREMENT_UNRESOLVED,
                requirement_outcome="NO_VERSION", shortfall_computed=False,
                required_minutes=480,
            )

    def test_a_computed_state_requires_every_minute_field(self):
        with pytest.raises(ValueError):
            DayEvaluation(
                organization_id=ORG, user_id=1, work_date=DAY, ledger_version=1,
                state=EcoDayState.SHORTFALL, requirement_outcome="RESOLVED",
                shortfall_computed=True, required_minutes=525,
            )


# ═════════════════════════════════════════════════════════════════════════════
# Persistence: idempotency, provenance, immutability (R1, R2, R14, R15)
# ═════════════════════════════════════════════════════════════════════════════
def _evaluate(db, led, provider):
    rows = [*db.execute(sa.select(WorkStateInterval).where(
        WorkStateInterval.ledger_id == led.id)).scalars().all()]
    return evaluate_day(ledger=led, intervals=rows, provider=provider)


class TestPersistence:
    def test_a_record_is_written_once(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        ev = _evaluate(db, led, _provider(525))
        first = _record_with_empty_leave(db, ev)
        assert first.id is not None
        assert first.state == SHORTFALL

    def test_re_evaluating_the_same_snapshot_is_idempotent(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        a = _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        b = _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        assert a.id == b.id
        assert len(get_day_record(db, organization_id=ORG, user_id=led.user_id,
                                  work_date=DAY)) == 1

    def test_a_new_ledger_version_produces_a_new_record(self, db):
        """R1: recomputation creates a new derived version, never a rewrite."""
        uid = _uid[0] + 1
        v1 = _ledger(db, version=1, user=uid)
        _interval(db, v1, seq=1, state=WRK, seconds=28800)
        _record_with_empty_leave(db, _evaluate(db, v1, _provider(525)))

        v2 = _ledger(db, version=2, user=uid)
        _interval(db, v2, seq=1, state=WRK, seconds=31500)
        _record_with_empty_leave(db, _evaluate(db, v2, _provider(525)))

        records = get_day_record(db, organization_id=ORG, user_id=uid,
                                 work_date=DAY)
        assert [r.ledger_version for r in records] == [1, 2]
        assert [r.state for r in records] == [SHORTFALL, NO_SHORTFALL]

    def test_a_differing_result_for_one_snapshot_is_refused(self, db):
        """Every authoritative input is in the identity, so a different answer
        means the evaluator changed — surfaced, not silently overwritten."""
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))

        tampered = _evaluate(db, led, _provider(525))
        object.__setattr__(tampered, "gross_shortfall_minutes", 999)
        object.__setattr__(tampered, "attributable_minutes", 999)
        object.__setattr__(tampered, "contributes_to_accumulator_minutes", 999)
        with pytest.raises(DayRecordConflict):
            _record_with_empty_leave(db, tampered)
        db.rollback()

    def test_provenance_is_retained(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        row = _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        assert row.organization_id == ORG
        assert row.user_id == led.user_id
        assert row.work_date == DAY
        assert row.ledger_version == 1
        assert row.config_version == "v1"
        assert row.requirement_outcome == "RESOLVED"
        assert row.content_hash and len(row.content_hash) == 64
        assert row.evaluated_at_utc is not None

    def test_policy_version_is_null_in_this_increment(self, db):
        """No policy-version concept exists distinct from configuration."""
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        row = _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        assert row.policy_version is None

    def test_schedule_variance_is_never_invented(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        row = _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        assert row.schedule_variance_minutes is None

    def test_a_record_cannot_be_modified(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        row = _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        row.attributable_minutes = 1
        with pytest.raises(DayRecordImmutable):
            db.flush()
        db.rollback()

    def test_a_record_cannot_be_deleted(self, db):
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        row = _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        db.delete(row)
        with pytest.raises(DayRecordImmutable):
            db.flush()
        db.rollback()

    def test_organization_isolation(self, db):
        led = _ledger(db, org=ORG)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        _record_with_empty_leave(db, _evaluate(db, led, _provider(525)))
        assert get_day_record(db, organization_id=OTHER_ORG,
                              user_id=led.user_id, work_date=DAY) == ()

    def test_the_same_snapshot_cannot_be_stored_twice_at_the_database(self, db):
        """The COALESCE index: NULL policy_version must not defeat uniqueness."""
        led = _ledger(db)
        _interval(db, led, seq=1, state=WRK, seconds=28800)
        ev = _evaluate(db, led, _provider(525))
        _record_with_empty_leave(db, ev)
        db.add(EcoDayRecord(
            organization_id=ORG, user_id=led.user_id, work_date=DAY,
            ledger_version=1, policy_version=None, config_version="v1",
            requirement_outcome="RESOLVED", content_hash="x" * 64,
            evaluated_at_utc=NOW, state=SHORTFALL, shortfall_computed=True,
            required_minutes=525, qualifying_minutes=480,
            gross_shortfall_minutes=45, exclusion_minutes=0,
            attributable_minutes=45, contributes_to_accumulator_minutes=45,
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Golden vectors executed against the locked artifact
# ═════════════════════════════════════════════════════════════════════════════
def _vector(vid):
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    for v in data["vectors"]:
        if v["id"] == vid:
            return v
    raise AssertionError(f"{vid} not found")


class TestGoldenVectorsExecuted:
    """GV-10 and GV-21 — the two vectors whose actual dependencies are now met.

    Expected values are read from the locked artifact, never restated here, so
    the assertion is against the normative fixture itself.
    """

    def test_gv10_dst_spring_forward(self, db):
        """Shift 01:00-10:00 local on the Europe/London transition day.

        Clocks jump 01:00 -> 02:00, so the wall-clock span reads nine hours and
        only eight elapsed. 01:00 GMT is 01:00 UTC; 10:00 BST is 09:00 UTC.
        """
        expected = _vector("GV-10")["expected_fields"]
        transition = date(2026, 3, 29)
        led = _ledger(db, on=transition, tz="Europe/London")
        _interval(db, led, seq=1, state=WRK, seconds=8 * 3600,
                  start=datetime(2026, 3, 29, 1, 0, tzinfo=UTC))
        ev = evaluate_day(
            ledger=led,
            intervals=[*db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()],
            provider=_provider(525, effective=date(2026, 1, 1)),
        )
        assert ev.qualifying_minutes == expected["duration_hours"] * 60
        assert ev.required_minutes == expected["required_minutes"]

    def test_gv21_policy_versioning(self, db):
        """Evaluate 2026-08-28 after v4 (525) supersedes v3 (510).

        The evaluated date predates v4, so it must resolve 510 — and keep
        resolving 510 after v4 exists.
        """
        expected = _vector("GV-21")["expected_fields"]
        evaluated = date.fromisoformat(expected["evaluated_date"])

        p = InMemoryConfigVersionProvider()
        p.capture_version(
            organization_id=ORG, config_version_id="v3",
            effective_from=date(2026, 1, 1),
            candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=510)])
        p.capture_version(
            organization_id=ORG, config_version_id="v4",
            effective_from=date(2026, 9, 1),
            candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=525)])

        led = _ledger(db, on=evaluated)
        _interval(db, led, seq=1, state=WRK, seconds=28800,
                  start=datetime(2026, 8, 28, 9, 0, tzinfo=UTC))
        rows = [*db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all()]

        ev = evaluate_day(ledger=led, intervals=rows, provider=p)
        assert ev.required_minutes == expected["required_minutes"]
        assert ev.config_version == "v3"

        # stable_in_perpetuity: re-resolving after v4 exists gives the same answer
        again = evaluate_day(ledger=led, intervals=rows, provider=p)
        assert again.required_minutes == expected["required_minutes"]
        assert expected["stable_in_perpetuity"] is True

    def test_the_locked_artifact_is_read_not_rewritten(self):
        """Guard: the fixtures above assert against the artifact's own values."""
        assert _vector("GV-10")["expected_fields"]["required_minutes"] == 525
        assert _vector("GV-21")["expected_fields"]["required_minutes"] == 510
        # GV-10 was promoted to executable by the D18-F8 producer ruling. Its
        # NORMATIVE content — the 525 above — is what this guard protects, and
        # that is unchanged; the execution value is harness metadata.
        assert _vector("GV-10")["harness"]["execution"] == "executable"
        assert _vector("GV-10")["harness"]["blocking_dependencies"] == []


class TestVectorsThatRemainBlocked:
    """Each blocked vector, with the precise dependency that blocks it."""

    @pytest.mark.parametrize("vid,blocker", [
        # GV-01 was here under "BRK_M + MODEL-EXCLUSION +
        # schedule_variance". All three closed: breaks are gaps (D-18),
        # MODEL-EXCLUSION shipped with P-4, and D-1.1 froze the scheduled
        # end so §18 step 14 has its second operand. Its execution lives
        # in `test_gv01_execution.py::TestGV01`.
        # GV-02 and GV-03 were here under "classifier". P-2 is now
        # implemented — a post-seal classification artifact, a versioned
        # BreakTreatmentProfile, and the cumulative rest cap applied in seconds
        # before the single rounding site — so both are executable and their
        # old rationale is not carried forward. Their execution lives in
        # `test_gv02_gv03_execution.py::TestGV02` and `::TestGV03`.
        # GV-16 was here under "expects state MISSING_CLOCK_OUT; S3 requires
        # DEFERRED". The owner resolved that contradiction in S3's favour and
        # authorised the one normative amendment, then the departure-resolution
        # primitive supplied the missing verdict — so it is no longer a blocked
        # vector and its old rationale is not carried forward. Its execution
        # lives in `test_departure_resolution.py::TestGV16`.
        # GV-17 was here under "visible_to_* and recomputes_on_resolution are
        # NOTIF and recomputation, both unauthorised". Owner ruling B3 settled
        # that visible_to_* is a read-surface requirement rather than a
        # notification one, and recomputation turned out to be authorised all
        # along — the producer supersedes a corrected day under D18-F3-S and
        # the recording layer supersedes the evaluation rather than rewriting
        # it. So it is no longer a blocked vector and its old rationale is not
        # carried forward. Its execution lives in
        # `test_gv17_execution.py::TestGV17`.
        # GV-26 was here under "expects a LEDGER_INTEGRITY_FAULT record; S4
        # fails closed". §24.15's third obligation — "raise a platform
        # incident" — had no implementation, so the vector's
        # platform_incident_raised was a claim no test could fail. Owner ruling
        # D-9.1 authorized app.eco_ledger_incident, and the vector was then
        # PROMOTED. The apparent state conflict was never one: GV-26's
        # expected state is the LEDGER's, which D-8.1 makes authoritative for
        # ledger facts, and S4's fail-closed behaviour is exactly the second
        # field. That reading is still pinned by
        # test_gv26_expected_state_conflicts_with_s4 below, and its execution
        # lives in `test_eco_ledger_incident.py::TestGV26`.
        # GV-28 was here under "advisory_shown depends on NOTIF". Owner ruling
        # D12-A settled that the structural absence of any advisory path is
        # sufficient for that field, and the dedicated working-day classifier
        # then made the vector executable — so it is no longer a blocked
        # vector and its old rationale is not carried forward. Its execution
        # lives in `test_working_day_classifier.py::TestGV28`.
    ])
    def test_blocked_vector_fixture_is_unchanged(self, vid, blocker):
        v = _vector(vid)
        assert v["harness"]["execution"] == "blocked_pending_platform_primitive"
        assert v["expected_fields"], blocker

    def test_gv16_state_conflict_was_resolved_in_s3s_favour(self):
        """The conflict this used to pin has been RESOLVED by owner ruling.

        It previously asserted that GV-16 expected ``MISSING_CLOCK_OUT`` while
        S3 required ``DEFERRED_PENDING_LEDGER`` — "pinned so it cannot be
        forgotten or silently resolved". It was not forgotten and it was not
        silent: the owner ruled in S3's favour and authorised a normative
        amendment to GV-16's ``expected_fields.state``.

        So the claim inverts. R4's six-value vocabulary stands, MISSING_CLOCK_OUT
        stays out of it and remains a Work Ledger evidence state, and the vector
        now expects what the platform produces.
        """
        assert _vector("GV-16")["expected_fields"]["state"] == "DEFERRED_PENDING_LEDGER"
        assert "MISSING_CLOCK_OUT" not in {s.value for s in EcoDayState}
        assert "MISSING_CLOCK_OUT" in {s.value for s in LedgerState}

    def test_gv26_expected_state_conflicts_with_s4(self):
        """GV-26's expected state is the LEDGER's, not an ECO evaluation state.

        This used to read as a conflict pinned so it could not be forgotten. It
        was not forgotten: owner ruling D-8.1 settled it without amending the
        vector. LedgerState is authoritative for ledger facts and already has
        LEDGER_INTEGRITY_FAULT; EcoDayState stays the seven-member EVALUATION
        vocabulary and never gains it. Those are the only two readings
        consistent with the vector's own second field — a day that was never
        evaluated cannot carry an evaluation state.

        The assertions are unchanged, because both halves are still true and
        still worth failing on: an ECO state gaining this member would break
        S4's fail-closed contract.
        """
        assert _vector("GV-26")["expected_fields"]["state"] == "LEDGER_INTEGRITY_FAULT"
        assert "LEDGER_INTEGRITY_FAULT" not in {s.value for s in EcoDayState}

    def test_gv17_deferred_fields_are_now_satisfiable(self):
        """Two of five fields are now producible; three are not, so it stays
        blocked. Recorded so the partial progress is visible without being
        mistaken for executability."""
        fields = _vector("GV-17")["expected_fields"]
        assert fields["state"] == "DEFERRED_PENDING_LEDGER"
        assert fields["contributes_to_accumulator_minutes"] == 0
        assert set(fields) - {"state", "contributes_to_accumulator_minutes"} == {
            "visible_to_worker", "visible_to_administrator",
            "recomputes_on_resolution",
        }


# ═════════════════════════════════════════════════════════════════════════════
# Isolation guards
# ═════════════════════════════════════════════════════════════════════════════
def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


class TestIsolationGuards:
    def test_the_guard_reads_the_whole_package(self):
        """Guard the guard: a new module must be added here deliberately, so
        the forbidden-import checks below cannot silently skip one.

        ``recording.py`` is the EVAL-DAY recording boundary — sealed ledger to
        immutable record — and is covered by every import guard in this class.
        """
        assert {p.name for p in sorted(PACKAGE.glob("*.py"))} == {
            "__init__.py",
            # Owner Decision 1A: the ONE definition of the leave-snapshot
            # append-only trigger SQL, imported by both emitters. String
            # constants only -- it imports nothing, which is why it cannot
            # violate an import guard.
            "append_only_ddl.py",
            "classification.py", "errors.py", "evaluator.py",
            "exclusion.py", "leave_snapshot.py", "models.py",
            # MODEL-NETTING's §18 step-10 fold. In the package because the
            # credit is an EVAL-DAY result; it reads day records and is
            # therefore boundary-side, never inside the pure evaluator.
            "netting.py",
            "recording.py", "rounding.py", "states.py",
        }

    @pytest.mark.parametrize("forbidden", [
        "app._worked", "app.payroll", "app.risk", "app.active_time",
        "app.intelligence", "app.breaks",
    ])
    def test_no_legacy_or_activity_dependency(self, forbidden):
        for path in sorted(PACKAGE.glob("*.py")):
            offenders = {
                m for m in _imports(path)
                if m == forbidden or m.startswith(forbidden + ".")
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_forbidden_modules_exist(self):
        import importlib.util

        for name in ("app._worked", "app.payroll", "app.risk", "app.breaks"):
            assert importlib.util.find_spec(name) is not None, name

    def test_no_weekly_event_or_notification_logic(self):
        """What EVAL-DAY must not grow into.

        ``grace`` left this list when MODEL-GRACE landed: §11 places the
        de-minimis decision in the DAY evaluation precisely so it never becomes
        "a magic number in the threshold comparison". Naming it here would have
        forced it into the very place §11 forbids. ``threshold`` stays banned,
        which is the half of the pair that actually protects the boundary, and
        netting and horizon stay banned because they remain unbuilt.
        """
        banned = ("week", "threshold", "accumulat", "notif", "event", "netting",
                  "horizon")
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
            }
            offenders = {n for n in names if any(b in n.lower() for b in banned)}
            assert not offenders, f"{path.name} defines {offenders}"

    def test_nothing_in_the_application_wires_this_package(self):
        """EVAL-WEEK legitimately consumes EVAL-DAY output — that is the
        authorised composition, and the boundary this guard protects is the
        APPLICATION reaching in, not one foundation layer building on another."""
        # ``eval_event`` is the next stage of the same closed pipeline
        # (EVAL-DAY -> EVAL-WEEK -> EVAL-EVENT) and reads day records for the
        # correction lineage D4 requires. Foundation, not application.
        #
        # ``eco_day_view`` is the GV-17 read contract (owner ruling B1/B2/B3
        # increment). It consumes recorded day records and borrows EVAL-DAY's
        # §19 step-6 predicate rather than restating it — which is the whole
        # reason it may import this package. It is equally not wired: no route
        # or UI consumes it either, and its own guard pins that.
        #
        # ``eco_driver`` is the P0 calculation driver, and the one thing this
        # guard was always going to have to admit: EVAL-DAY exists to be
        # invoked, and until the driver existed nothing did. It is not the
        # APPLICATION reaching in -- it has no route, no UI and no scheduler,
        # and its own guards pin that it calls this package and computes
        # nothing itself.
        foundation = {"eval_day", "eval_week", "eval_event", "eco_day_view",
                      "eco_driver"}
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if foundation & set(path.parts):
                continue
            if any(m.startswith("app.eval_day") for m in _imports(path)):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == []


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════
def _tree():
    return ast.parse(MIGRATION.read_text(encoding="utf-8"))


def _const(tree, name):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(name)


class TestMigration:
    def test_revision_and_parent(self):
        tree = _tree()
        assert _const(tree, "revision") == EXPECTED_REVISION
        assert _const(tree, "down_revision") == EXPECTED_PARENT_REVISION

    def test_single_parent_not_a_merge(self):
        assert isinstance(_const(_tree(), "down_revision"), str)

    def test_one_head_one_root(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert len(sd.get_heads()) == 1, sd.get_heads()
        assert len(sd.get_bases()) == 1

    def test_additive_only_and_no_backfill(self):
        called = {
            n.func.attr for n in ast.walk(_tree())
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert not (called & {"alter_column", "drop_column", "add_column",
                              "rename_table", "drop_constraint", "drop_index"})
        assert "bulk_insert" not in called
        for node in _tree().body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    upper = " ".join(inner.value.upper().split())
                    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
                        assert verb not in upper, inner.value

    def test_it_runs_and_matches_the_model(self):
        """The chain — this revision plus every later one touching the table.

        ``d3a1c8b57f92`` (Owner Ruling 2) adds the leave identity and the
        authority columns, so this revision alone no longer produces the whole
        model. Applied in order and compared as a set, so an unexplained extra
        column still fails: ``LATER_REVISION_COLUMNS`` names each addition and
        the revision that owns it.
        """
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        import importlib.util

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)

        for path in (MIGRATION, *LATER_MIGRATIONS):
            spec = importlib.util.spec_from_file_location(
                f"_evalday_mig_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with eng.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    module.upgrade()

        cols = sa.inspect(eng).get_columns("eco_day_record")
        assert sorted(c["name"] for c in cols) == sorted(
            c.name for c in EcoDayRecord.__table__.columns)
        assert {c["name"]: c["nullable"] for c in cols} == {
            c.name: c.nullable for c in EcoDayRecord.__table__.columns}
        # Every later addition is accounted for by name, not waved through.
        assert set(LATER_REVISION_COLUMNS) <= {c["name"] for c in cols}
        with eng.connect() as conn:
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM eco_day_record")).scalar() == 0
            sql = conn.execute(sa.text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_eco_day_record_snapshot'")).scalar()
        assert sql is not None and "COALESCE" in sql.upper()

    def test_postgres_dialect_compilation(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        ddl = str(CreateTable(EcoDayRecord.__table__).compile(
            dialect=postgresql.dialect()))
        assert "work_date DATE" in ddl
        assert "evaluated_at_utc TIMESTAMP WITH TIME ZONE" in ddl
        assert "state VARCHAR(32)" in ddl
        assert "CREATE TYPE" not in ddl
        assert "REFERENCES organizations (id) ON DELETE RESTRICT" in ddl
