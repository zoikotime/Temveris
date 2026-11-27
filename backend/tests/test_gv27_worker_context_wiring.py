"""
GV-27 — Worker Context wired into ECO requirement resolution.
=============================================================

    scenario   "Classification change mid-week (§24.12)"
    inputs     "Part-Time Mon-Tue, Full-Time Wed-Fri"
    expected   "Per-day requirements 240/240/525/525/525; single weekly threshold"

Owner rulings R1-R9. The property under test: **what a Worker owed on a past day
is decided by what they WERE on that day**, read from an append-only timeline,
never from the mutable column that says what they are now.

WHY THE VALUES ARE NOT WRITTEN DOWN HERE
----------------------------------------
240 and 525 appear in exactly two places in this file: the configuration the
test captures, and the golden vector it reads back from disk. The expected
sequence is DERIVED by pairing the captured configuration with the recorded
timeline, then checked against the vector's own ``expected_fields``. Nothing
asserts a literal, so a provider that hard-coded either number would fail the
derivation rather than agree with it.

NOT A GV-27 EXECUTION HARNESS
-----------------------------
These are the wiring's focused tests. GV-27 stays
``blocked_pending_platform_primitive`` and unregistered until its own promotion
decision; ``test_the_vector_is_still_blocked`` pins that.
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
from app.config_versioning import DatabaseConfigVersionProvider
from app.config_versioning.adapter import AUTHORIZED_WORKER_CLASSIFICATIONS
from app.eval_day import EcoDayState, evaluate_and_record_day
from app.eval_week import evaluate_workweek
from app.eval_week.outcomes import WeeklyState
from app.requirement_resolution import RequirementCandidate
from app.requirement_resolution.outcomes import ResolutionOutcome
from app.requirement_resolution.resolver import resolve_requirement_strict
from app.schedule_policy import keys as K
from app.work_ledger import Provenance
from app.work_ledger_producer import produce_and_seal_work_day, set_producer_cutover
from tests._eco_policy import capture_test_policy

from app.worker_context import ContextPeriod, record_timeline

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, WORKER = 7480, 7481
ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)

#: Mon 7 Sep 2026 .. Fri 11 Sep 2026.
WEEK_START = date(2026, 9, 7)
WEEK_KEY = "2026-09-07"
DAYS = [WEEK_START + timedelta(days=i) for i in range(5)]
#: The classification changes on the WEDNESDAY, mid-week — the whole point.
CHANGE_ON = DAYS[2]

CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
ALL_SEVEN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]

#: §10.1 rank 4, the tier owner ruling R5 uses in its own worked example.
TIER, RANK = "EMPLOYMENT_POLICY", 4
#: The tenant-wide fallback, deliberately present so the tests can prove R3
#: refuses rather than quietly falling through to it.
FALLBACK_TIER, FALLBACK_RANK, FALLBACK_MINUTES = "TENANT_FALLBACK", 6, 480


def _vector() -> dict:
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    return [v for v in data["vectors"] if v["id"] == "GV-27"][0]


def _local(on: date, hour: int, minute: int = 0) -> datetime:
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=UTC) - IST


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def engine_fk():
    import app.clock_session_boundary.models      # noqa: F401
    import app.config_versioning.models           # noqa: F401
    import app.eval_day.leave_snapshot            # noqa: F401
    import app.eval_day.models                    # noqa: F401
    import app.policy_version.models             # noqa: F401
    import app.break_treatment.models             # noqa: F401
    import app.break_classification.models       # noqa: F401
    import app.work_ledger.models                 # noqa: F401
    import app.work_ledger_producer.models        # noqa: F401
    import app.worker_context.models              # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(engine_fk):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(engine_fk, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="GV27 Org"))
        s.commit()
        s.add(dbm.User(id=WORKER, name="W", email="w@gv27.test",
                       organization_id=ORG, role="employee",
                       employment_type="full_time"))
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(ALL_SEVEN)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@gv27.test")
        s.commit()
        # An explicit historical PolicyVersion. ``evaluate_and_record_day``
        # refuses with POLICY_UNRESOLVED without one — a production rule this
        # fixture satisfies rather than weakens. Carries no requirement:
        # daily_minutes stays with ConfigValue / Requirement Resolution.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


# ── the configuration under test ────────────────────────────────────────────
def _per_type_minutes() -> dict:
    """The per-worker-type requirements, taken from the GV-27 vector itself.

    ``expected_fields.per_day_required_minutes`` is ``[240, 240, 525, 525, 525]``
    and the timeline changes on the third day, so the first entry is the
    part-time requirement and the last is the full-time one. Derived, so this
    file contains no requirement literal of its own.
    """
    per_day = _vector()["expected_fields"]["per_day_required_minutes"]
    return {"part_time": per_day[0], "full_time": per_day[-1]}


def _capture(db, *, per_type=None, fallback=True, version="gv27-v1",
             effective_from=date(2026, 1, 1)):
    per_type = _per_type_minutes() if per_type is None else per_type
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id=version,
        effective_from=effective_from,
        candidates=([RequirementCandidate(tier=FALLBACK_TIER,
                                          rank=FALLBACK_RANK,
                                          minutes=FALLBACK_MINUTES)]
                    if fallback else []),
        candidates_by_classification={
            classification: [RequirementCandidate(
                tier=TIER, rank=RANK, minutes=minutes,
                source_ref=f"policy:{classification}")]
            for classification, minutes in per_type.items()
        },
    )
    db.flush()


def _timeline(db, *, periods=None):
    """Part-time from the start of the week, full-time from the Wednesday."""
    if periods is None:
        periods = [
            ContextPeriod(effective_from=WEEK_START - timedelta(days=7),
                          worker_context="part_time"),
            ContextPeriod(effective_from=CHANGE_ON, worker_context="full_time"),
        ]
    record_timeline(db, user=db.get(dbm.User, WORKER), periods=periods)
    db.commit()


def _resolve(db, on: date):
    return resolve_requirement_strict(
        organization_id=ORG, user_id=WORKER, as_of=on,
        provider=DatabaseConfigVersionProvider(db),
    )


def _seal(db, on: date):
    """A real sealed day, so EVAL-DAY has canonical evidence to evaluate."""
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=_local(on, 9), clock_out_time=_local(on, 18),
        total_seconds=0, break_seconds=0, offline_seconds=0, is_active=False,
    )
    db.add(row)
    db.flush()
    for boundary, at in ((BOUNDARY_CLOCK_IN, _local(on, 9)),
                         (BOUNDARY_CLOCK_OUT, _local(on, 18))):
        append_boundary_event(db, session_id=row.id, user_id=WORKER,
                              boundary=boundary, boundary_at=at,
                              provenance=Provenance.WORKER,
                              actor_label="w@gv27.test")
    db.commit()
    produce_and_seal_work_day(db, organization_id=ORG, user_id=WORKER,
                              work_date=on)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# 1. One version, both classifications (R1 / R4 / R5)
# ══════════════════════════════════════════════════════════════════════════════
class TestOneVersionHoldsBoth:
    def test_a_single_version_carries_a_value_per_classification(self, db):
        from app.config_versioning import ConfigValue

        _capture(db)
        rows = db.query(ConfigValue).filter(
            ConfigValue.worker_classification.is_not(None)).all()
        assert {r.worker_classification: r.value_minutes for r in rows} == \
            _per_type_minutes()
        # One version, one tier, one key — what the old index forbade.
        assert len({r.config_version_id for r in rows}) == 1
        assert {r.tier for r in rows} == {TIER}
        assert {r.key for r in rows} == {"required_minutes"}

    def test_the_uniqueness_rule_still_bites_within_a_classification(self, db):
        """R5 widened the identity; it did not loosen it."""
        from app.config_versioning import ConfigValue

        _capture(db)
        version_pk = db.query(ConfigValue).first().config_version_id
        db.add(ConfigValue(
            config_version_id=version_pk, organization_id=ORG,
            worker_classification="part_time", tier=TIER, rank=RANK,
            key="required_minutes", value_minutes=99,
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_the_two_scopes_are_mutually_exclusive(self, db):
        """R4: a row cannot be scoped to a Worker AND a classification."""
        from app.config_versioning import ConfigValue

        _capture(db)
        version_pk = db.query(ConfigValue).first().config_version_id
        db.add(ConfigValue(
            config_version_id=version_pk, organization_id=ORG,
            scope_user_id=WORKER, worker_classification="part_time",
            tier=TIER, rank=RANK, key="required_minutes", value_minutes=99,
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_an_unauthorized_classification_is_refused_not_normalised(self, db):
        """R2: never coerced onto full_time, never stored."""
        with pytest.raises(ValueError, match="not an authorized"):
            _capture(db, per_type={"contractor": 300})

    def test_the_authorized_vocabulary_is_exactly_two_values(self):
        assert AUTHORIZED_WORKER_CLASSIFICATIONS == {"full_time", "part_time"}


# ══════════════════════════════════════════════════════════════════════════════
# 2. Historical selection (R8)
# ══════════════════════════════════════════════════════════════════════════════
class TestHistoricalSelection:
    def test_each_day_resolves_the_classification_in_force_that_day(self, db):
        _capture(db)
        _timeline(db)
        per_type = _per_type_minutes()
        expected = [per_type["part_time"] if d < CHANGE_ON
                    else per_type["full_time"] for d in DAYS]

        resolved = [_resolve(db, d) for d in DAYS]
        assert [r.outcome for r in resolved] == [ResolutionOutcome.RESOLVED] * 5
        assert [r.required_minutes for r in resolved] == expected

    def test_todays_classification_cannot_change_a_historical_day(self, db):
        """The regression this whole increment exists to prevent."""
        _capture(db)
        _timeline(db)
        monday_before = _resolve(db, DAYS[0]).required_minutes

        # The Worker is promoted again, far later. History must not move.
        record_timeline(db, user=db.get(dbm.User, WORKER), periods=[
            ContextPeriod(effective_from=WEEK_START - timedelta(days=7),
                          worker_context="part_time"),
            ContextPeriod(effective_from=CHANGE_ON, worker_context="full_time"),
            ContextPeriod(effective_from=date(2026, 12, 1),
                          worker_context="part_time"),
        ], expected_version=1)
        db.commit()

        assert _resolve(db, DAYS[0]).required_minutes == monday_before
        assert _resolve(db, DAYS[4]).required_minutes == \
            _per_type_minutes()["full_time"]

    def test_the_mutable_column_is_not_consulted(self, db):
        """``User.employment_type`` says full_time throughout; Monday is still
        part-time, because the TIMELINE says so."""
        _capture(db)
        _timeline(db)
        assert db.get(dbm.User, WORKER).employment_type == "full_time"
        assert _resolve(db, DAYS[0]).required_minutes == \
            _per_type_minutes()["part_time"]

    def test_provenance_identifies_the_winning_candidate_and_version(self, db):
        _capture(db)
        _timeline(db)
        monday = _resolve(db, DAYS[0])
        assert monday.source_tier == TIER
        assert monday.source_rank == RANK
        assert monday.source_ref == "policy:part_time"
        assert monday.config_version_id == "gv27-v1"
        assert monday.effective_from == date(2026, 1, 1)
        # The classification is recoverable from source_ref because the capture
        # put it there — NOT because anything selects on it (R8.10).
        assert monday.source_ref != _resolve(db, DAYS[4]).source_ref


# ══════════════════════════════════════════════════════════════════════════════
# 3. Fail closed (R3)
# ══════════════════════════════════════════════════════════════════════════════
class TestFailsClosed:
    def test_no_history_refuses_rather_than_using_the_fallback(self, db):
        _capture(db)          # includes a rank-6 TENANT_FALLBACK
        # No timeline recorded at all.
        result = _resolve(db, DAYS[0])
        assert result.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
        assert result.required_minutes is None

    def test_below_floor_refuses_rather_than_using_the_fallback(self, db):
        _capture(db)
        _timeline(db, periods=[
            ContextPeriod(effective_from=CHANGE_ON, worker_context="full_time"),
        ])
        # Monday precedes the earliest captured period -> UNKNOWN_BELOW_FLOOR.
        assert _resolve(db, DAYS[0]).outcome is \
            ResolutionOutcome.REQUIREMENT_UNRESOLVED
        # Wednesday onward is resolvable.
        assert _resolve(db, CHANGE_ON).outcome is ResolutionOutcome.RESOLVED

    def test_the_fallback_really_was_reachable(self, db):
        """Guard the two guards above: if the fallback were absent, they would
        pass for the wrong reason.

        The classification rows are omitted at CAPTURE rather than deleted
        afterwards. A ConfigVersion is a complete snapshot and is never edited
        — which owner decision 1A now enforces in the database — so capturing
        the version this test actually means is both the faithful shape and the
        only one available.
        """
        _capture(db, per_type={}, fallback=True)
        _timeline(db)
        assert _resolve(db, DAYS[0]).required_minutes == FALLBACK_MINUTES


# ══════════════════════════════════════════════════════════════════════════════
# 4. Classification-independent configuration is untouched
# ══════════════════════════════════════════════════════════════════════════════
class TestExistingBehaviourPreserved:
    def test_a_version_without_classification_rows_ignores_worker_context(self, db):
        """No timeline, no classification rows — the pre-existing answer."""
        _capture(db, per_type={}, fallback=True)
        result = _resolve(db, DAYS[0])
        assert result.outcome is ResolutionOutcome.RESOLVED
        assert result.required_minutes == FALLBACK_MINUTES

    def test_a_worker_scoped_value_still_wins_outright(self, db):
        _capture(db)
        _timeline(db)
        DatabaseConfigVersionProvider(db).capture_version(
            organization_id=ORG, config_version_id="gv27-v2",
            effective_from=date(2026, 2, 1),
            candidates=[RequirementCandidate(tier=FALLBACK_TIER,
                                             rank=FALLBACK_RANK, minutes=480)],
            candidates_by_user={WORKER: [RequirementCandidate(
                tier="WORKER_OVERRIDE", rank=0, minutes=300)]},
        )
        db.flush()
        assert _resolve(db, DAYS[0]).required_minutes == 300


# ══════════════════════════════════════════════════════════════════════════════
# 5. End to end — EVAL-DAY and EVAL-WEEK
# ══════════════════════════════════════════════════════════════════════════════
class TestEndToEnd:
    def test_eval_day_records_the_per_day_requirement(self, db):
        _capture(db)
        _timeline(db)
        per_type = _per_type_minutes()
        expected = [per_type["part_time"] if d < CHANGE_ON
                    else per_type["full_time"] for d in DAYS]

        recorded = []
        for day in DAYS:
            _seal(db, day)
            outcome = evaluate_and_record_day(
                db, organization_id=ORG, user_id=WORKER, work_date=day)
            assert outcome.is_recorded, f"{outcome.reason} {outcome.detail}"
            recorded.append(outcome.record)
        db.commit()

        assert [r.required_minutes for r in recorded] == expected
        # And the vector's own expectation, read from disk.
        assert [r.required_minutes for r in recorded] == \
            _vector()["expected_fields"]["per_day_required_minutes"]
        assert all(r.state in (EcoDayState.NO_SHORTFALL.value,
                               EcoDayState.SHORTFALL.value) for r in recorded)

    def test_the_week_folds_over_those_records(self, db):
        _capture(db)
        _timeline(db)
        for day in DAYS:
            _seal(db, day)
            evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                    work_date=day)
        db.commit()

        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=WEEK_KEY)
        # Every day evaluated, so the week is foldable — not deferred and not
        # blocked on an unresolved requirement.
        assert week.state is WeeklyState.COMPLETE
        assert week.weekly_attributable_shortfall_minutes is not None
        # GV-27's other expected field: ONE weekly threshold for the week, not
        # one per classification.
        assert _vector()["expected_fields"]["weekly_threshold_count"] == 1

    def test_re_evaluation_is_idempotent(self, db):
        from app.eval_day.models import EcoDayRecord

        _capture(db)
        _timeline(db)
        _seal(db, DAYS[0])
        first = evaluate_and_record_day(db, organization_id=ORG,
                                        user_id=WORKER, work_date=DAYS[0])
        db.commit()
        again = evaluate_and_record_day(db, organization_id=ORG,
                                        user_id=WORKER, work_date=DAYS[0])
        db.commit()
        assert again.record.id == first.record.id
        assert db.query(EcoDayRecord).filter_by(work_date=DAYS[0]).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# 6. Static guards — the wiring must not acquire a back door
# ══════════════════════════════════════════════════════════════════════════════
class TestStaticGuards:
    ADAPTER = BACKEND / "app" / "config_versioning" / "adapter.py"

    def test_the_adapter_reads_no_live_or_mutable_employment_data(self):
        src = self.ADAPTER.read_text(encoding="utf-8")
        for banned in ("DEFAULT_EXPECTED_WORK_DURATIONS",
                       "DEFAULT_EXPECTED_WORK_SECONDS",
                       "attendance_policy", "schedule_policy",
                       "employment_type", "28800"):
            assert banned not in src, f"adapter.py references {banned}"

    def test_the_adapter_resolves_context_at_the_evaluation_date(self):
        """``as_of`` is passed through — not ``date.today()``, not ``None``."""
        tree = ast.parse(self.ADAPTER.read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "worker_context_as_of"
        ]
        assert len(calls) == 1, "expected exactly one context resolution"
        as_of = [k for k in calls[0].keywords if k.arg == "as_of"]
        assert len(as_of) == 1
        assert getattr(as_of[0].value, "id", None) == "as_of", (
            "the context must be resolved at the evaluation date")

    def test_source_ref_is_never_a_selector(self):
        """R8.10: provenance stays provenance."""
        src = self.ADAPTER.read_text(encoding="utf-8")
        assert "source_ref ==" not in src
        assert "ConfigValue.source_ref" not in src

    def test_no_classification_token_was_folded_into_a_tier(self):
        """R1: §10.1's tier semantics are untouched."""
        src = self.ADAPTER.read_text(encoding="utf-8")
        for invented in ("EMPLOYMENT_POLICY_FULL_TIME", "TENANT_TYPE_DEFAULT_",
                         "tier + ", "f\"{tier}"):
            assert invented not in src, f"adapter.py invents {invented!r}"

    def test_the_vector_is_promoted_and_executed_elsewhere(self):
        """Promoted 2026-09-17, once the wiring below had an execution harness.

        This guard asserted the opposite while the wiring existed but was
        unproven end to end — the risk then was promoting it quietly. GV-27 now
        executes in ``test_gv27_execution.py::TestGV27``; these tests remain the
        wiring's own focused coverage, which is why the pointer is asserted
        rather than the execution being duplicated here.
        """
        harness = _vector()["harness"]
        assert harness["execution"] == "executable"
        assert harness["blocking_dependencies"] == []
        src = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-27": ("test_gv27_execution.py", "class TestGV27")' in src
