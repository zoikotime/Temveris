"""
GV-27 — "Classification change mid-week (§24.12)" executed end to end.
======================================================================

The vector, verbatim:

    scenario   "Classification change mid-week (§24.12)"
    inputs     "Part-Time Mon-Tue, Full-Time Wed-Fri"
    expected   "Per-day requirements 240/240/525/525/525; single weekly threshold"

``TestGV27`` drives the REAL pipeline for both clauses:

    Worker Context timeline -> ConfigVersion (classification-scoped values)
        -> RequirementCandidateProvider -> RequirementResolution
        -> producer -> sealed WorkDayLedger -> EVAL-DAY -> EcoDayRecord
        -> EVAL-WEEK -> EVAL-EVENT

WHERE THE NUMBERS COME FROM
---------------------------
240 and 525 are never written in this file. The two per-worker-type
requirements are read out of the vector's own ``expected_fields`` and captured
as configuration; the five daily requirements then come back through the
pipeline and are compared with the same vector. A provider that hard-coded
either number would fail that round trip rather than agree with it.

THE SECOND CLAUSE
-----------------
"Single weekly threshold" is the §24.12 point that a mid-week reclassification
does not split the week: the Worker owes two different daily requirements but
faces ONE weekly threshold, not one per classification. Proven behaviourally —
the week crosses, and exactly one threshold event exists for it — and
structurally, because the threshold is a classification-INDEPENDENT row while
the requirements beside it are classification-scoped.
"""

from __future__ import annotations

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
from app.config_versioning.models import ConfigValue, ConfigVersion
from app.eval_day import EcoDayState, evaluate_and_record_day
from app.eval_event import EventOutcome, evaluate_threshold_event
from app.eval_event.models import EcoThresholdEvent
from app.eval_week import evaluate_workweek
from app.eval_week.outcomes import WeeklyState
from app.requirement_resolution import RequirementCandidate
from app.requirement_resolution.outcomes import ResolutionOutcome
from app.requirement_resolution.resolver import resolve_requirement_strict
from app.schedule_policy import keys as K
from app.work_ledger import Provenance, WorkDayLedger
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)
from tests._eco_policy import capture_test_policy

from app.worker_context import ContextPeriod, record_timeline, worker_context_as_of

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, WORKER = 7500, 7501
ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)

#: Mon 7 Sep 2026 .. Fri 11 Sep 2026, all in the past so the closure gate holds.
WEEK_START = date(2026, 9, 7)
WEEK_KEY = "2026-09-07"
DAYS = [WEEK_START + timedelta(days=i) for i in range(5)]
#: "Part-Time Mon-Tue, Full-Time Wed-Fri" — the change lands on the Wednesday.
CHANGE_ON = DAYS[2]

CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
WORKING_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]

#: §10.1 rank 4 — the tier owner ruling R5 used in its own worked example.
TIER, RANK = "EMPLOYMENT_POLICY", 4
#: Weekly review threshold. 30 is §21's canonical value (GV-04 triggers at
#: exactly 30); the week below clears it comfortably, so the crossing is not a
#: boundary case pretending to be a behaviour.
THRESHOLD_MINUTES = 30


def _vector() -> dict:
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    return [v for v in data["vectors"] if v["id"] == "GV-27"][0]


def _per_type() -> dict:
    """The two requirements, taken FROM the vector.

    ``per_day_required_minutes`` is part-time for the first day and full-time
    for the last, because the timeline changes in between. Nothing in this file
    states either number.
    """
    per_day = _vector()["expected_fields"]["per_day_required_minutes"]
    return {"part_time": per_day[0], "full_time": per_day[-1]}


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
    import app.eval_event.models                  # noqa: F401
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
        # The mutable column says full_time for the whole week. Every part-time
        # day below therefore proves the timeline was read, not this.
        s.add(dbm.User(id=WORKER, name="W", email="w@gv27x.test",
                       organization_id=ORG, role="employee",
                       employment_type="full_time"))
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(WORKING_DAYS)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@gv27x.test")
        s.commit()
        # An explicit historical PolicyVersion. ``evaluate_and_record_day``
        # refuses with POLICY_UNRESOLVED without one — a production rule this
        # fixture satisfies rather than weakens. Carries no requirement:
        # daily_minutes stays with ConfigValue / Requirement Resolution.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


