"""
EVAL-WEEK minimal fold — tests.
===============================

The property under test: the week is the sum of already-rounded daily
contributions, or it is not final. There is no partial week.

GV-12 is executed against the locked artifact — its expected values are read at
test time, never restated here, and the production code contains no hard-coded
answer.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.config_versioning import InMemoryConfigVersionProvider
from app.eval_day import (
    DayEvaluation,
    EcoDayState,
    evaluate_day,
    round_half_up_minutes,
)
from app.eval_week import (
    REASON_DEFERRED_DAY,
    REASON_NO_DAYS,
    REASON_PARTICIPATION_NOT_ESTABLISHED,
    REASON_UNRESOLVED_DAY,
    WeekFold,
    fold_week,
)
from app.requirement_resolution import RequirementCandidate
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = timezone.utc
ORG = 61
REQUIRED_MINUTES = 525            # written literally
SECONDS_SHORT = 36                # GV-12's amended, concrete stated deficit
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eval_week"
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)


_uid = [9000]


def _provider(minutes=REQUIRED_MINUTES):
    p = InMemoryConfigVersionProvider()
    p.capture_version(
        organization_id=ORG, config_version_id="v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="SHIFT", rank=2, minutes=minutes)])
    return p


def _day(db, *, on, worked_seconds, state=LedgerState.SEALED.value):
    """One real EVAL-DAY evaluation — not a hand-built stub."""
    _uid[0] += 1
    led = WorkDayLedger(
        organization_id=ORG, user_id=_uid[0],
        work_date=datetime(on.year, on.month, on.day),
        workweek_key="2026-W36", ledger_version=1, state=state,
        # D4-A: NOT_A_WORKING_DAY implies is_working_day=False. Derived
        # from the state so a fixture cannot assert both that no work was
        # expected and that the day was a working day.
        timezone_iana="Asia/Kolkata", is_working_day=(state != LedgerState.NOT_A_WORKING_DAY.value), working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST",
    )
    db.add(led)
    db.flush()
    intervals = []
    if worked_seconds and state == LedgerState.SEALED.value:
        begin = datetime(on.year, on.month, on.day, 9, 0, tzinfo=UTC)
        row = WorkStateInterval(
            ledger_id=led.id, organization_id=ORG, seq=1, state="WRK",
            start_at_utc=begin, end_at_utc=begin + timedelta(seconds=worked_seconds),
            duration_seconds=worked_seconds, provenance="WORKER", created_at=NOW,
        )
        db.add(row)
        db.flush()
        intervals = [row]
    return evaluate_day(ledger=led, intervals=intervals, provider=_provider())


def _short_day(db, on):
    return _day(db, on=on,
                worked_seconds=REQUIRED_MINUTES * 60 - SECONDS_SHORT)


def _vector(vid):
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == vid:
            return v
    raise AssertionError(vid)


# ═════════════════════════════════════════════════════════════════════════════
# GV-12 — executed against the locked artifact
# ═════════════════════════════════════════════════════════════════════════════
class TestGV12:
    WEEK = [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2),
            date(2026, 9, 3), date(2026, 9, 4)]

    def test_gv12_daily_and_weekly(self, db):
        """Five days each 30-45s short: daily [1,1,1,1,1], weekly 5.

        Expected values come from the locked fixture, not from this file.
        """
        expected = _vector("GV-12")["expected_fields"]
        days = [_short_day(db, d) for d in self.WEEK]

        assert [d.gross_shortfall_minutes for d in days] == \
            expected["daily_shortfall_minutes"]

        fold = fold_week(days)
        assert fold.is_complete is True
        assert fold.weekly_total_minutes == expected["weekly_total_minutes"]

    def test_rounding_once_at_week_end_would_give_the_wrong_answer(self, db):
        """The defect GV-12 exists to catch, demonstrated rather than asserted.

        Summing the shortfall SECONDS and rounding once produces the vector's
        recorded incorrect value; the fold produces the correct one.
        """
        expected = _vector("GV-12")["expected_fields"]
        total_short_seconds = SECONDS_SHORT * len(self.WEEK)

        assert round_half_up_minutes(total_short_seconds) == \
            expected["incorrect_result_if_rounded_once_at_week_end"]

        days = [_short_day(db, d) for d in self.WEEK]
        assert fold_week(days).weekly_total_minutes == \
            expected["weekly_total_minutes"]
        assert expected["weekly_total_minutes"] != \
            expected["incorrect_result_if_rounded_once_at_week_end"]

    def test_the_fixture_is_read_not_restated(self):
        v = _vector("GV-12")
        assert v["expected_fields"]["weekly_total_minutes"] == 5
        assert v["expected_fields"]["daily_shortfall_minutes"] == [1, 1, 1, 1, 1]
        # Promoted by the owner's Option A ruling, once the vector's input was
        # amended to a concrete deficit. This class is its execution.
        assert v["harness"]["execution"] == "executable"
        assert v["harness"]["blocking_dependencies"] == []

    def test_the_fixture_seconds_are_the_vector_s_stated_deficit(self):
        """The amended vector states a CONCRETE deficit, so the constant this
        file uses must be that number rather than merely inside a range.

        The old wording was "30-45s short", which was not satisfiable across
        its whole range: at 42-45s the vector's own week-end counterfactual is
        4, not 3. The owner amended it to 36s.
        """
        stated = _vector("GV-12")["inputs_verbatim"]
        assert f"{SECONDS_SHORT}s short" in stated, stated
        assert SECONDS_SHORT == 36

    def test_thirty_seconds_short_is_zero_under_the_owner_ruling(self, db):
        """The operand, pinned. GV-12's own 36s scenario CANNOT pin it.

        Owner ruling, 2026-09-10 — OPTION A, QUALIFYING-TIME ROUNDING: ECO v2
        rounds per-day qualifying SECONDS to minutes, then subtracts minutes.
        The two candidate operands agree everywhere except at a deficit of
        exactly 30 seconds, so this is the only assertion that distinguishes
        them:

            qualifying   479m30s  ->  ROUND_HALF_UP  ->  480m
            required     480m
            shortfall    0m                    (rounding the shortfall: 1m)

        If the implementation is ever changed to round the raw shortfall
        instead, this fails and GV-12's own five days do not.
        """
        day = _day(db, on=date(2026, 9, 7),
                   worked_seconds=REQUIRED_MINUTES * 60 - 30)

        assert day.qualifying_minutes == REQUIRED_MINUTES
        assert day.gross_shortfall_minutes == 0
        assert day.contributes_to_accumulator_minutes == 0
        assert fold_week([day]).weekly_total_minutes == 0

        # 29s and 31s are NOT boundary cases — both operands agree there — so
        # they are asserted only to show the boundary sits where it is claimed.
        assert _day(db, on=date(2026, 9, 8),
                    worked_seconds=REQUIRED_MINUTES * 60 - 29
                    ).gross_shortfall_minutes == 0
        assert _day(db, on=date(2026, 9, 9),
                    worked_seconds=REQUIRED_MINUTES * 60 - 31
                    ).gross_shortfall_minutes == 1


# ═════════════════════════════════════════════════════════════════════════════
# The fold
# ═════════════════════════════════════════════════════════════════════════════
class TestFold:
    def test_it_sums_already_rounded_contributions(self, db):
        days = [
            _day(db, on=date(2026, 9, 1), worked_seconds=28800),   # 480 -> 45 short
            _day(db, on=date(2026, 9, 2), worked_seconds=30000),   # 500 -> 25 short
        ]
        assert [d.contributes_to_accumulator_minutes for d in days] == [45, 25]
        assert fold_week(days).weekly_total_minutes == 70

    def test_a_met_requirement_contributes_zero(self, db):
        days = [_day(db, on=date(2026, 9, 1), worked_seconds=REQUIRED_MINUTES * 60)]
        assert days[0].state is EcoDayState.NO_SHORTFALL
        assert fold_week(days).weekly_total_minutes == 0

    def test_the_sum_is_order_independent(self, db):
        days = [
            _day(db, on=date(2026, 9, 1), worked_seconds=28800),
            _day(db, on=date(2026, 9, 2), worked_seconds=30000),
        ]
        assert fold_week(days).weekly_total_minutes == \
            fold_week(list(reversed(days))).weekly_total_minutes

    def test_it_reads_only_the_contribution_field(self, db):
        """No seconds, no durations, no intervals — one integer per day."""
        source = (PACKAGE / "fold.py").read_text(encoding="utf-8")
        for banned in ("duration_seconds", "start_at_utc", "end_at_utc",
                       "qualifying_minutes", "round_half_up", "/ 60", "// 60"):
            assert banned not in source, f"fold.py references {banned}"

    def test_an_empty_week_is_not_a_zero_week(self, db):
        """Ambiguous input must not become an authoritative zero."""
        fold = fold_week([])
        assert fold.is_complete is False
        assert fold.weekly_total_minutes is None
        assert fold.reason == REASON_NO_DAYS


class TestIncompleteWeeks:
    def test_a_deferred_day_prevents_a_final_result(self, db):
        """W1: a deferred day's zero means 'nothing yet', not 'nothing'."""
        days = [
            _short_day(db, date(2026, 9, 1)),
            _day(db, on=date(2026, 9, 2), worked_seconds=0,
                 state=LedgerState.MISSING_CLOCK_OUT.value),
        ]
        assert days[1].state is EcoDayState.DEFERRED_PENDING_LEDGER
        fold = fold_week(days)
        assert fold.is_complete is False
        assert fold.weekly_total_minutes is None
        assert fold.reason == REASON_DEFERRED_DAY

    def test_a_deferred_day_is_not_an_ordinary_completed_day(self, db):
        """Its contribution is 0, yet it must not be folded as if evaluated."""
        deferred = _day(db, on=date(2026, 9, 2), worked_seconds=0,
                        state=LedgerState.MISSING_CLOCK_OUT.value)
        assert deferred.contributes_to_accumulator_minutes == 0
        assert deferred.shortfall_computed is False
        assert fold_week([deferred]).is_complete is False

    def test_an_unresolved_requirement_prevents_a_final_result(self, db):
        led_day = _day(db, on=date(2026, 9, 1), worked_seconds=28800)
        unresolved = evaluate_day(
            ledger=_bare_ledger(db, date(2026, 9, 2)), intervals=[],
            provider=InMemoryConfigVersionProvider())
        assert unresolved.state is EcoDayState.REQUIREMENT_UNRESOLVED
        fold = fold_week([led_day, unresolved])
        assert fold.is_complete is False
        assert fold.reason == REASON_UNRESOLVED_DAY

    def test_a_non_working_day_is_a_member_and_contributes_zero(self, db):
        """Owner ruling 1.

        Previously this returned ``PARTICIPATION_NOT_ESTABLISHED``, which meant
        no real calendar week could ever fold — every real week contains a
        non-working day. Membership in the week and participation as a
        qualifying working day are distinct; only the second is denied to it.
        """
        nwd = _day(db, on=date(2026, 9, 5), worked_seconds=0,
                   state=LedgerState.NOT_A_WORKING_DAY.value)
        assert nwd.state is EcoDayState.NOT_A_WORKING_DAY
        assert nwd.contributes_to_accumulator_minutes == 0

        short = _short_day(db, date(2026, 9, 1))
        fold = fold_week([short, nwd])
        assert fold.is_complete is True
        # It added nothing: the total is the working day's alone.
        assert fold.weekly_total_minutes == short.contributes_to_accumulator_minutes
        assert fold.day_count == 2

    def test_a_week_of_only_non_working_days_folds_to_zero(self, db):
        """A truthful zero, not an unresolved week: every day is accounted
        for and every one of them expected no work."""
        days = [
            _day(db, on=date(2026, 9, 5) + timedelta(days=i), worked_seconds=0,
                 state=LedgerState.NOT_A_WORKING_DAY.value)
            for i in range(2)
        ]
        fold = fold_week(days)
        assert fold.is_complete is True
        assert fold.weekly_total_minutes == 0

    def test_a_deferred_day_is_reported_ahead_of_other_blockers(self, db):
        unresolved = _day(db, on=date(2026, 9, 5), worked_seconds=0)
        object.__setattr__(unresolved, "state", EcoDayState.REQUIREMENT_UNRESOLVED)
        deferred = _day(db, on=date(2026, 9, 2), worked_seconds=0,
                        state=LedgerState.MISSING_CLOCK_OUT.value)
        assert fold_week([unresolved, deferred]).reason == REASON_DEFERRED_DAY

    def test_a_persisted_string_state_is_classified_correctly(self, db):
        """The identity-comparison defect.

        ``EcoDayRecord.state`` is a ``String``, and ``value is EcoDayState.X``
        is False for a matching string — so a persisted deferred day was
        misreported as ``PARTICIPATION_NOT_ESTABLISHED``. It failed closed, but
        it named the wrong condition and hid the one an operator can act on.
        """
        deferred = _day(db, on=date(2026, 9, 2), worked_seconds=0,
                        state=LedgerState.MISSING_CLOCK_OUT.value)
        object.__setattr__(deferred, "state", "DEFERRED_PENDING_LEDGER")
        assert fold_week([deferred]).reason == REASON_DEFERRED_DAY

        working = _short_day(db, date(2026, 9, 1))
        object.__setattr__(working, "state", "SHORTFALL")
        assert fold_week([working]).is_complete is True

    def test_an_unplaced_state_still_fails_closed(self, db):
        """``EVALUABLE`` carries no contribution and must never be summed."""
        day = _short_day(db, date(2026, 9, 1))
        object.__setattr__(day, "state", EcoDayState.EVALUABLE)
        fold = fold_week([day])
        assert fold.is_complete is False
        assert fold.reason == REASON_PARTICIPATION_NOT_ESTABLISHED

    def test_a_contributing_day_with_no_contribution_fails_closed(self, db):
        """Not summable, and a missing value is never read as a zero."""
        day = _short_day(db, date(2026, 9, 1))
        object.__setattr__(day, "contributes_to_accumulator_minutes", None)
        fold = fold_week([day])
        assert fold.is_complete is False
        assert fold.weekly_total_minutes is None

    def test_an_incomplete_fold_cannot_carry_a_total(self):
        with pytest.raises(ValueError):
            WeekFold(is_complete=False, weekly_total_minutes=5,
                     reason=REASON_DEFERRED_DAY)

    def test_a_complete_fold_must_carry_a_total(self):
        with pytest.raises(ValueError):
            WeekFold(is_complete=True)


