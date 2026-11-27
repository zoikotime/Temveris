"""
EVAL-DAY recording boundary — tests.
====================================

The property under test: **the authoritative configuration snapshot is decided
here, once, and cannot be supplied by a caller.**

The service takes an identity and nothing else. It resolves the current ledger
version itself, constructs the configuration provider itself, and delegates
every calculation to the pure evaluator. So the tests are largely about what it
refuses to be told, and about the snapshot identity that comes out the other
end.

Configuration is seeded as real ``ConfigVersion`` rows rather than injected
through an in-memory provider — that is the whole point of the increment, and a
test that injected a provider would prove nothing about the production path.

Literals are transcribed rather than imported where a wrong production constant
could otherwise agree with itself.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.config_versioning import ConfigValue, ConfigVersion, compute_content_hash
from app.eval_day import (
    DayRecordConflict,
    DayRecordingResult,
    EcoDayRecord,
    EcoDayState,
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_NOT_SEALED,
    REASON_NO_LEDGER_DAY,
    REASON_NON_WRK_STATE_PRESENT,
    REASON_RESULT_CONFLICT,
    RecordingOutcome,
    evaluate_and_record_day,
    evaluate_day,
    record_evaluation,
)
from app.eval_week import WeeklyState, evaluate_workweek
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval
from tests._eco_policy import DEFAULT_GRACE_MINUTES, DEFAULT_POLICY_KEY

UTC = timezone.utc
ORG = 81
OTHER_ORG = 82
USER = 8500
OTHER_USER = 8600

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
EVAL_DAY = BACKEND / "app" / "eval_day"
EVAL_WEEK = BACKEND / "app" / "eval_week"

# ── Independently transcribed ────────────────────────────────────────────────
REQUIREMENT_KEY = "required_minutes"
MONDAY_WEEK = "2026-08-31"            # a Monday; the canonical key IS this date
REQUIRED_MINUTES = 480
SECONDS_SHORT = 36                    # rounds to 1 minute


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import LeaveRequest, Organization, User

    from app.leave_intervals import LeaveInterval, LeaveIntervalSet

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    ConfigVersion.__table__.create(engine, checkfirst=True)
    ConfigValue.__table__.create(engine, checkfirst=True)
    EcoDayRecord.__table__.create(engine, checkfirst=True)
    # MODEL-EXCLUSION reads approved leave, so the day evaluation now needs the
    # canonical leave tables present.
    LeaveRequest.__table__.create(engine, checkfirst=True)
    LeaveIntervalSet.__table__.create(engine, checkfirst=True)
    LeaveInterval.__table__.create(engine, checkfirst=True)
    from app.eval_day import LeaveInputContribution, LeaveInputSnapshot
    from app.policy_version import PolicyVersion
    from app.break_treatment import BreakTreatmentProfile
    from app.break_classification import (
        BreakClassificationAssertion, ClassificationSnapshot,
        EcoClassificationAuthority,
    )

    PolicyVersion.__table__.create(engine, checkfirst=True)
    for _m in (BreakTreatmentProfile, EcoClassificationAuthority,
               BreakClassificationAssertion, ClassificationSnapshot):
        _m.__table__.create(engine, checkfirst=True)

    LeaveInputSnapshot.__table__.create(engine, checkfirst=True)
    LeaveInputContribution.__table__.create(engine, checkfirst=True)

@pytest.fixture(autouse=True)
def _eco_policy(db):
    """Every recorded day needs a policy (owner ruling).

    ``evaluate_and_record_day`` refuses with POLICY_UNRESOLVED when a Tenant has
    none, so this suite captures one explicitly rather than weakening that rule.
    It carries no requirement: daily_minutes stays with Requirement Resolution,
    which is what these tests actually exercise.
    """
    from tests._eco_policy import ensure_test_policy

    ensure_test_policy(db, organization_id=ORG, commit=False)
    db.flush()


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


def _week_dates(key=MONDAY_WEEK):
    start = date.fromisoformat(key)
    return [start + timedelta(days=i) for i in range(7)]


def _config(db, *, effective_from, minutes=REQUIRED_MINUTES, key=None,
            organization_id=ORG, tier="TENANT_FALLBACK", rank=6):
    """A real configuration version, as first capture would write one."""
    version_key = key or effective_from.isoformat()
    values = [{
        "scope_user_id": None, "tier": tier, "rank": rank,
        "key": REQUIREMENT_KEY, "value_minutes": minutes, "source_ref": None,
    }]
    version = ConfigVersion(
        organization_id=organization_id, version_key=version_key,
        effective_from=effective_from,
        captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        captured_by_label="SYSTEM",
        content_hash=compute_content_hash(
            organization_id=organization_id, version_key=version_key,
            effective_from=effective_from, values=values),
    )
    db.add(version)
    db.flush()
    for value in values:
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=organization_id,
            **value))
    db.flush()
    return version


def _ledger(db, *, on, state=LedgerState.SEALED.value, week=MONDAY_WEEK,
            organization_id=ORG, user_id=USER, version=1, fault=None):
    led = WorkDayLedger(
        organization_id=organization_id, user_id=user_id,
        work_date=datetime(on.year, on.month, on.day),
        workweek_key=week, ledger_version=version, state=state,
        integrity_fault_reason=fault,
        # D4-A: NOT_A_WORKING_DAY implies is_working_day=False. Derived
        # from the state so a fixture cannot assert both that no work was
        # expected and that the day was a working day.
        timezone_iana="Asia/Kolkata", is_working_day=(state != LedgerState.NOT_A_WORKING_DAY.value), working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST",
    )
    db.add(led)
    db.flush()
    return led


def _interval(db, led, *, seconds, state="WRK"):
    begin = datetime(
        led.work_date.year, led.work_date.month, led.work_date.day, 9, 0,
        tzinfo=UTC)
    row = WorkStateInterval(
        ledger_id=led.id, organization_id=led.organization_id, seq=1,
        state=state, start_at_utc=begin,
        end_at_utc=begin + timedelta(seconds=seconds),
        duration_seconds=seconds, provenance="WORKER", created_at=NOW,
    )
    db.add(row)
    db.flush()
    return row


def _worked_day(db, *, on, short=SECONDS_SHORT, **kw):
    led = _ledger(db, on=on, **kw)
    _interval(db, led, seconds=REQUIRED_MINUTES * 60 - short)
    return led


def _record(db, *, on, organization_id=ORG, user_id=USER):
    return evaluate_and_record_day(
        db, organization_id=organization_id, user_id=user_id, work_date=on)


def _rows(db, *, on=None, organization_id=ORG, user_id=USER):
    """Persisted day records.

    ``EcoDayRecord.work_date`` is a ``Date`` column while
    ``WorkDayLedger.work_date`` is a ``DateTime`` — comparing the former to a
    datetime silently matches nothing.
    """
    stmt = sa.select(EcoDayRecord).where(
        EcoDayRecord.organization_id == organization_id,
        EcoDayRecord.user_id == user_id,
    )
    if on is not None:
        stmt = stmt.where(EcoDayRecord.work_date == on)
    return db.execute(stmt.order_by(EcoDayRecord.id)).scalars().all()


def _code_tokens(tree: ast.Module) -> set:
    """Identifiers and runtime strings — docstrings excluded."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    tokens = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.add(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            tokens.add(node.name)
        elif isinstance(node, ast.arg):
            tokens.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            tokens.add(node.arg)
        elif isinstance(node, ast.alias):
            tokens.add(node.asname or node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            tokens.add(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                tokens.add(node.value)
    return tokens


# ═════════════════════════════════════════════════════════════════════════════
# The recording boundary
# ═════════════════════════════════════════════════════════════════════════════
class TestRecordingBoundary:
    def test_a_sealed_day_is_evaluated_and_persisted(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on)

        result = _record(db, on=on)
        assert result.outcome is RecordingOutcome.RECORDED
        assert result.is_recorded
        assert result.state == EcoDayState.SHORTFALL.value
        assert result.record.gross_shortfall_minutes == 1
        assert result.record.contributes_to_accumulator_minutes == 1
        assert result.ledger_state == LedgerState.SEALED.value
        assert result.ledger_version == 1
        assert len(_rows(db, on=on)) == 1

    def test_a_met_requirement_records_no_shortfall(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        led = _ledger(db, on=on)
        _interval(db, led, seconds=REQUIRED_MINUTES * 60)

        result = _record(db, on=on)
        assert result.state == EcoDayState.NO_SHORTFALL.value
        assert result.record.contributes_to_accumulator_minutes == 0

    def test_a_non_working_day_records_a_zeroed_result(self, db):
        """R5 / §24.2: written so the week fold stays complete."""
        on = _week_dates()[5]
        _config(db, effective_from=on - timedelta(days=30))
        _ledger(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)

        result = _record(db, on=on)
        assert result.state == EcoDayState.NOT_A_WORKING_DAY.value
        assert result.record.contributes_to_accumulator_minutes == 0
        assert result.record.required_minutes == 0

    def test_a_missing_clock_out_records_a_deferral(self, db):
        """S2/S3: a deferral is a fact worth recording, and fabricates nothing."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _ledger(db, on=on, state=LedgerState.MISSING_CLOCK_OUT.value)

        result = _record(db, on=on)
        assert result.is_recorded
        assert result.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert result.record.contributes_to_accumulator_minutes == 0
        assert result.record.required_minutes is None
        assert result.record.qualifying_minutes is None

    def test_an_open_day_is_refused_and_nothing_is_written(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _ledger(db, on=on, state=LedgerState.OPEN.value)

        result = _record(db, on=on)
        assert result.outcome is RecordingOutcome.REFUSED
        assert result.reason == REASON_LEDGER_NOT_SEALED
        assert result.record is None
        assert _rows(db, on=on) == []

    def test_a_ledger_integrity_fault_fails_closed(self, db):
        """§24.15: record the fault and refuse the day; never repair it."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _ledger(db, on=on, state=LedgerState.LEDGER_INTEGRITY_FAULT.value,
                fault="overlapping intervals")

        result = _record(db, on=on)
        assert result.reason == REASON_LEDGER_INTEGRITY_FAULT
        assert _rows(db, on=on) == []

    def test_a_day_with_no_ledger_is_refused_not_zeroed(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))

        result = _record(db, on=on)
        assert result.reason == REASON_NO_LEDGER_DAY
        assert result.ledger_state is None
        assert _rows(db, on=on) == []

    def test_a_non_wrk_interval_is_refused_with_the_evaluators_reason(self, db):
        """R6 authorises WRK only; the classifier and Legal Matrix are absent,
        so the day is refused rather than partly qualified."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        led = _ledger(db, on=on)
        _interval(db, led, seconds=1800, state="BRK")

        result = _record(db, on=on)
        assert result.reason == REASON_NON_WRK_STATE_PRESENT
        assert _rows(db, on=on) == []

    def test_an_unconfigured_day_records_requirement_unresolved(self, db):
        """§10.2: no requirement resolved, so no shortfall — but the day is
        still recorded, carrying no minutes at all."""
        on = _week_dates()[0]
        _worked_day(db, on=on)          # no ConfigVersion seeded

        result = _record(db, on=on)
        assert result.is_recorded
        assert result.state == EcoDayState.REQUIREMENT_UNRESOLVED.value
        assert result.record.required_minutes is None
        assert result.record.contributes_to_accumulator_minutes is None
        assert result.record.config_version is None

    def test_the_service_takes_an_identity_and_nothing_else(self):
        """Rulings 3/5/7 held by the signature: no configuration source can be
        injected, and no ledger version can be chosen."""
        params = set(inspect.signature(evaluate_and_record_day).parameters)
        assert params == {"db", "organization_id", "user_id", "work_date"}
        for banned in ("provider", "config_version", "policy_version",
                       "ledger_version", "ledger", "tier", "key",
                       "required_minutes", "intervals"):
            assert banned not in params

    def test_it_performs_no_arithmetic_of_its_own(self):
        """Orchestration only: the evaluator owns every calculation."""
        tokens = _code_tokens(
            ast.parse((EVAL_DAY / "recording.py").read_text(encoding="utf-8")))
        for banned in ("round_half_up_minutes", "duration_seconds",
                       "resolve_requirement_strict", "compute_result_hash",
                       "qualifying_minutes", "gross_shortfall_minutes"):
            assert banned not in tokens, f"recording.py uses {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# Snapshot governance (rulings 3, 4, 5, 7)
# ═════════════════════════════════════════════════════════════════════════════
class TestSnapshotGovernance:
    def test_the_config_version_is_the_one_authoritative_as_of_the_work_date(
        self, db
    ):
        """Ruling 3. Two versions exist; the day resolves the one effective at
        or before it, not the newest in the table."""
        on = _week_dates()[3]
        _config(db, effective_from=on - timedelta(days=30), key="early",
                minutes=REQUIRED_MINUTES)
        _config(db, effective_from=on + timedelta(days=10), key="later",
                minutes=600)
        _worked_day(db, on=on)

        result = _record(db, on=on)
        assert result.record.config_version == "early"
        assert result.record.required_minutes == REQUIRED_MINUTES

    def test_a_day_below_the_historical_floor_resolves_no_version(self, db):
        """First capture cannot be backdated, so days before it stay
        unresolved — fail-closed, never defaulted."""
        floor = _week_dates()[0]
        _config(db, effective_from=floor)
        before = floor - timedelta(days=7)
        _worked_day(db, on=before, week="2026-08-24")

        result = _record(db, on=before)
        assert result.state == EcoDayState.REQUIREMENT_UNRESOLVED.value
        assert result.record.config_version is None

    def test_the_resolved_policy_identity_is_persisted(self, db):
        """This guard inverted when PolicyVersion gained a consumer.

        It previously asserted ``policy_version`` stayed NULL and that
        ``recording.py`` did not mention ``resolve_policy`` — the right claim
        while no policy artifact existed, because the alternative then was
        aliasing ``config_version`` into it and inventing a second meaning for
        one fact. A real PolicyVersion now exists and is resolved historically,
        so the column carries the identity that was actually in force.
        """
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on)

        record = _record(db, on=on).record
        # The value is the captured policy's own key, never the config version.
        from tests._eco_policy import DEFAULT_POLICY_KEY

        assert record.policy_version == DEFAULT_POLICY_KEY
        assert record.policy_version != record.config_version
        tokens = _code_tokens(
            ast.parse((EVAL_DAY / "recording.py").read_text(encoding="utf-8")))
        assert "resolve_policy_as_of" in tokens

    def test_the_service_constructs_the_provider_itself(self):
        """Ruling 5: authority is established at this boundary, so the
        configuration source is not a caller's choice."""
        body = (EVAL_DAY / "recording.py").read_text(encoding="utf-8")
        assert "DatabaseConfigVersionProvider" in body

    def test_no_second_configuration_resolution_algorithm_exists(self):
        """One algorithm, in Step 6. This path calls it."""
        tokens = _code_tokens(
            ast.parse((EVAL_DAY / "recording.py").read_text(encoding="utf-8")))
        for banned in ("version_as_of", "effective_from", "ConfigVersion",
                       "ConfigValue"):
            assert banned not in tokens, f"recording.py resolves {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# A-H: replay, conflict, corrections
# ═════════════════════════════════════════════════════════════════════════════
class TestReplayAndCorrections:
    def test_A_sequential_identical_replay_is_idempotent(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on)

        first = _record(db, on=on)
        second = _record(db, on=on)

        assert first.is_recorded and second.is_recorded
        assert first.record.id == second.record.id
        assert len(_rows(db, on=on)) == 1

    def test_B_concurrent_identical_replay_recovers(self, db, monkeypatch):
        """The race, exercised deterministically.

        True multi-connection concurrency is not reproducible on the shared
        in-memory SQLite engine, so the race is produced by blinding the
        pre-check while the row already exists — which is precisely the state a
        losing writer is in: its SELECT saw nothing, the unique index sees the
        row. The recovery path must return the persisted snapshot rather than
        leaking IntegrityError.
        """
        from app.eval_day import evaluator as ev

        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        led = _worked_day(db, on=on)
        winner = _record(db, on=on)
        assert winner.is_recorded

        monkeypatch.setattr(ev, "_existing", lambda *a, **k: None)
        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=__import__(
                "app.config_versioning", fromlist=["x"]
            ).DatabaseConfigVersionProvider(db),
            # The SAME policy the recorded row was evaluated under — identity
            # AND value. Without the identity the snapshot identity differs and
            # this becomes a new record rather than the conflict the test is
            # about; without the grace the RESULT differs, and the content hash
            # with it, which is the same failure one layer down.
            policy_version=DEFAULT_POLICY_KEY,
            grace_minutes=DEFAULT_GRACE_MINUTES,
            # AC-16: the netting mode joins the content hash as an identity
            # field, so it has to be mirrored here for the same reason the
            # policy identity and the grace are -- this call is reproducing
            # the recording path by hand, and every governing rule it omits
            # produces a different digest and therefore a different record.
            netting_mode="NONE",
        )
        monkeypatch.undo()

        # Blind only the pre-check, leaving the recovery lookup real.
        calls = {"n": 0}
        real_existing = ev._existing

        def _blind_first(db_, ev_, key_):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_existing(db_, ev_, key_)

        monkeypatch.setattr(ev, "_existing", _blind_first)

        recovered = _record_with_empty_leave(db, evaluation)
        assert calls["n"] >= 2, "the recovery lookup must have run"
        assert recovered.id == winner.record.id
        assert len(_rows(db, on=on)) == 1

    def test_B2_a_non_duplicate_integrity_error_is_not_swallowed(self, db,
                                                                 monkeypatch):
        """The recovery is specific. If the snapshot is still absent after a
        failed insert, some other constraint failed and the caller must see it.
        """
        from app.eval_day import evaluator as ev

        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        led = _worked_day(db, on=on)
        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=__import__(
                "app.config_versioning", fromlist=["x"]
            ).DatabaseConfigVersionProvider(db),
            # The SAME policy the recorded row was evaluated under — identity
            # AND value. Without the identity the snapshot identity differs and
            # this becomes a new record rather than the conflict the test is
            # about; without the grace the RESULT differs, and the content hash
            # with it, which is the same failure one layer down.
            policy_version=DEFAULT_POLICY_KEY,
            grace_minutes=DEFAULT_GRACE_MINUTES,
        )
        # A different constraint: state must be non-empty.
        object.__setattr__(evaluation, "requirement_outcome", None)
        monkeypatch.setattr(ev, "_existing", lambda *a, **k: None)

        with pytest.raises(sa.exc.IntegrityError):
            _record_with_empty_leave(db, evaluation)
        db.rollback()

    def test_C_same_identity_different_result_conflicts(self, db):
        """The evaluator itself changed — surfaced, never overwritten."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        led = _worked_day(db, on=on)
        first = _record(db, on=on)
        assert first.is_recorded

        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=__import__(
                "app.config_versioning", fromlist=["x"]
            ).DatabaseConfigVersionProvider(db),
            # The SAME policy the recorded row was evaluated under — identity
            # AND value. Without the identity the snapshot identity differs and
            # this becomes a new record rather than the conflict the test is
            # about; without the grace the RESULT differs, and the content hash
            # with it, which is the same failure one layer down.
            policy_version=DEFAULT_POLICY_KEY,
            grace_minutes=DEFAULT_GRACE_MINUTES,
        )
        object.__setattr__(evaluation, "gross_shortfall_minutes", 99)

        with pytest.raises(DayRecordConflict):
            _record_with_empty_leave(db, evaluation)
        # And the stored row is untouched.
        assert _rows(db, on=on)[0].gross_shortfall_minutes == 1

    def test_C2_the_service_returns_a_conflict_as_a_value(self, db, monkeypatch):
        """A recording boundary reports; it does not raise for a condition a
        caller is expected to handle."""
        from app.eval_day import recording as rec

        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on)

        def _boom(db_, evaluation, **kw):
            raise DayRecordConflict("the evaluator changed")

        monkeypatch.setattr(rec, "record_evaluation", _boom)
        result = _record(db, on=on)
        assert result.outcome is RecordingOutcome.REFUSED
        assert result.reason == REASON_RESULT_CONFLICT
        assert result.record is None

    def test_D_a_new_config_version_produces_a_second_historical_snapshot(
        self, db
    ):
        """Ruling 4: multiple immutable historical snapshots are allowed, and
        neither is silently promoted."""
        week = _week_dates()
        on = week[3]
        _config(db, effective_from=on - timedelta(days=30), key="v1")
        _worked_day(db, on=on)
        first = _record(db, on=on)
        assert first.record.config_version == "v1"

        # A version effective earlier in the SAME open week — the only window
        # in which this is reachable.
        _config(db, effective_from=week[0], key="v2", minutes=600)
        second = _record(db, on=on)

        assert second.is_recorded
        assert second.record.config_version == "v2"
        assert second.record.id != first.record.id
        assert len(_rows(db, on=on)) == 2
        # The earlier snapshot is untouched.
        assert _rows(db, on=on)[0].config_version == "v1"
        assert _rows(db, on=on)[0].required_minutes == REQUIRED_MINUTES

    def test_E_a_ledger_correction_produces_a_new_ledger_version(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        old = _worked_day(db, on=on)
        first = _record(db, on=on)
        assert first.ledger_version == 1

        new = _ledger(db, on=on, version=2)
        _interval(db, new, seconds=REQUIRED_MINUTES * 60)
        old.superseded_by = new.id
        db.flush()

        second = _record(db, on=on)
        assert second.ledger_version == 2
        assert second.record.id != first.record.id
        assert second.state == EcoDayState.NO_SHORTFALL.value
        assert len(_rows(db, on=on)) == 2

    def test_F_a_superseded_ledger_version_is_never_selected(self, db):
        """The service resolves the CURRENT version, so a caller cannot have a
        superseded one recorded as though it were in force."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        old = _worked_day(db, on=on, short=SECONDS_SHORT)
        new = _ledger(db, on=on, version=2)
        _interval(db, new, seconds=REQUIRED_MINUTES * 60)
        old.superseded_by = new.id
        db.flush()

        result = _record(db, on=on)
        assert result.ledger_version == 2
        assert result.record.ledger_version == 2

    def test_G_open_week_re_evaluation_under_changed_config(self, db):
        """Same as D, stated as the reachability boundary: this is the only
        window in which a second snapshot can appear."""
        week = _week_dates()
        on = week[2]
        _config(db, effective_from=week[0], key="w1")
        _worked_day(db, on=on)
        assert _record(db, on=on).record.config_version == "w1"

        _config(db, effective_from=week[1], key="w2", minutes=600)
        assert _record(db, on=on).record.config_version == "w2"
        assert len(_rows(db, on=on)) == 2

    def test_H_a_completed_day_stays_on_its_historical_config(self, db):
        """A version cannot become effective at or before a date in a completed
        week, so ``version_as_of`` for such a day is frozen and re-evaluation
        reproduces the identical snapshot — one row, forever."""
        completed = date(2026, 8, 26)          # in the week before MONDAY_WEEK
        _config(db, effective_from=date(2026, 1, 1), key="historical")
        _worked_day(db, on=completed, week="2026-08-24")

        first = _record(db, on=completed)
        assert first.record.config_version == "historical"

        # A later version, effective after the completed day.
        _config(db, effective_from=_week_dates()[0], key="current",
                minutes=600)
        second = _record(db, on=completed)

        assert second.record.id == first.record.id
        assert second.record.config_version == "historical"
        assert len(_rows(db, on=completed)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# EVAL-WEEK regression contract (§6)
# ═════════════════════════════════════════════════════════════════════════════
class TestEvalWeekContract:
    def _seed_week(self, db, *, short=SECONDS_SHORT):
        week = _week_dates()
        _config(db, effective_from=week[0] - timedelta(days=30))
        for on in week[:5]:
            _worked_day(db, on=on, short=short)
            _record(db, on=on)
        for on in week[5:]:
            _ledger(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
            _record(db, on=on)
        return week

    def _evaluate(self, db, week=MONDAY_WEEK):
        return evaluate_workweek(
            db, organization_id=ORG, user_id=USER, workweek_key=week)

    def test_one_authoritative_record_per_day_contributes_normally(self, db):
        """The whole pipeline, end to end, through persisted records only."""
        self._seed_week(db)
        result = self._evaluate(db)
        assert result.state is WeeklyState.COMPLETE
        assert result.day_count == 7
        assert result.weekly_attributable_shortfall_minutes == 5

    def test_no_record_yields_day_record_missing(self, db):
        week = _week_dates()
        _config(db, effective_from=week[0] - timedelta(days=30))
        for on in week[:6]:
            _ledger(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
            _record(db, on=on)
        _ledger(db, on=week[6], state=LedgerState.OPEN.value)   # refused

        result = self._evaluate(db)
        assert result.state is WeeklyState.DAY_RECORD_MISSING
        assert week[6] in result.blocking_dates

    def test_a_re_evaluation_selects_the_authoritative_record(self, db):
        """Owner Ruling 2, L — the behaviour that changed.

        A configuration correction used to leave two records for one ledger
        version and make the week ``DAY_RECORD_AMBIGUOUS``. Now the second
        supersedes the first, so EVAL-WEEK selects the authoritative one and
        the week completes — while both records are retained.
        """
        week = self._seed_week(db)
        assert self._evaluate(db).state is WeeklyState.COMPLETE

        _config(db, effective_from=week[1], key="mid-week", minutes=600)
        second = _record(db, on=week[3])
        assert second.is_recorded

        result = self._evaluate(db)
        assert result.state is WeeklyState.COMPLETE
        assert second.record.id in result.day_record_ids

        rows = _rows(db, on=week[3])
        assert len(rows) == 2                       # both retained
        assert sum(1 for r in rows if r.superseded_by is None) == 1

    def test_ambiguity_remains_the_safety_net(self, db):
        """``DAY_RECORD_AMBIGUOUS`` is now an invariant violation, not the
        ordinary result of a legitimate re-evaluation.

        Constructed by hand-linking nothing — two authoritative rows for one
        ledger version, which the writer cannot produce — so the net is proved
        to still catch it.
        """
        week = self._seed_week(db)
        on = week[3]
        original = [r for r in _rows(db, on=on) if r.superseded_by is None][0]

        _config(db, effective_from=week[1], key="mid-week", minutes=600)
        _record(db, on=on)

        # Un-supersede the predecessor: an invariant violation by construction.
        db.execute(
            sa.update(EcoDayRecord)
            .where(EcoDayRecord.id == original.id)
            .values(superseded_by=None)
        )
        db.flush()

        result = self._evaluate(db)
        assert result.state is WeeklyState.DAY_RECORD_AMBIGUOUS
        assert on in result.blocking_dates
        assert result.weekly_attributable_shortfall_minutes is None

    def test_a_stale_ledger_version_record_is_ignored(self, db):
        week = self._seed_week(db)
        on = week[0]

        old = db.execute(
            sa.select(WorkDayLedger).where(
                WorkDayLedger.organization_id == ORG,
                WorkDayLedger.user_id == USER,
                WorkDayLedger.work_date == datetime(
                    on.year, on.month, on.day),
                WorkDayLedger.ledger_version == 1,
            )
        ).scalars().one()
        new = _ledger(db, on=on, version=2)
        _interval(db, new, seconds=REQUIRED_MINUTES * 60)
        old.superseded_by = new.id
        db.flush()

        # The v1 record exists but is no longer current; v2 has none yet.
        assert self._evaluate(db).state is WeeklyState.DAY_RECORD_MISSING

        _record(db, on=on)
        result = self._evaluate(db)
        assert result.state is WeeklyState.COMPLETE
        assert set(result.ledger_versions) == {1, 2}
        # v2 met the requirement, so the week lost that day's minute.
        assert result.weekly_attributable_shortfall_minutes == 4

    def test_the_config_version_is_consumed_from_the_persisted_record(self, db):
        self._seed_week(db)
        result = self._evaluate(db)
        stored = {r.config_version for r in _rows(db)}
        assert set(result.config_versions) == stored

    def test_eval_week_resolves_no_configuration(self):
        """Ruling 6, test-enforced and unchanged."""
        for path in sorted(EVAL_WEEK.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("resolve_requirement_strict", "version_as_of",
                           "DatabaseConfigVersionProvider", "resolve_threshold",
                           "app.config_versioning", "app.requirement_resolution",
                           "app.eco_threshold"):
                assert banned not in tokens, f"{path.name} resolves {banned}"

    def test_eval_week_source_is_unchanged_by_this_increment(self):
        """No EVAL-WEEK semantics were modified: its own guard set still holds
        and it still imports work_ledger for membership only."""
        tokens = _code_tokens(
            ast.parse((EVAL_WEEK / "membership.py").read_text(encoding="utf-8")))
        assert "WorkDayLedger" in tokens
        assert "EcoDayRecord" in tokens
        assert "WorkStateInterval" not in tokens


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_no_scheduler_was_introduced(self):
        """Ruling 7: the audit found no scheduler and none is invented."""
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("apscheduler", "celery", "add_job", "BackgroundTasks",
                           "crontab", "schedule"):
                assert banned not in tokens, f"{path.name} uses {banned}"
        assert not list(BACKEND.glob("app/**/eco_scheduler*.py"))

    def test_no_route_or_schema_was_added(self):
        for path in sorted(EVAL_DAY.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            for banned in ("fastapi", "pydantic", "starlette"):
                assert not any(m == banned or m.startswith(banned + ".")
                               for m in imported), f"{path.name}: {banned}"

    def test_nothing_in_the_application_wires_the_recording_boundary(self):
        foundation = (
            "eval_day", "eval_week", "work_ledger", "config_versioning",
            "requirement_resolution", "eco_threshold", "eco_threshold_capture",
            "config_first_capture", "leave_intervals", "worker_context",
            "rollout",
            # The next stage of the same closed pipeline. It reads persisted
            # day records for the D4 correction lineage and freezes their
            # content hashes into the event; it is not a route or a scheduler.
            "eval_event",
            # The GV-17 read contract. It reads persisted day records and the
            # ledger version each was evaluated against, and borrows EVAL-DAY's
            # §19 step-6 predicate rather than restating the §24.6/§24.7
            # boundary a second time. Read-only, and not wired either.
            "eco_day_view",
            # The P0 calculation driver. This guard asserted that NOTHING wired
            # the recording boundary, which was true until an authorized
            # invocation existed -- and §44.2's shadow period cannot produce
            # data without one. The driver CALLS `evaluate_and_record_day` and
            # does nothing else with it: it computes no measurement, forms no
            # state, and reads no field of the returned record beyond the state
            # it must branch on. `test_eco_driver.py` enforces that by AST scan.
            "eco_driver",
        )
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if any(part in foundation for part in path.parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    module = ",".join(a.name for a in node.names)
                if module and "eval_day" in module:
                    offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_no_migration_was_added(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # 86 since MODEL-NETTING added b6d4f8a12c93. EVAL-DAY now DOES own
        # a revision of its own — the §18 step-10 bank credit on
        # eco_day_record — so this guard no longer claims otherwise. What
        # it still asserts is the graph shape and the count, which is what
        # catches an UNEXPLAINED revision.
        # 91 since §24.15's D-9.1 added d9a4c7e35f18 (eco_ledger_incident).
        assert len(list((BACKEND / "alembic" / "versions").glob("*.py"))) == 94
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        # Head moved on again with D-1.1's f4a7c2b91e68, an ordinary
        # single-parent child of P2-D4's e2b6d4f89c35. Still one base, one
        # head — and still none of them added by EVAL-DAY, which is the claim.
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_the_authority_mechanism_is_the_ruled_one(self):
        """Owner Ruling 2, H — and NOT the alternatives it excluded.

        The earlier increment asserted these columns were absent, because
        governance was then deliberately deferred. Ruling H authorized exactly
        ``evaluation_generation`` + ``supersedes`` + ``superseded_by``, and
        ruled OUT a boolean marker and a separate governance table — so both
        halves are pinned here.
        """
        columns = set(EcoDayRecord.__table__.c.keys())
        for required in ("evaluation_generation", "supersedes", "superseded_by",
                         "leave_input_snapshot_key"):
            assert required in columns, f"EcoDayRecord lacks {required}"
        for excluded in ("is_current", "is_authoritative", "governance",
                         "authoritative"):
            assert excluded not in columns, f"EcoDayRecord gained {excluded}"

    def test_authority_is_serialized_by_a_unique_generation_not_a_marker(self):
        """The correction the design validation found.

        A partial unique index on ``superseded_by IS NULL`` cannot serialize a
        pointer — the insert must precede the predecessor's update, which
        transiently leaves two NULLs. So the serializer is a unique constraint
        over (day + generation), the ``WorkDayLedger`` mechanism.
        """
        names = {
            c.name for c in EcoDayRecord.__table__.constraints
            if type(c).__name__ == "UniqueConstraint"
        }
        assert "uq_eco_day_record_generation" in names

        generation = EcoDayRecord.__table__.c.evaluation_generation
        assert generation.nullable is False

    #: ``withdrawn`` is banned in EVAL-DAY because in this programme's
    #: vocabulary it names the WITHDRAWN ZT-WFM-ECO-001 v1.0 draft, and EVAL-DAY
    #: must not reach back into it. Owner Ruling C (A2-narrow) introduced an
    #: unrelated homonym — a withdrawn LEAVE INTERVAL SET, the leave module's
    #: own long-standing term (``withdraw_leave_intervals``) and not §31.1
    #: prohibited terminology. The exemption is one token in one file, and
    #: ``test_the_withdrawn_exemption_is_narrow`` holds it there.
    _WITHDRAWN_EXEMPT = {"exclusion.py"}

    def test_no_threshold_event_or_notification(self):
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            banned_here = ["resolve_threshold", "ECO_THRESHOLD", "crossing",
                           "notification", "notify", "sequence"]
            if path.name not in self._WITHDRAWN_EXEMPT:
                banned_here.append("withdrawn")
            for banned in banned_here:
                assert banned not in tokens, f"{path.name} uses {banned}"

    def test_the_withdrawn_exemption_is_narrow(self):
        """Guard the exemption. Two things must stay true.

        The token is still refused in every OTHER EVAL-DAY module, and the
        exempted file gains nothing else from the banned list — the exemption
        is for one homonym, not a hole in the scope guard.
        """
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            if path.name not in self._WITHDRAWN_EXEMPT:
                assert "withdrawn" not in tokens, f"{path.name} uses withdrawn"
                continue
            for banned in ("resolve_threshold", "ECO_THRESHOLD", "crossing",
                           "notification", "notify", "sequence"):
                assert banned not in tokens, f"{path.name} uses {banned}"

        # And the exempted file is the one the leave ruling authorized.
        assert self._WITHDRAWN_EXEMPT == {"exclusion.py"}
        # The v1 draft is still not referenced from EVAL-DAY by any route.
        for path in sorted(EVAL_DAY.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            assert "ZT-WFM-ECO-001" not in body, f"{path.name} names the draft"

    def test_eco_v1_and_rollout_untouched(self):
        """No rollout COUPLING — read from the code, not the prose.

        Two pre-existing EVAL-DAY docstrings state that "rollout remains OFF",
        which is the opposite of depending on it; a text scan cannot tell those
        apart.
        """
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("app.rollout", "RolloutControl",
                           "EARLY_CLOCK_OUT_FEATURE_ENABLED"):
                assert banned not in tokens, f"{path.name} uses {banned}"

    def test_the_golden_vectors_are_untouched(self):
        import json

        fixture = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        assert data["_vector_count"] == 28
        # Owner-ruled split: exactly these eighteen execute, the other 10 do
        # not. GV-04, GV-05 and GV-08 joined when EVAL-EVENT was implemented;
        # GV-06 and GV-22 joined when ALERT's record/render layer was; GV-17
        # joined when VISIBILITY-SURFACE and VIEW-AUTHORITY closed; GV-27 when
        # WORKER-CONTEXT-WIRING did; GV-25 when POLICY-VERSION was consumed.
        # GV-26 joined on §24.15's platform incident (owner ruling D-9.1):
        # STATE-CONTRACT was satisfied by the existing D-8.1 contract and
        # ALERT was a STALE citation, an incident being operational
        # evidence rather than a notification. The set is now COMPLETE,
        # which is asserted as an exact set so a vector leaving it fails.
        executable = {"GV-01", "GV-02", "GV-03", "GV-04", "GV-05", "GV-06",
                      "GV-07", "GV-08", "GV-09", "GV-10", "GV-11", "GV-12", "GV-13",
                      "GV-14", "GV-15", "GV-16", "GV-17", "GV-18", "GV-19", "GV-20",
                      "GV-21", "GV-22", "GV-23", "GV-24", "GV-25", "GV-26", "GV-27",
                      "GV-28"}
        for vector in data["vectors"]:
            expected = ("executable" if vector["id"] in executable
                        else "blocked_pending_platform_primitive")
            assert vector["harness"]["execution"] == expected, vector["id"]
        assert sum(1 for v in data["vectors"]
                   if v["harness"]["execution"] == "executable") == 28

    def test_no_production_database_is_reachable_from_here(self):
        for path in sorted(EVAL_DAY.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("create_engine", "DATABASE_URL", "postgresql://",
                           "sessionmaker", "load_dotenv"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_the_terminology_guard_is_satisfied(self):
        """Scoped to the module this increment adds.

        Scanning the whole package surfaced a PRE-EXISTING §31.1 hit — the word
        "unauthorised" in the docstrings of ``evaluator.py`` and ``__init__.py``
        — which this increment is not authorized to edit. Reported to the owner
        rather than quietly fixed, and rather than asserted here as though this
        increment were responsible for it.
        """
        from app.early_clock_out import find_prohibited_terms, format_hits, scan_text

        for token in (REASON_NO_LEDGER_DAY, REASON_RESULT_CONFLICT):
            assert find_prohibited_terms(token) == [], token
        path = EVAL_DAY / "recording.py"
        hits = scan_text(path.read_text(encoding="utf-8"))
        assert hits == [], format_hits(hits, source=path.name)


# ═════════════════════════════════════════════════════════════════════════════
# Isolation and the result type
# ═════════════════════════════════════════════════════════════════════════════
class TestIsolationAndResult:
    def test_organization_isolation(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on)
        assert _record(db, on=on).is_recorded
        assert _record(db, on=on, organization_id=OTHER_ORG).reason == (
            REASON_NO_LEDGER_DAY)

    def test_user_isolation(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on)
        assert _record(db, on=on, user_id=OTHER_USER).reason == (
            REASON_NO_LEDGER_DAY)

    def test_a_refusal_cannot_carry_a_record(self):
        with pytest.raises(ValueError):
            DayRecordingResult(
                outcome=RecordingOutcome.REFUSED, organization_id=ORG,
                user_id=USER, work_date=date(2026, 9, 1),
                reason=REASON_NO_LEDGER_DAY, record=object(),
            )

    def test_a_refusal_must_state_a_reason(self):
        with pytest.raises(ValueError):
            DayRecordingResult(
                outcome=RecordingOutcome.REFUSED, organization_id=ORG,
                user_id=USER, work_date=date(2026, 9, 1))

    def test_a_recording_must_carry_the_record(self):
        with pytest.raises(ValueError):
            DayRecordingResult(
                outcome=RecordingOutcome.RECORDED, organization_id=ORG,
                user_id=USER, work_date=date(2026, 9, 1))

    def test_the_result_is_frozen(self):
        result = DayRecordingResult(
            outcome=RecordingOutcome.REFUSED, organization_id=ORG,
            user_id=USER, work_date=date(2026, 9, 1),
            reason=REASON_NO_LEDGER_DAY)
        with pytest.raises(Exception):
            result.reason = None        # type: ignore[misc]