# ── the scenario ────────────────────────────────────────────────────────────
def _configure(db):
    """One configuration version: two classification-scoped requirements and
    ONE classification-independent weekly threshold."""
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id="gv27-v1",
        effective_from=date(2026, 1, 1),
        candidates_by_classification={
            classification: [RequirementCandidate(
                tier=TIER, rank=RANK, minutes=minutes,
                source_ref=f"policy:{classification}")]
            for classification, minutes in _per_type().items()
        },
    )
    db.flush()

    # The threshold goes straight into the same version's values. There is
    # deliberately no threshold capture path — who may set a threshold is
    # unruled — so this writes it the way `test_eval_event.py` does, and for
    # the same reason. Classification-independent BY CONSTRUCTION: one weekly
    # threshold, whatever the Worker was on any given day.
    from app.eco_threshold import (
        ECO_THRESHOLD_KEY, ECO_THRESHOLD_RANK, ECO_THRESHOLD_TIER,
    )
    version = db.query(ConfigVersion).filter_by(version_key="gv27-v1").one()
    db.add(ConfigValue(
        config_version_id=version.id, organization_id=ORG,
        scope_user_id=None, worker_classification=None,
        tier=ECO_THRESHOLD_TIER, rank=ECO_THRESHOLD_RANK,
        key=ECO_THRESHOLD_KEY, value_minutes=THRESHOLD_MINUTES,
    ))
    db.commit()


def _timeline(db):
    """Part-Time from before the week, Full-Time from the Wednesday."""
    record_timeline(db, user=db.get(dbm.User, WORKER), periods=[
        ContextPeriod(effective_from=WEEK_START - timedelta(days=7),
                      worker_context="part_time"),
        ContextPeriod(effective_from=CHANGE_ON, worker_context="full_time"),
    ])
    db.commit()


def _work(db, on: date, minutes: int):
    """A real session, sealed by the producer into canonical intervals."""
    start = _local(on, 9)
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=start, clock_out_time=start + timedelta(minutes=minutes),
        total_seconds=0, break_seconds=0, offline_seconds=0, is_active=False,
    )
    db.add(row)
    db.flush()
    for boundary, at in ((BOUNDARY_CLOCK_IN, row.clock_in_time),
                         (BOUNDARY_CLOCK_OUT, row.clock_out_time)):
        append_boundary_event(db, session_id=row.id, user_id=WORKER,
                              boundary=boundary, boundary_at=at,
                              provenance=Provenance.WORKER,
                              actor_label="w@gv27x.test")
    db.commit()
    result = produce_and_seal_work_day(db, organization_id=ORG,
                                       user_id=WORKER, work_date=on)
    db.commit()
    assert result.outcome is ProducerOutcome.SEALED, result.outcome
    return result


def _worked_minutes():
    """What the Worker actually does each day.

    Mon/Tue meet the part-time requirement exactly. Wed-Fri fall short of the
    full-time one, so the week accumulates a real shortfall and the threshold
    has something to cross. Derived from the requirements, never stated.
    """
    per_type = _per_type()
    short_by = 45
    return [per_type["part_time"], per_type["part_time"]] + \
           [per_type["full_time"] - short_by] * 3, short_by