def _bare_ledger(db, on):
    _uid[0] += 1
    led = WorkDayLedger(
        organization_id=ORG, user_id=_uid[0],
        work_date=datetime(on.year, on.month, on.day),
        workweek_key="2026-W36", ledger_version=1,
        state=LedgerState.SEALED.value, timezone_iana="Asia/Kolkata", is_working_day=True, working_day_source="org",
        utc_offset_seconds=0, created_at=NOW, created_by="TEST",
    )
    db.add(led)
    db.flush()
    return led


# ═════════════════════════════════════════════════════════════════════════════
# Boundary guards
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


class TestBoundaries:
    def test_the_package_is_four_modules(self):
        """``membership`` and ``evaluator`` joined ``fold`` under ruling 3:
        the public entry point derives its own day set."""
        assert {p.name for p in sorted(PACKAGE.glob("*.py"))} == {
            "__init__.py", "fold.py", "membership.py", "evaluator.py",
            "outcomes.py"}

    @pytest.mark.parametrize("forbidden", [
        "app._worked", "app.payroll", "app.risk", "app.active_time",
        "app.breaks", "app.rollout", "app.early_clock_out",
    ])
    def test_no_forbidden_dependency(self, forbidden):
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

    def test_no_interval_or_requirement_reconstruction(self):
        """The daily calculation is never redone here.

        ``work_ledger`` is now imported by ``membership.py`` alone, and only for
        ``WorkDayLedger`` — ruling 3 makes the ledger the authority on which
        days are in the week. What stays forbidden is re-deriving the daily
        VALUES: no ``WorkStateInterval``, no requirement resolution, no
        configuration versioning. EVAL-DAY's stored answer is the only input.
        """
        for path in sorted(PACKAGE.glob("*.py")):
            offenders = {
                m for m in _imports(path)
                if "config_versioning" in m or "requirement_resolution" in m
            }
            assert not offenders, f"{path.name} imports {offenders}"

            body = path.read_text(encoding="utf-8")
            for banned in ("WorkStateInterval", "duration_seconds",
                           "resolve_requirement", "round_half_up"):
                assert banned not in body, f"{path.name} uses {banned}"

        ledger_importers = {
            path.name for path in sorted(PACKAGE.glob("*.py"))
            if any("work_ledger" in m for m in _imports(path))
        }
        assert ledger_importers == {"membership.py"}, ledger_importers

    @pytest.mark.parametrize("banned", [
        # "workweek" is deliberately absent: ruling 3 makes
        # ``evaluate_workweek`` the authorized public entry point, and
        # membership derivation from the persisted key is in scope. "week_start"
        # stays banned — that would be week_start_date(), which reads CURRENT
        # configuration and would re-derive a historical week wrongly.
        "threshold", "tier", "crossing", "event", "notif", "advisory",
        "netting", "grace", "horizon", "week_start", "persist",
        "correction", "supersede",
    ])
    def test_no_out_of_scope_identifier(self, banned):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
            }
            offenders = {n for n in names if banned in n.lower()}
            assert not offenders, f"{path.name} defines {offenders}"

    def test_no_orm_model_is_declared(self):
        """W6 unresolved: no weekly persistence."""
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    bases = {
                        b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                        for b in node.bases
                    }
                    assert "Base" not in bases, f"{path.name} declares {node.name}"

    def test_no_weekly_migration_exists(self):
        versions = BACKEND / "alembic" / "versions"
        hits = [
            p.name for p in versions.glob("*.py")
            if "weekly" in p.read_text(encoding="utf-8", errors="ignore").lower()
            and "eval_week" in p.read_text(encoding="utf-8", errors="ignore").lower()
        ]
        assert hits == []

    def test_nothing_in_the_application_wires_this_package(self):
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            # ``eval_event`` is the authorised consumer of W — the next stage
            # of the same closed pipeline, not the application reaching in.
            if {"eval_week", "eval_event"} & set(path.parts):
                continue
            # MODEL-NETTING asks this package ONE question: which dates belong
            # to a workweek (`canonical_dates`). §15 defines the weekly bank
            # over the canonical workweek, so the alternative was a second
            # membership rule inside EVAL-DAY — exactly the drift W4-3 exists
            # to prevent. The import is DEFERRED, inside the single function
            # that needs it, so the module graph stays acyclic.
            if path.name == "netting.py":
                continue
            if any(m.startswith("app.eval_week") for m in _imports(path)):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == []

    def test_eval_day_behaviour_is_unchanged(self, db):
        """The fold must not have perturbed the layer beneath it."""
        day = _day(db, on=date(2026, 9, 1), worked_seconds=28800)
        assert day.qualifying_minutes == 480
        assert day.required_minutes == REQUIRED_MINUTES
        assert day.gross_shortfall_minutes == 45
        assert day.attributable_minutes == 45
        assert day.contributes_to_accumulator_minutes == 45
