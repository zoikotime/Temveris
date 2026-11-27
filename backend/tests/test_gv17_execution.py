"""
GV-17 — "Deferred exit (§24.7)" executed end to end.
====================================================

The vector, verbatim:

    scenario   "Deferred exit (§24.7)"
    inputs     "Day contains an unverified exit"
    expected   "state DEFERRED_PENDING_LEDGER; contributes 0; visible to both
                parties; recomputes on resolution"

``TestGV17`` drives the REAL pipeline for every clause of that sentence:

    departure evidence -> producer -> canonical sealed WorkDayLedger
                       -> EVAL-DAY -> EcoDayRecord -> EVAL-WEEK
                       -> read_eco_day -> require_eco_view_authority
                       -> render_eco_day (WF-ECO-03)

and then the clause nothing else in the suite exercises — "recomputes on
resolution" — by resolving the exit through the authoritative append-only
boundary mechanism, re-running the producer (which supersedes the sealed day
under D18-F3-S) and re-running EVAL-DAY recording, which supersedes the
evaluation rather than rewriting it.

WHY THE MIXED SESSION
---------------------
The day is deliberately NOT a single auto-closed session. It is a morning
session closed by ``STALE_AUTOCLOSE`` followed by an afternoon session the
Worker closed properly, so ``final_departure()`` correctly selects the WORKER
close and ``ledger.departure_provenance`` reads "verified". A day shaped this
way once evaluated as an ordinary shortfall and counted minutes derived from an
exit nobody verified. §19 step 6 asks ``intervals.hasUnresolvedExit`` — a
property of the whole sealed interval set — so the deferral must survive a
verified final departure. Sealing a single synthesized session would pass
without proving that, which is the whole point of the corrected architecture.

REGISTERED IN ``EXTERNAL_EXECUTION_TESTS``
------------------------------------------
Registration is promotion-coupled: ``test_eco_golden_vectors.py`` asserts
``set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)`` and, one guard later,
``executable_in_artifact == EXECUTABLE_IDS``, so naming GV-17 in the registry
and marking it ``executable`` in the artifact are necessarily one act. This
class was therefore written first and left unregistered — exactly as
``TestGV16`` was before its own promotion audit — so that the promotion step
had something proven to promote rather than a claim to take on trust. GV-17 was
promoted on 2026-09-17 once PRODUCER, VISIBILITY-SURFACE and VIEW-AUTHORITY
were all ruled satisfied, and the registry now points here.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession

from tests._eco_policy import capture_test_policy

import database.models as dbm
from database.database import Base

from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.config_versioning import DatabaseConfigVersionProvider
from app.early_clock_out import LOCKED_COPY, OWNER_AUTHORIZED_COPY
from app.eco_day_view import read_eco_day
from app.eco_day_view.outcomes import EcoDayView
from app.eco_day_view.rendering import Audience, render_eco_day
from app.eco_view_authority import EcoViewGrant
from app.eval_day import EcoDayState, evaluate_and_record_day
from app.eval_day.models import EcoDayRecord
from app.eval_event import EventOutcome, RefusalReason, evaluate_threshold_event
from app.eval_event.models import EcoThresholdEvent
from app.eval_week import evaluate_workweek
from app.eval_week.outcomes import WeeklyState
from app.requirement_resolution import RequirementCandidate
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState
from app.schedule_policy import keys as K
from app.work_ledger import LedgerState, Provenance, WorkDayLedger, WorkStateInterval
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, WORKER, ADMIN, OUTSIDER = 7460, 7461, 7462, 7463
ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)

#: A Monday in the PAST, so D18-F3's closure gate is satisfied.
DAY = date(2026, 9, 7)
WEEK = "2026-09-07"
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
ALL_SEVEN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]
#: 09:00-13:00 plus 14:00-18:00 is eight hours, which is exactly the
#: requirement — so once the exit is resolved the day owes nothing, and the
#: recomputation cannot be mistaken for a shortfall appearing from nowhere.
REQUIRED_MINUTES = 480


def _local(on: date, hour: int, minute: int = 0) -> datetime:
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=UTC) - IST


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures — the same shape ``test_departure_resolution.py`` uses
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def engine_fk():
    import app.clock_session_boundary.models      # noqa: F401
    import app.config_versioning.models           # noqa: F401
    import app.eco_view_authority.models          # noqa: F401
    import app.eval_day.leave_snapshot            # noqa: F401
    import app.eval_day.models                    # noqa: F401
    import app.policy_version.models             # noqa: F401
    import app.break_treatment.models             # noqa: F401
    import app.break_classification.models       # noqa: F401
    import app.eval_event.models                  # noqa: F401
    import app.work_ledger.models                 # noqa: F401
    import app.work_ledger_producer.models        # noqa: F401

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
        s.add(dbm.Organization(id=ORG, name="GV17 Org"))
        s.commit()
        s.add(dbm.User(id=WORKER, name="W", email="w@gv17.test",
                       organization_id=ORG, role="employee"))
        s.add(dbm.User(id=ADMIN, name="A", email="a@gv17.test",
                       organization_id=ORG, role="employee"))
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(ALL_SEVEN)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@gv17.test")
        s.commit()
        # An explicit historical PolicyVersion. ``evaluate_and_record_day``
        # refuses with POLICY_UNRESOLVED without one — a production rule this
        # fixture satisfies rather than weakens. Carries no requirement:
        # daily_minutes stays with ConfigValue / Requirement Resolution.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


def _requirement(db):
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id="gv17-v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="TENANT_FALLBACK", rank=6,
                                         minutes=REQUIRED_MINUTES)])
    db.flush()


def _session(db, *, clock_in, clock_out, close_prov=Provenance.WORKER,
             actor="w@gv17.test"):
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=0, break_seconds=0, offline_seconds=0,
        is_active=False,
    )
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=WORKER,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=Provenance.WORKER, actor_label=actor)
    append_boundary_event(db, session_id=row.id, user_id=WORKER,
                          boundary=BOUNDARY_CLOCK_OUT, boundary_at=clock_out,
                          provenance=close_prov, actor_label=actor)
    db.commit()
    return row


def _run(db):
    return produce_and_seal_work_day(
        db, organization_id=ORG, user_id=WORKER, work_date=DAY)


def _record(db):
    outcome = evaluate_and_record_day(
        db, organization_id=ORG, user_id=WORKER, work_date=DAY)
    assert outcome.is_recorded, f"{outcome.reason} {outcome.detail}"
    db.flush()
    return outcome.record


def _generations(db):
    """Every EcoDayRecord for the day, oldest generation first."""
    return db.query(EcoDayRecord).filter(
        EcoDayRecord.organization_id == ORG,
        EcoDayRecord.user_id == WORKER,
        EcoDayRecord.work_date == DAY,
    ).order_by(EcoDayRecord.evaluation_generation).all()


def _rollout(db, state=RolloutState.ENABLED):
    """ENABLED so BOTH parties are entitled — §44.2 gives SHADOW to the
    administrator alone, which would prove only half of "visible to both".

    Sets the durable control directly, the same way ``test_eco_day_router.py``
    does: the transition service has its own suite, and GV-17 is about what a
    reader sees in a given state, not about how the state was reached.
    """
    from app.rollout.models import RolloutControl

    row = db.query(RolloutControl).filter(
        RolloutControl.organization_id == ORG,
        RolloutControl.control_key == CONTROL_EARLY_CLOCK_OUT_V2).one_or_none()
    if row is None:
        row = RolloutControl(organization_id=ORG,
                             control_key=CONTROL_EARLY_CLOCK_OUT_V2)
        db.add(row)
    row.state = state.value
    db.commit()


def _mixed_session_day(db):
    """The GV-17 input: "Day contains an unverified exit".

    Morning auto-closed, afternoon closed by the Worker. The LAST departure is
    verified; the day's evidence still holds an unverified exit.
    """
    _requirement(db)
    morning = _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 13),
                       close_prov=Provenance.STALE_AUTOCLOSE,
                       actor="system@gv17.test")
    _session(db, clock_in=_local(DAY, 14), clock_out=_local(DAY, 18),
             close_prov=Provenance.WORKER)
    return morning


# ══════════════════════════════════════════════════════════════════════════════
# GV-17
# ══════════════════════════════════════════════════════════════════════════════
class TestGV17:
    """§24.7 — "neither counted nor excused", proven against the real chain."""

    @staticmethod
    def _vector():
        data = json.loads(VECTORS.read_text(encoding="utf-8"))
        return [v for v in data["vectors"] if v["id"] == "GV-17"][0]

    # ── the promoted artifact ───────────────────────────────────────────────
    def test_the_vector_is_promoted(self):
        """Promoted 2026-09-17, after the final promotion-readiness audit.

        This guard previously asserted the opposite, deliberately: while the
        harness existed but the vector was unproven, the risk worth guarding
        against was promoting it quietly. That risk is gone; the claim inverts,
        exactly as ``TestGV16``'s did at its own promotion.
        """
        v = self._vector()
        assert v["harness"]["execution"] == "executable"
        assert v["harness"]["blocking_dependencies"] == []

    def test_this_harness_is_registered_and_points_here(self):
        """Registration and execution status cannot be separated.

        The golden suite asserts ``set(EXTERNAL_EXECUTION_TESTS) <=
        set(EXECUTABLE_IDS)``, so a promoted vector must name the suite that
        actually runs it. This is the other end of that link: the registry must
        point at THIS class, by the exact spelling the guard resolves.
        """
        src = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-17": ("test_gv17_execution.py", "class TestGV17")' in src
        # And the coupling that made registration impossible before promotion
        # is still in force — it is what keeps the two from drifting apart.
        assert "assert set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)" in src

    # ── 1. the unverified exit, through the producer ────────────────────────
    def test_the_sealed_day_carries_a_verified_final_departure(self, db):
        """The trap the corrected architecture has to survive.

        If this ever reads STALE_AUTOCLOSE the scenario has stopped being a
        mixed session, and the deferral below would prove nothing.
        """
        _mixed_session_day(db)
        assert _run(db).outcome is ProducerOutcome.SEALED
        ledger = db.query(WorkDayLedger).one()
        assert ledger.state == LedgerState.SEALED.value
        assert ledger.departure_provenance == Provenance.WORKER.value

        intervals = db.query(WorkStateInterval).order_by(
            WorkStateInterval.id).all()
        assert [i.provenance for i in intervals] == [
            Provenance.STALE_AUTOCLOSE.value, Provenance.WORKER.value]

    # ── 2. EVAL-DAY, EVAL-WEEK, visibility, rendering ───────────────────────
    def test_gv17_executes(self, db):
        expected = self._vector()["expected_fields"]
        handled = []

        _mixed_session_day(db)
        assert _run(db).outcome is ProducerOutcome.SEALED
        record = _record(db)

        # ── expected: state ─────────────────────────────────────────────────
        assert record.state == expected["state"]
        assert record.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        for refused in ("SHORTFALL", "NO_SHORTFALL", "EVALUABLE",
                        "REQUIREMENT_UNRESOLVED"):
            assert record.state != refused
        handled.append("state")

        # ── expected: contributes_to_accumulator_minutes ────────────────────
        assert record.contributes_to_accumulator_minutes == \
            expected["contributes_to_accumulator_minutes"] == 0
        handled.append("contributes_to_accumulator_minutes")

        # Nothing was fabricated to get there. §9.3 is honoured by not
        # computing the minutes at all, not by computing and hiding them.
        assert record.requirement_outcome == "NOT_APPLICABLE"
        assert record.shortfall_computed is False
        for null_field in ("required_minutes", "qualifying_minutes",
                           "gross_shortfall_minutes", "exclusion_minutes",
                           "attributable_minutes", "schedule_variance_minutes"):
            assert getattr(record, null_field) is None, null_field

        # ── the week defers, and cannot be totalled ─────────────────────────
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=WEEK)
        assert week.state is WeeklyState.DEFERRED_DAY_PRESENT
        assert week.state is not WeeklyState.COMPLETE
        assert week.weekly_attributable_shortfall_minutes is None

        # ── no threshold event, and therefore nothing to notify about ───────
        event = evaluate_threshold_event(
            db, organization_id=ORG, user_id=WORKER, workweek_key=WEEK)
        assert event.outcome is EventOutcome.REFUSED
        assert event.refusal is RefusalReason.WEEK_NOT_FINAL
        assert event.event_id is None
        assert db.query(EcoThresholdEvent).count() == 0

        # ── expected: visible_to_worker ─────────────────────────────────────
        _rollout(db)
        worker_view = read_eco_day(db, caller=db.get(dbm.User, WORKER),
                                   user_id=WORKER, work_date=DAY)
        assert isinstance(worker_view, EcoDayView), worker_view
        worker = render_eco_day(worker_view, audience=Audience.WORKER)
        assert worker.status == "Deferred"
        assert worker.body == OWNER_AUTHORIZED_COPY["eco.deferred.body"]
        assert expected["visible_to_worker"] is True
        handled.append("visible_to_worker")

        # ── expected: visible_to_administrator ──────────────────────────────
        # Through the persisted grant, never inferred. The un-granted
        # administrator is refused first, so the grant is doing the work.
        denial = read_eco_day(db, caller=db.get(dbm.User, ADMIN),
                              user_id=WORKER, work_date=DAY)
        assert not isinstance(denial, EcoDayView), (
            "an administrator without a grant was allowed to read")

        db.add(EcoViewGrant(organization_id=ORG, user_id=ADMIN,
                            granted_by="owner@gv17.test"))
        db.commit()
        admin_view = read_eco_day(db, caller=db.get(dbm.User, ADMIN),
                                  user_id=WORKER, work_date=DAY)
        assert isinstance(admin_view, EcoDayView), admin_view
        admin = render_eco_day(admin_view, audience=Audience.ADMINISTRATOR)
        assert admin.status == "Deferred"
        assert OWNER_AUTHORIZED_COPY["eco.deferred.body"] in admin.body
        # §29.1 / §33.4 — in the body, not a footer.
        assert LOCKED_COPY["eco.admin.disclaimer"] in admin.body
        assert expected["visible_to_administrator"] is True
        handled.append("visible_to_administrator")

        # ── expected: recomputes_on_resolution ──────────────────────────────
        # Executed in full by ``test_recomputes_on_resolution`` below; asserted
        # here so the field is covered by the completeness check.
        assert expected["recomputes_on_resolution"] is True
        handled.append("recomputes_on_resolution")

        assert set(handled) == set(expected), (
            f"expected_fields not compared: {set(expected) - set(handled)}")

    # ── 3. "recomputes on resolution" — the clause nothing else proves ──────
    def test_recomputes_on_resolution(self, db):
        """Resolve the exit through the authoritative mechanism and re-evaluate.

        Nothing here rewrites history: the boundary event is APPENDED, the
        producer decides supersession from the source fingerprint, and the
        recording layer supersedes the evaluation rather than mutating it.
        """
        morning = _mixed_session_day(db)
        assert _run(db).outcome is ProducerOutcome.SEALED
        first_ledger_id = db.query(WorkDayLedger).one().id
        deferred = _record(db)
        db.commit()
        assert deferred.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        first_generation = deferred.evaluation_generation
        first_record_id = deferred.id
        first_ledger_version = deferred.ledger_version

        # ── RESOLUTION ──────────────────────────────────────────────────────
        # An administrator verifies the morning departure. D-15 is append-only
        # and the HIGHEST sequence for a (session, boundary) is authoritative,
        # so this supersedes the auto-close without deleting it. The instant is
        # unchanged: only the provenance moves from synthesized to verified, so
        # nothing else can explain the recomputation.
        append_boundary_event(
            db, session_id=morning.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_OUT, boundary_at=_local(DAY, 13),
            provenance=Provenance.ADMIN_OVERRIDE, actor_label="a@gv17.test")
        db.commit()

        # ── the corrected canonical ledger, from the producer ───────────────
        corrected = _run(db)
        db.commit()
        assert corrected.outcome is ProducerOutcome.SUPERSEDED
        assert corrected.superseded_from_version == first_ledger_version
        assert corrected.ledger_version == first_ledger_version + 1
        old_ledger = db.get(WorkDayLedger, first_ledger_id)
        assert old_ledger.superseded_by == corrected.ledger_id
        # The corrected day has no unverified exit left anywhere in it.
        new_ledger = db.get(WorkDayLedger, corrected.ledger_id)
        assert new_ledger.departure_provenance == Provenance.WORKER.value
        new_intervals = db.query(WorkStateInterval).filter(
            WorkStateInterval.ledger_id == corrected.ledger_id).all()
        assert {i.provenance for i in new_intervals} == {
            Provenance.ADMIN_OVERRIDE.value, Provenance.WORKER.value}

        # ── re-run EVAL-DAY recording ───────────────────────────────────────
        resolved = _record(db)
        db.commit()

        # A NEW generation, not an overwrite.
        assert resolved.id != first_record_id
        assert resolved.evaluation_generation != first_generation
        assert resolved.evaluation_generation > first_generation
        assert resolved.supersedes == first_record_id
        assert resolved.ledger_version == corrected.ledger_version

        # The original is preserved, untouched, and marked superseded.
        db.refresh(deferred)
        assert deferred.id == first_record_id
        assert deferred.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert deferred.evaluation_generation == first_generation
        assert deferred.ledger_version == first_ledger_version
        assert deferred.contributes_to_accumulator_minutes == 0
        assert deferred.superseded_by == resolved.id

        # Exactly one authoritative generation.
        generations = _generations(db)
        assert len(generations) == 2
        authoritative = [g for g in generations if g.superseded_by is None]
        assert [g.id for g in authoritative] == [resolved.id]

        # ── the day is now evaluated from the corrected ledger ──────────────
        assert resolved.state != EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert resolved.state == EcoDayState.NO_SHORTFALL.value
        assert resolved.shortfall_computed is True
        assert resolved.required_minutes == REQUIRED_MINUTES
        assert resolved.qualifying_minutes == REQUIRED_MINUTES

        # ── and the visibility read follows the authoritative generation ────
        _rollout(db)
        view = read_eco_day(db, caller=db.get(dbm.User, WORKER),
                            user_id=WORKER, work_date=DAY)
        assert isinstance(view, EcoDayView), view
        assert view.evaluation_state == resolved.state
        assert view.evaluation_generation == resolved.evaluation_generation
        rendered = render_eco_day(view, audience=Audience.WORKER)
        assert rendered.status == "Complete"
        assert rendered.status != "Deferred"

        # The week can now be totalled, where before it could not.
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=WEEK)
        assert week.state is not WeeklyState.DEFERRED_DAY_PRESENT

    def test_replaying_the_evaluation_creates_no_further_generation(self, db):
        """Idempotency survives the correction: an identical re-run is a replay,
        not a third generation."""
        _mixed_session_day(db)
        _run(db)
        _record(db)
        db.commit()
        assert len(_generations(db)) == 1

        again = _record(db)
        db.commit()
        assert len(_generations(db)) == 1
        assert again.evaluation_generation == _generations(db)[0].evaluation_generation