# ══════════════════════════════════════════════════════════════════════════════
# GV-27
# ══════════════════════════════════════════════════════════════════════════════
class TestGV27:
    """§24.12 — a mid-week reclassification, through the real chain."""

    # ── the artifact ────────────────────────────────────────────────────────
    def test_the_vector_is_promoted(self):
        """Promoted 2026-09-17, after its promotion-readiness audit.

        The guard this replaces asserted the opposite while the wiring existed
        but was unproven end to end; the risk then was promoting it quietly.
        That risk is gone, so the claim inverts — exactly as GV-16's and
        GV-17's did at their own promotions.
        """
        harness = _vector()["harness"]
        assert harness["execution"] == "executable"
        assert harness["blocking_dependencies"] == []

    def test_this_harness_is_registered_and_points_here(self):
        """Registration and execution status cannot be separated.

        The golden suite asserts ``EXTERNAL_EXECUTION_TESTS <= EXECUTABLE_IDS``,
        so a promoted vector must name the suite that runs it. This is the other
        end of that link, by the exact spelling the guard resolves.
        """
        src = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-27": ("test_gv27_execution.py", "class TestGV27")' in src

    # ── the scenario is really a mid-week change ────────────────────────────
    def test_the_timeline_changes_classification_mid_week(self, db):
        """Guard the scenario: if the timeline did not move on the Wednesday,
        everything below would pass for the wrong reason."""
        _configure(db)
        _timeline(db)
        contexts = [
            worker_context_as_of(db, organization_id=ORG, user_id=WORKER,
                                 as_of=day).worker_context
            for day in DAYS
        ]
        assert contexts == ["part_time", "part_time",
                            "full_time", "full_time", "full_time"]
        # And the mutable column disagrees with the first two, which is the
        # whole §24.12 hazard.
        assert db.get(dbm.User, WORKER).employment_type == "full_time"

    # ── the vector ──────────────────────────────────────────────────────────
    def test_gv27_executes(self, db):
        expected = _vector()["expected_fields"]
        handled = []

        _configure(db)
        _timeline(db)

        # INPUT, verbatim: "Part-Time Mon-Tue, Full-Time Wed-Fri".
        worked, short_by = _worked_minutes()
        records = []
        for day, minutes in zip(DAYS, worked):
            _work(db, day, minutes)
            outcome = evaluate_and_record_day(
                db, organization_id=ORG, user_id=WORKER, work_date=day)
            assert outcome.is_recorded, f"{outcome.reason} {outcome.detail}"
            records.append(outcome.record)
        db.commit()

        # ── expected: per_day_required_minutes ──────────────────────────────
        assert [r.required_minutes for r in records] == \
            expected["per_day_required_minutes"]
        handled.append("per_day_required_minutes")

        # The requirement genuinely came from the classification, tier and rank
        # unchanged across the change — only the value moved.
        assert {r.state for r in records} == {
            EcoDayState.NO_SHORTFALL.value, EcoDayState.SHORTFALL.value}
        assert [r.attributable_minutes for r in records] == \
            [0, 0, short_by, short_by, short_by]

        # ── the five days are ONE workweek ──────────────────────────────────
        ledgers = db.query(WorkDayLedger).filter(
            WorkDayLedger.organization_id == ORG,
            WorkDayLedger.user_id == WORKER).all()
        assert {led.workweek_key for led in ledgers} == {WEEK_KEY}

        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=WEEK_KEY)
        assert week.state is WeeklyState.COMPLETE
        assert week.weekly_attributable_shortfall_minutes == short_by * 3

        # ── expected: weekly_threshold_count ────────────────────────────────
        # One threshold for the week, not one per classification. Evaluated
        # twice on purpose: §21.2 makes a crossing a natural-key fact, so a
        # replay must not produce a second event either.
        first = evaluate_threshold_event(
            db, organization_id=ORG, user_id=WORKER, workweek_key=WEEK_KEY)
        db.commit()
        assert first.outcome is EventOutcome.CROSSED
        replay = evaluate_threshold_event(
            db, organization_id=ORG, user_id=WORKER, workweek_key=WEEK_KEY)
        db.commit()
        assert replay.outcome is EventOutcome.ALREADY_CROSSED

        events = db.query(EcoThresholdEvent).filter(
            EcoThresholdEvent.organization_id == ORG,
            EcoThresholdEvent.user_id == WORKER,
            EcoThresholdEvent.workweek_key == WEEK_KEY).all()
        assert len(events) == expected["weekly_threshold_count"]
        assert {e.threshold_minutes for e in events} == {THRESHOLD_MINUTES}
        handled.append("weekly_threshold_count")

        assert set(handled) == set(expected), (
            f"expected_fields not compared: {set(expected) - set(handled)}")

    # ── the threshold is not per-classification ─────────────────────────────
    def test_the_weekly_threshold_is_classification_independent(self, db):
        """Structural half of "single weekly threshold".

        The week's requirements are classification-SCOPED and its threshold is
        classification-INDEPENDENT, in one version. That is what makes a
        mid-week reclassification change what the Worker owes per day without
        changing how many thresholds the week has.
        """
        _configure(db)
        from app.eco_threshold import ECO_THRESHOLD_KEY

        rows = db.query(ConfigValue).filter(
            ConfigValue.organization_id == ORG).all()
        thresholds = [r for r in rows if r.key == ECO_THRESHOLD_KEY]
        requirements = [r for r in rows if r.key != ECO_THRESHOLD_KEY]

        assert len(thresholds) == 1
        assert thresholds[0].worker_classification is None
        assert {r.worker_classification for r in requirements} == \
            {"part_time", "full_time"}
        assert len({r.config_version_id for r in rows}) == 1

    # ── historical determinism ──────────────────────────────────────────────
    def test_a_later_reclassification_cannot_move_the_week(self, db):
        """§17 / INV-8. Re-resolving Monday after a further change still uses
        what the Worker was on Monday."""
        _configure(db)
        _timeline(db)
        before = [
            resolve_requirement_strict(
                organization_id=ORG, user_id=WORKER, as_of=day,
                provider=DatabaseConfigVersionProvider(db)).required_minutes
            for day in DAYS
        ]

        record_timeline(db, user=db.get(dbm.User, WORKER), periods=[
            ContextPeriod(effective_from=WEEK_START - timedelta(days=7),
                          worker_context="part_time"),
            ContextPeriod(effective_from=CHANGE_ON, worker_context="full_time"),
            ContextPeriod(effective_from=date(2027, 1, 1),
                          worker_context="part_time"),
        ], expected_version=1)
        db.commit()

        after = [
            resolve_requirement_strict(
                organization_id=ORG, user_id=WORKER, as_of=day,
                provider=DatabaseConfigVersionProvider(db)).required_minutes
            for day in DAYS
        ]
        assert after == before == _vector()["expected_fields"][
            "per_day_required_minutes"]

    # ── fail closed (owner ruling R3) ───────────────────────────────────────
    def test_an_unresolved_classification_refuses_the_day(self, db):
        """No timeline at all: the requirement is unresolved, not guessed."""
        _configure(db)
        result = resolve_requirement_strict(
            organization_id=ORG, user_id=WORKER, as_of=DAYS[0],
            provider=DatabaseConfigVersionProvider(db))
        assert result.outcome is ResolutionOutcome.REQUIREMENT_UNRESOLVED
        assert result.required_minutes is None
        assert result.chain_evaluated == ()
