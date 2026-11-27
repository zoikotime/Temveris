"""
EVAL-WEEK — canonical membership and the weekly evaluator.
==========================================================

The property under test: **a caller cannot change what the week contains.**

The earlier fold took its days from the caller, which made the most dangerous
week — one with a day missing — look exactly like a complete one. So the tests
here are mostly about what the evaluator refuses to be told: the day set comes
from the persisted ledger, the day values come from persisted ``EcoDayRecord``
rows, and the seven calendar dates come from parsing the persisted
``workweek_key`` rather than from any current configuration.

Days are built through the real EVAL-DAY path and persisted with
``record_evaluation`` — never hand-stubbed — so what the weekly evaluator reads
is what the daily evaluator actually stores.

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

from app.config_versioning import InMemoryConfigVersionProvider
from app.eval_day import (
    EcoDayRecord,
    EcoDayState,
    evaluate_day,
    record_evaluation,
)
from app.eval_week import (
    WeekEvaluation,
    WeeklyState,
    canonical_dates,
    evaluate_workweek,
)
from app.requirement_resolution import RequirementCandidate
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = timezone.utc
ORG = 71
OTHER_ORG = 72
USER = 8100
OTHER_USER = 8200

REQUIRED_MINUTES = 525
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eval_week"

# ── Independently transcribed ────────────────────────────────────────────────
# The canonical key format is the local workweek START DATE, ISO-formatted
# (W4-3). 2026-08-31 is a Monday; 2026-08-30 the Sunday before it.
MONDAY_WEEK = "2026-08-31"
SUNDAY_WEEK = "2026-08-30"
DAYS_IN_WEEK = 7


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


def _provider(minutes=REQUIRED_MINUTES):
    p = InMemoryConfigVersionProvider()
    p.capture_version(
        organization_id=ORG, config_version_id="v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=minutes)])
    p.capture_version(
        organization_id=OTHER_ORG, config_version_id="v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=minutes)])
    return p


def _ledger(db, *, on, state=LedgerState.SEALED.value, week=MONDAY_WEEK,
            organization_id=ORG, user_id=USER, version=1):
    led = WorkDayLedger(
        organization_id=organization_id, user_id=user_id,
        work_date=datetime(on.year, on.month, on.day),
        workweek_key=week, ledger_version=version, state=state,
        # D4-A: NOT_A_WORKING_DAY implies is_working_day=False. Derived
        # from the state so a fixture cannot assert both that no work was
        # expected and that the day was a working day.
        timezone_iana="Asia/Kolkata", is_working_day=(state != LedgerState.NOT_A_WORKING_DAY.value), working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST",
    )
    db.add(led)
    db.flush()
    return led


def _seal(db, led, worked_seconds):
    begin = datetime(
        led.work_date.year, led.work_date.month, led.work_date.day, 9, 0,
        tzinfo=UTC)
    row = WorkStateInterval(
        ledger_id=led.id, organization_id=led.organization_id, seq=1,
        state="WRK", start_at_utc=begin,
        end_at_utc=begin + timedelta(seconds=worked_seconds),
        duration_seconds=worked_seconds, provenance="WORKER", created_at=NOW,
    )
    db.add(row)
    db.flush()
    return [row]


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


def _day(db, *, on, worked_seconds=0, state=LedgerState.SEALED.value,
         week=MONDAY_WEEK, organization_id=ORG, user_id=USER, version=1,
         persist=True):
    """A real EVAL-DAY evaluation, persisted the way the platform persists it."""
    led = _ledger(db, on=on, state=state, week=week,
                  organization_id=organization_id, user_id=user_id,
                  version=version)
    intervals = (_seal(db, led, worked_seconds)
                 if worked_seconds and state == LedgerState.SEALED.value else [])
    evaluation = evaluate_day(
        ledger=led, intervals=intervals, provider=_provider())
    if persist:
        _record_with_empty_leave(db, evaluation)
        db.flush()
    return led, evaluation


def _week_dates(key=MONDAY_WEEK):
    start = date.fromisoformat(key)
    return [start + timedelta(days=i) for i in range(DAYS_IN_WEEK)]


def _full_week(db, *, short_seconds=0, week=MONDAY_WEEK, user_id=USER,
               organization_id=ORG):
    """Five sealed working days plus two non-working days — a real week."""
    dates = _week_dates(week)
    out = []
    for on in dates[:5]:
        out.append(_day(
            db, on=on,
            worked_seconds=REQUIRED_MINUTES * 60 - short_seconds,
            week=week, user_id=user_id, organization_id=organization_id))
    for on in dates[5:]:
        out.append(_day(
            db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value,
            week=week, user_id=user_id, organization_id=organization_id))
    return out


def _evaluate(db, week=MONDAY_WEEK, user_id=USER, organization_id=ORG):
    return evaluate_workweek(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=week)


def _code_tokens(tree: ast.Module) -> set:
    """Identifiers and runtime strings — docstrings excluded.

    The same helper the reader and capture suites use: a text scan cannot tell
    "calls X" from "deliberately does not call X", and these docstrings do a
    great deal of the second.
    """
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
# The canonical seven dates
# ═════════════════════════════════════════════════════════════════════════════
class TestCanonicalDates:
    def test_the_key_is_the_start_date_and_the_week_is_seven_dates(self):
        dates = canonical_dates(MONDAY_WEEK)
        assert len(dates) == DAYS_IN_WEEK
        assert dates[0] == date(2026, 8, 31)
        assert dates[-1] == date(2026, 9, 6)
        assert [d.isoformat() for d in dates][0] == MONDAY_WEEK

    def test_a_sunday_start_organization_gets_a_different_week(self):
        """The same physical week, a different canonical key — which is the
        whole reason ISO week notation was rejected in W4-3."""
        assert canonical_dates(SUNDAY_WEEK)[0] == date(2026, 8, 30)
        assert canonical_dates(SUNDAY_WEEK)[-1] == date(2026, 9, 5)
        assert canonical_dates(SUNDAY_WEEK) != canonical_dates(MONDAY_WEEK)

    @pytest.mark.parametrize("bad", [
        "2026-W36", "", "not-a-date", "2026-13-01", "20260831", None, 20260831,
        "2026-08-31T00:00:00",
    ])
    def test_an_uncanonical_key_is_refused_not_guessed(self, bad):
        assert canonical_dates(bad) is None

    def test_the_derivation_reads_no_configuration(self):
        """It must parse the persisted key, never call week_start_date().

        ``week_start_date()`` reads CURRENT configuration — right at seal time,
        wrong for re-deriving a historical week.

        Read from the code, not the file text: the docstring explains at length
        which helpers are deliberately NOT called, and a text scan cannot tell
        that from calling them.
        """
        tokens = _code_tokens(
            ast.parse((PACKAGE / "membership.py").read_text(encoding="utf-8")))
        assert "fromisoformat" in tokens
        for banned in ("week_start_date", "isocalendar", "schedule_policy",
                       "app_settings", "resolve_work_schedule"):
            assert banned not in tokens, f"membership.py uses {banned}"

    def test_it_needs_no_session(self):
        """Pure date arithmetic: a DST week is 169 hours and still 7 dates."""
        assert "db" not in inspect.signature(canonical_dates).parameters


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 1 — NOT_A_WORKING_DAY membership
# ═════════════════════════════════════════════════════════════════════════════
class TestNonWorkingDayMembership:
    def test_a_real_week_with_non_working_days_completes(self, db):
        """The blocker ruling 1 closed. Every real week contains one."""
        _full_week(db)
        result = _evaluate(db)

        assert result.state is WeeklyState.COMPLETE
        assert result.is_complete
        assert result.day_count == DAYS_IN_WEEK
        assert result.weekly_attributable_shortfall_minutes == 0

    def test_a_non_working_day_contributes_exactly_zero(self, db):
        """Five days 36s short each round to 1 apiece; the two non-working days
        add nothing, so the week is 5 — not 7, and not unresolved."""
        _full_week(db, short_seconds=36)
        result = _evaluate(db)
        assert result.state is WeeklyState.COMPLETE
        assert result.weekly_attributable_shortfall_minutes == 5

    def test_it_stays_visible_in_the_weekly_state_set(self, db):
        """Ruling 1: membership without participation, but never invisible."""
        _full_week(db, short_seconds=36)
        result = _evaluate(db)
        assert EcoDayState.NOT_A_WORKING_DAY.value in result.observed_states
        assert EcoDayState.SHORTFALL.value in result.observed_states

    def test_it_creates_no_shortfall_of_its_own(self, db):
        dates = _week_dates()
        for on in dates:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        result = _evaluate(db)
        assert result.state is WeeklyState.COMPLETE
        assert result.weekly_attributable_shortfall_minutes == 0
        assert result.observed_states == (EcoDayState.NOT_A_WORKING_DAY.value,)


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 2 — a missing EcoDayRecord
# ═════════════════════════════════════════════════════════════════════════════
class TestMissingDayRecord:
    def test_a_day_without_a_record_blocks_the_week(self, db):
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        # A ledger day exists; its evaluation was never recorded.
        _day(db, on=dates[6], state=LedgerState.NOT_A_WORKING_DAY.value,
             persist=False)

        result = _evaluate(db)
        assert result.state is WeeklyState.DAY_RECORD_MISSING
        assert result.weekly_attributable_shortfall_minutes is None
        assert result.blocking_dates == (dates[6],)

    def test_it_is_not_treated_as_zero_or_as_a_non_working_day(self, db):
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, worked_seconds=REQUIRED_MINUTES * 60 - 36)
        _day(db, on=dates[6], worked_seconds=REQUIRED_MINUTES * 60 - 36,
             persist=False)

        result = _evaluate(db)
        # Six recorded days would have summed to 6. It refuses instead.
        assert result.weekly_attributable_shortfall_minutes is None
        assert result.state is not WeeklyState.COMPLETE

    def test_an_unsealed_day_surfaces_as_a_missing_record(self, db):
        """EVAL-DAY writes no record for an OPEN ledger day, so the week must
        not quietly exclude it — exactly the case a caller-supplied day list
        could have hidden."""
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        _ledger(db, on=dates[6], state=LedgerState.OPEN.value)

        result = _evaluate(db)
        assert result.state is WeeklyState.DAY_RECORD_MISSING
        assert dates[6] in result.blocking_dates

    def test_a_ledger_integrity_fault_day_surfaces_the_same_way(self, db):
        """§24.15 refuses the day rather than repairing it, so no record
        exists — and the week must fail closed rather than skip it."""
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        _ledger(db, on=dates[6],
                state=LedgerState.LEDGER_INTEGRITY_FAULT.value)

        result = _evaluate(db)
        assert result.state is WeeklyState.DAY_RECORD_MISSING

    def test_a_record_for_a_superseded_ledger_version_does_not_count(self, db):
        """The authoritative record is the one matching the CURRENT ledger
        version. A record left behind by an earlier version is history."""
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)

        old = _ledger(db, on=dates[6],
                      state=LedgerState.NOT_A_WORKING_DAY.value, version=1)
        evaluation = evaluate_day(ledger=old, intervals=[], provider=_provider())
        _record_with_empty_leave(db, evaluation)
        new = _ledger(db, on=dates[6],
                      state=LedgerState.NOT_A_WORKING_DAY.value, version=2)
        old.superseded_by = new.id
        db.flush()

        result = _evaluate(db)
        assert result.state is WeeklyState.DAY_RECORD_MISSING
        assert dates[6] in result.blocking_dates


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 3 — the evaluator owns its day set
# ═════════════════════════════════════════════════════════════════════════════
class TestMembershipIsDerived:
    def test_the_signature_accepts_no_day_list(self):
        params = set(inspect.signature(evaluate_workweek).parameters)
        assert params == {"db", "organization_id", "user_id", "workweek_key"}
        for banned in ("days", "day_evaluations", "records", "dates",
                       "day_records", "week"):
            assert banned not in params

    def test_a_caller_cannot_omit_a_missing_day(self, db):
        """The defect ruling 3 closes, stated as a test.

        There is no argument through which a caller could drop the seventh
        day, so the only way to learn about the week is to be told the truth
        about it.
        """
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, worked_seconds=REQUIRED_MINUTES * 60 - 36)
        _ledger(db, on=dates[6], state=LedgerState.OPEN.value)

        assert _evaluate(db).state is WeeklyState.DAY_RECORD_MISSING
        # ... and there is no second call shape that returns a total.
        with pytest.raises(TypeError):
            evaluate_workweek(
                db, organization_id=ORG, user_id=USER,
                workweek_key=MONDAY_WEEK,
                day_evaluations=[],            # type: ignore[call-arg]
            )

    def test_superseded_ledger_rows_are_not_members(self, db):
        """Corrections supersede rather than mutate, so only the current
        version of a day is in force."""
        dates = _week_dates()
        for on in dates:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)

        stale = _ledger(db, on=dates[0], state=LedgerState.OPEN.value,
                        version=2)
        # Point it at itself as superseded so it is history, not current.
        stale.superseded_by = stale.id
        db.flush()

        result = _evaluate(db)
        assert result.state is WeeklyState.COMPLETE
        assert result.day_count == DAYS_IN_WEEK

    def test_a_day_whose_key_moved_leaves_the_old_week(self, db):
        """W4: ``supersede_day()`` re-derives the key; the superseded row keeps
        the original. A day cannot be in two weeks."""
        dates = _week_dates()
        for on in dates:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        assert _evaluate(db).day_count == DAYS_IN_WEEK

        moved = _day(db, on=dates[0], state=LedgerState.NOT_A_WORKING_DAY.value,
                     week=SUNDAY_WEEK, version=2)[0]
        current = db.execute(
            sa.select(WorkDayLedger).where(
                WorkDayLedger.workweek_key == MONDAY_WEEK,
                WorkDayLedger.work_date == datetime(
                    dates[0].year, dates[0].month, dates[0].day),
                WorkDayLedger.ledger_version == 1,
            )
        ).scalars().one()
        current.superseded_by = moved.id
        db.flush()

        assert _evaluate(db).day_count == DAYS_IN_WEEK - 1

    def test_no_ledger_day_is_not_a_zero_week(self, db):
        """"Nothing recorded" and "days did not load" are indistinguishable
        from an empty result, and one of them is a defect."""
        result = _evaluate(db)
        assert result.state is WeeklyState.NO_LEDGER_DAYS
        assert result.weekly_attributable_shortfall_minutes is None

    def test_a_malformed_key_is_refused(self, db):
        result = _evaluate(db, week="2026-W36")
        assert result.state is WeeklyState.MALFORMED_WORKWEEK_KEY
        assert result.canonical_dates == ()
        assert result.weekly_attributable_shortfall_minutes is None

    def test_a_ledger_day_outside_its_own_week_is_refused(self, db):
        """A mis-keyed row must be reported, never folded into the wrong
        week: §24.15's principle is record and refuse, not repair."""
        for on in _week_dates():
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        stray = date(2026, 9, 20)
        _day(db, on=stray, state=LedgerState.NOT_A_WORKING_DAY.value)

        result = _evaluate(db)
        assert result.state is WeeklyState.LEDGER_MEMBERSHIP_INCONSISTENT
        assert stray in result.blocking_dates

    def test_two_current_rows_for_one_date_are_refused(self, db):
        for on in _week_dates():
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        # A second current row for the same date: neither is superseded.
        _day(db, on=_week_dates()[0],
             state=LedgerState.NOT_A_WORKING_DAY.value, version=2)

        result = _evaluate(db)
        assert result.state is WeeklyState.LEDGER_MEMBERSHIP_INCONSISTENT


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 5 — the weekly result states
# ═════════════════════════════════════════════════════════════════════════════
class TestWeeklyStates:
    def _week_with(self, db, *, state, on_index=6):
        dates = _week_dates()
        for i, on in enumerate(dates):
            if i == on_index:
                _day(db, on=on, state=state)
            else:
                _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        return dates

    def test_a_deferred_day_blocks_the_week(self, db):
        dates = self._week_with(db, state=LedgerState.MISSING_CLOCK_OUT.value)
        result = _evaluate(db)
        assert result.state is WeeklyState.DEFERRED_DAY_PRESENT
        assert result.weekly_attributable_shortfall_minutes is None
        assert result.blocking_dates == (dates[6],)
        assert EcoDayState.DEFERRED_PENDING_LEDGER.value in result.observed_states

    def test_a_requirement_unresolved_day_blocks_the_week(self, db):
        """No configuration covers the date, so §10.2 forbids a weekly total
        just as it forbids a daily one."""
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        led = _ledger(db, on=dates[6])
        _seal(db, led, REQUIRED_MINUTES * 60)
        empty = InMemoryConfigVersionProvider()
        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=empty)
        assert evaluation.state is EcoDayState.REQUIREMENT_UNRESOLVED
        _record_with_empty_leave(db, evaluation)
        db.flush()

        result = _evaluate(db)
        assert result.state is WeeklyState.REQUIREMENT_UNRESOLVED
        assert result.weekly_attributable_shortfall_minutes is None

    def test_a_deferred_day_outranks_an_unresolved_one(self, db):
        """Re-sealing a ledger is something an operator can actually do."""
        dates = _week_dates()
        for on in dates[:5]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        _day(db, on=dates[5], state=LedgerState.MISSING_CLOCK_OUT.value)

        led = _ledger(db, on=dates[6])
        _seal(db, led, REQUIRED_MINUTES * 60)
        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=InMemoryConfigVersionProvider())
        _record_with_empty_leave(db, evaluation)
        db.flush()

        assert _evaluate(db).state is WeeklyState.DEFERRED_DAY_PRESENT

    def test_a_missing_record_outranks_a_deferred_day(self, db):
        """A week whose membership is not fully evaluated cannot be diagnosed
        at day level yet."""
        dates = _week_dates()
        for on in dates[:5]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
        _day(db, on=dates[5], state=LedgerState.MISSING_CLOCK_OUT.value)
        _ledger(db, on=dates[6], state=LedgerState.OPEN.value)

        assert _evaluate(db).state is WeeklyState.DAY_RECORD_MISSING

    def test_a_re_evaluated_day_selects_the_authoritative_record(self, db):
        """Owner Ruling 2, L.

        Two records for one ledger version is now the ORDINARY outcome of a
        legitimate re-evaluation under a different configuration version. The
        second supersedes the first, so the reader selects the authoritative
        one instead of refusing — and it does so with a predicate, not by
        resolving configuration or comparing snapshots.
        """
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)

        led = _ledger(db, on=dates[6])
        _seal(db, led, REQUIRED_MINUTES * 60 - 36)
        intervals = list(db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all())
        first = evaluate_day(ledger=led, intervals=intervals,
                             provider=_provider())
        first_row = _record_with_empty_leave(db, first)
        db.flush()

        # A second record for the same day under a different config version.
        second = InMemoryConfigVersionProvider()
        second.capture_version(
            organization_id=ORG, config_version_id="v2",
            effective_from=date(2026, 2, 1),
            candidates=[RequirementCandidate(
                tier="SHIFT", rank=2, minutes=REQUIRED_MINUTES)])
        again = evaluate_day(ledger=led, intervals=intervals, provider=second)
        second_row = _record_with_empty_leave(db, again)
        db.flush()

        assert second_row.id != first_row.id
        assert first_row.superseded_by == second_row.id
        assert second_row.superseded_by is None

        result = _evaluate(db)
        assert result.state is WeeklyState.COMPLETE
        assert second_row.id in result.day_record_ids
        assert first_row.id not in result.day_record_ids

    def test_two_authoritative_records_still_refuse(self, db):
        """The safety net, unchanged in strength.

        ``DAY_RECORD_AMBIGUOUS`` now means the one-authoritative-row invariant
        is broken rather than "somebody re-evaluated a day" — so it is
        constructed by breaking that invariant directly, which the writer
        cannot do.
        """
        dates = _week_dates()
        for on in dates[:6]:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)

        led = _ledger(db, on=dates[6])
        _seal(db, led, REQUIRED_MINUTES * 60 - 36)
        intervals = list(db.execute(sa.select(WorkStateInterval).where(
            WorkStateInterval.ledger_id == led.id)).scalars().all())
        first_row = _record_with_empty_leave(
            db, evaluate_day(ledger=led, intervals=intervals,
                             provider=_provider()))
        second = InMemoryConfigVersionProvider()
        second.capture_version(
            organization_id=ORG, config_version_id="v2",
            effective_from=date(2026, 2, 1),
            candidates=[RequirementCandidate(
                tier="SHIFT", rank=2, minutes=REQUIRED_MINUTES)])
        _record_with_empty_leave(
            db, evaluate_day(ledger=led, intervals=intervals, provider=second))
        db.flush()

        db.execute(
            sa.update(EcoDayRecord)
            .where(EcoDayRecord.id == first_row.id)
            .values(superseded_by=None)
        )
        db.flush()

        result = _evaluate(db)
        assert result.state is WeeklyState.DAY_RECORD_AMBIGUOUS
        assert result.weekly_attributable_shortfall_minutes is None

    def test_the_mandated_states_are_all_distinct(self):
        """Ruling 5: they must not collapse into one generic error."""
        mandated = {
            WeeklyState.COMPLETE.value,
            WeeklyState.DEFERRED_DAY_PRESENT.value,
            WeeklyState.REQUIREMENT_UNRESOLVED.value,
            WeeklyState.DAY_RECORD_MISSING.value,
        }
        assert len(mandated) == 4
        assert mandated <= {s.value for s in WeeklyState}

    def test_no_weekly_state_uses_event_vocabulary(self):
        for state in WeeklyState:
            for banned in ("REACHED", "WITHDRAWN", "PRIMARY", "CROSS",
                           "SEQUENCE", "NOTIF", "THRESHOLD"):
                assert banned not in state.value.upper(), state.value


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 4 — W, and the one rounding locus
# ═════════════════════════════════════════════════════════════════════════════
class TestWeeklyAccumulator:
    def test_w_is_the_sum_of_daily_rounded_contributions(self, db):
        _full_week(db, short_seconds=36)
        contributions = [
            r.contributes_to_accumulator_minutes
            for r in db.execute(
                sa.select(EcoDayRecord)
                .where(EcoDayRecord.organization_id == ORG,
                       EcoDayRecord.user_id == USER)
                .order_by(EcoDayRecord.work_date)
            ).scalars().all()
        ]
        assert contributions == [1, 1, 1, 1, 1, 0, 0]
        assert _evaluate(db).weekly_attributable_shortfall_minutes == sum(
            contributions)

    def test_no_second_rounding(self, db):
        """Five days 36s short: 1 apiece, 5 in total. Rounding the summed
        seconds once at week end would give 3 — the defect GV-12 exists for."""
        _full_week(db, short_seconds=36)
        assert _evaluate(db).weekly_attributable_shortfall_minutes == 5
        assert round((36 * 5) / 60) == 3

    def test_it_reads_no_seconds_and_recomputes_nothing(self):
        """Read from the code: the docstrings name these precisely because the
        package must not use them."""
        for path in sorted(PACKAGE.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("duration_seconds", "round_half_up_minutes",
                           "WorkStateInterval", "app._worked",
                           "resolve_requirement_strict", "qualifying_minutes"):
                assert banned not in tokens, f"{path.name} uses {banned}"

    def test_the_order_of_days_cannot_change_w(self, db):
        """Integer addition, and the day set is derived — so there is no
        caller-visible order at all."""
        _full_week(db, short_seconds=36)
        first = _evaluate(db).weekly_attributable_shortfall_minutes
        assert _evaluate(db).weekly_attributable_shortfall_minutes == first

    def test_it_stops_at_w(self):
        """``W >= T`` is EVAL-EVENT's by a closed ruling."""
        for path in sorted(PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("resolve_threshold", "ThresholdOutcome",
                           "eco_threshold", "ECO_THRESHOLD"):
                assert banned not in body, f"{path.name} uses {banned}"

    def test_nothing_is_written(self, db):
        """A pure evaluator (ruling 6): no weekly row, no mutation."""
        _full_week(db, short_seconds=36)
        before = db.execute(
            sa.select(sa.func.count()).select_from(EcoDayRecord)).scalar()
        _evaluate(db)
        after = db.execute(
            sa.select(sa.func.count()).select_from(EcoDayRecord)).scalar()
        assert before == after


# ═════════════════════════════════════════════════════════════════════════════
# Ruling 6/7 — identity, provenance, isolation
# ═════════════════════════════════════════════════════════════════════════════
class TestIdentityAndProvenance:
    def test_the_identity_is_organization_user_and_workweek_key(self, db):
        _full_week(db)
        result = _evaluate(db)
        assert result.organization_id == ORG
        assert result.user_id == USER
        assert result.workweek_key == MONDAY_WEEK

    def test_provenance_is_reported(self, db):
        _full_week(db, short_seconds=36)
        result = _evaluate(db)
        assert len(result.day_record_ids) == DAYS_IN_WEEK
        assert len(set(result.day_record_ids)) == DAYS_IN_WEEK
        assert set(result.ledger_versions) == {1}
        assert len(result.config_versions) == DAYS_IN_WEEK
        assert result.canonical_dates == tuple(_week_dates())

    def test_policy_version_is_preserved_as_persisted_including_null(self, db):
        """Ruling 7: no policy-version subsystem, no invented value."""
        _full_week(db)
        result = _evaluate(db)
        assert set(result.policy_versions) == {None}
        for path in sorted(PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            assert "resolve_policy" not in body

    def test_another_worker_is_a_different_week(self, db):
        _full_week(db, short_seconds=36, user_id=USER)
        assert _evaluate(db, user_id=OTHER_USER).state is (
            WeeklyState.NO_LEDGER_DAYS)
        assert _evaluate(db, user_id=USER).state is WeeklyState.COMPLETE

    def test_another_organization_is_a_different_week(self, db):
        _full_week(db, short_seconds=36)
        assert _evaluate(db, organization_id=OTHER_ORG).state is (
            WeeklyState.NO_LEDGER_DAYS)

    def test_another_workweek_key_is_a_different_week(self, db):
        _full_week(db, short_seconds=36, week=MONDAY_WEEK)
        assert _evaluate(db, week=SUNDAY_WEEK).state is (
            WeeklyState.NO_LEDGER_DAYS)

    def test_two_organizations_do_not_blend(self, db):
        _full_week(db, short_seconds=36, organization_id=ORG)
        _full_week(db, short_seconds=0, organization_id=OTHER_ORG,
                   user_id=OTHER_USER)
        assert _evaluate(db, organization_id=ORG).\
            weekly_attributable_shortfall_minutes == 5
        assert _evaluate(db, organization_id=OTHER_ORG,
                         user_id=OTHER_USER).\
            weekly_attributable_shortfall_minutes == 0


# ═════════════════════════════════════════════════════════════════════════════
# The result type
# ═════════════════════════════════════════════════════════════════════════════
class TestResultType:
    def test_an_unresolved_week_cannot_carry_a_total(self):
        with pytest.raises(ValueError):
            WeekEvaluation(
                state=WeeklyState.DAY_RECORD_MISSING, organization_id=ORG,
                user_id=USER, workweek_key=MONDAY_WEEK,
                weekly_attributable_shortfall_minutes=5,
            )

    def test_a_complete_week_must_carry_a_total(self):
        with pytest.raises(ValueError):
            WeekEvaluation(
                state=WeeklyState.COMPLETE, organization_id=ORG,
                user_id=USER, workweek_key=MONDAY_WEEK,
            )

    def test_a_complete_week_has_no_blocking_dates(self):
        with pytest.raises(ValueError):
            WeekEvaluation(
                state=WeeklyState.COMPLETE, organization_id=ORG,
                user_id=USER, workweek_key=MONDAY_WEEK,
                weekly_attributable_shortfall_minutes=0,
                blocking_dates=(date(2026, 9, 1),),
            )

    def test_zero_is_a_valid_complete_total(self):
        result = WeekEvaluation(
            state=WeeklyState.COMPLETE, organization_id=ORG, user_id=USER,
            workweek_key=MONDAY_WEEK,
            weekly_attributable_shortfall_minutes=0,
        )
        assert result.is_complete
        assert result.weekly_attributable_shortfall_minutes == 0

    def test_the_result_is_frozen(self):
        result = WeekEvaluation(
            state=WeeklyState.NO_LEDGER_DAYS, organization_id=ORG,
            user_id=USER, workweek_key=MONDAY_WEEK,
        )
        with pytest.raises(Exception):
            result.state = WeeklyState.COMPLETE   # type: ignore[misc]


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_no_weekly_table_or_migration(self):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = {getattr(b, "id", None) or getattr(b, "attr", None)
                             for b in node.bases}
                    assert "Base" not in bases, f"{path.name} declares a model"
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        assert getattr(t, "id", None) != "__tablename__"

        revisions = list((BACKEND / "alembic" / "versions").glob("*.py"))
        # 76 since MODEL-WORKWEEK added b2e6d9f04a17 (shift_attribution_rule on
        # the ledger, R-12.3.4 / §35) — a day-record column, not a weekly table,
        # which is what this guard actually protects.
        # 91 since §24.15's D-9.1 added d9a4c7e35f18 (eco_ledger_incident).
        assert len(revisions) == 94
        for path in revisions:
            body = path.read_text(encoding="utf-8")
            for banned in ("eco_week", "weekly_record", "eco_weekly_record"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_nothing_in_the_application_wires_this_package(self):
        foundation = (
            "eval_week", "eval_day", "work_ledger", "config_versioning",
            "requirement_resolution", "eco_threshold", "eco_threshold_capture",
            "config_first_capture", "leave_intervals", "worker_context",
            "rollout",
            # EVAL-EVENT consumes W from this package — the authorised
            # composition this package's own docstring names. Foundation, not
            # application: it is not a route, a service or a scheduler.
            "eval_event",
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
                if module and "eval_week" in module:
                    offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_no_event_or_notification_identifier(self):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
            }
            for banned in ("threshold", "crossing", "withdraw", "sequence",
                           "event", "notif", "advisory"):
                assert not {n for n in names if banned in n.lower()}, \
                    f"{path.name}: {banned}"

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
        for path in sorted(PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("create_engine", "DATABASE_URL", "postgresql://",
                           "sessionmaker", "load_dotenv"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_eco_v1_and_rollout_are_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
        for path in sorted(PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("rollout", "EARLY_CLOCK_OUT_FEATURE_ENABLED"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_the_terminology_guard_is_satisfied(self):
        from app.early_clock_out import find_prohibited_terms, format_hits, scan_text

        for state in WeeklyState:
            assert find_prohibited_terms(state.value) == [], state.value
        for path in sorted(PACKAGE.glob("*.py")):
            hits = scan_text(path.read_text(encoding="utf-8"))
            assert hits == [], format_hits(hits, source=path.name)
