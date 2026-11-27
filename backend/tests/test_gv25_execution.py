"""
GV-25 — "Idempotent replay (§22)" executed end to end.
======================================================

The vector, verbatim:

    scenario   "Idempotent replay (§22)"
    inputs     "Replay every event in the fixture set twice"
    expected   "Identical final state; no duplicate events, records or
                notifications"

``TestGV25`` drives the REAL pipeline and replays each of the three operations
the vector names:

    PolicyVersion -> Requirement Resolution -> producer -> sealed ledger
      -> EVAL-DAY      (day-evaluation record)     replayed
      -> EVAL-WEEK
      -> EVAL-EVENT    (threshold event)           replayed
      -> ALERT         (durable notification)      replayed

SCOPE, BY OWNER RULING
----------------------
``duplicate_notifications: 0`` means no duplicate durable notification RECORD.
It does not mean dispatch: email, websocket, push, delivery attempts, retries
and recipient/channel identity all remain ALERT-DELIVERY and are outside this
vector. The inert delivery-attempt table is never touched here.

"The fixture set" is the three outcomes the vector itself names. §22.1's wider
table also covers clock-out submission, exclusion application and correction;
those are outside GV-25 and are not exercised.

"BYTE-IDENTICAL", PRECISELY
---------------------------
The persisted evaluation CONTENT, compared through ``content_hash`` — the
canonical projection EVAL-DAY already computes. Not literal row bytes: primary
keys and storage metadata are not part of the evaluation, and redefining
persistence to make raw rows match would be changing the system to fit the test.

REGISTERED IN ``EXTERNAL_EXECUTION_TESTS``
------------------------------------------
Registration is promotion-coupled — ``test_eco_golden_vectors.py`` asserts
``set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)`` and, one guard later,
``executable_in_artifact == EXECUTABLE_IDS`` — so naming GV-25 in the registry
and marking it ``executable`` are necessarily one act. This class was written
first and left unregistered, exactly as ``TestGV16``, ``TestGV17`` and
``TestGV27`` were, so the promotion step had something proven to promote.
GV-25 was promoted on 2026-09-18.
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

from app.alert import (
    NotificationAudience,
    NotificationOutcome,
    record_threshold_notification,
)
from app.alert.models import EcoNotification
from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.config_versioning import DatabaseConfigVersionProvider
from app.eval_day import EcoDayState, evaluate_and_record_day
from app.eval_day.models import EcoDayRecord
from app.eval_event import EventOutcome, evaluate_threshold_event
from app.eval_event.models import EcoThresholdEvent
from app.eval_week import evaluate_workweek
from app.eval_week.outcomes import WeeklyState
from app.policy_version import capture_policy_version
from app.requirement_resolution import RequirementCandidate
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState
from app.schedule_policy import keys as K
from app.work_ledger import Provenance
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, WORKER = 7540, 7541
ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)

DAY = date(2026, 9, 7)          # a Monday in the past
WEEK_KEY = "2026-09-07"
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
#: One working day, so a single sealed day completes the week.
WORKING_DAYS = ["MONDAY"]

REQUIRED_MINUTES = 480
WORKED_MINUTES = 400            # 80 short — comfortably over the threshold
THRESHOLD_MINUTES = 30

CONFIG_KEY = "gv25-config-v1"
POLICY_KEY = "gv25-policy-v1"


def _vector() -> dict:
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    return [v for v in data["vectors"] if v["id"] == "GV-25"][0]


def _local(on: date, hour: int, minute: int = 0) -> datetime:
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=UTC) - IST


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def engine_fk():
    import app.alert.models                       # noqa: F401
    import app.clock_session_boundary.models      # noqa: F401
    import app.config_versioning.models           # noqa: F401
    import app.eval_day.leave_snapshot            # noqa: F401
    import app.eval_day.models                    # noqa: F401
    import app.eval_event.models                  # noqa: F401
    import app.policy_version.models              # noqa: F401
    import app.break_treatment.models             # noqa: F401
    import app.break_classification.models       # noqa: F401
    import app.rollout.models                     # noqa: F401
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
        s.add(dbm.Organization(id=ORG, name="GV25 Org"))
        s.commit()
        s.add(dbm.User(id=WORKER, name="W", email="w@gv25.test",
                       organization_id=ORG, role="employee"))
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(WORKING_DAYS)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@gv25.test")
        s.commit()
        yield s
    _tz.invalidate_tz_cache()


def _config(db):
    """The requirement and the weekly threshold, in one version."""
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id=CONFIG_KEY,
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="TENANT_FALLBACK", rank=6,
                                         minutes=REQUIRED_MINUTES)])
    db.flush()

    from app.config_versioning.models import ConfigValue, ConfigVersion
    from app.eco_threshold import (
        ECO_THRESHOLD_KEY, ECO_THRESHOLD_RANK, ECO_THRESHOLD_TIER,
    )
    version = db.query(ConfigVersion).filter_by(version_key=CONFIG_KEY).one()
    db.add(ConfigValue(
        config_version_id=version.id, organization_id=ORG, scope_user_id=None,
        worker_classification=None, tier=ECO_THRESHOLD_TIER,
        rank=ECO_THRESHOLD_RANK, key=ECO_THRESHOLD_KEY,
        value_minutes=THRESHOLD_MINUTES))
    db.commit()


def _policy(db, *, key=POLICY_KEY, effective_from=date(2026, 1, 1),
            netting_mode="NONE", grace_minutes=0,
            netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
            surplus_cap_per_day_minutes=None,
            surplus_cap_per_period_minutes=None):
    capture_policy_version(
        db, organization_id=ORG, policy_version=key,
        effective_from=effective_from, counts_toward_week=True,
        netting_mode=netting_mode, grace_minutes=grace_minutes,
        netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
        surplus_cap_per_day_minutes=surplus_cap_per_day_minutes,
        surplus_cap_per_period_minutes=surplus_cap_per_period_minutes,
        captured_by="owner@gv25.test")
    db.commit()


def _rollout(db, state=RolloutState.ENABLED):
    """ALERT records only under an enabled rollout (D8)."""
    from app.rollout.models import RolloutControl

    row = RolloutControl(organization_id=ORG,
                         control_key=CONTROL_EARLY_CLOCK_OUT_V2)
    row.state = state.value
    db.add(row)
    db.commit()


def _seal(db):
    start = _local(DAY, 9)
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG, clock_in_time=start,
        clock_out_time=start + timedelta(minutes=WORKED_MINUTES),
        total_seconds=0, break_seconds=0, offline_seconds=0, is_active=False)
    db.add(row)
    db.flush()
    for boundary, at in ((BOUNDARY_CLOCK_IN, row.clock_in_time),
                         (BOUNDARY_CLOCK_OUT, row.clock_out_time)):
        append_boundary_event(db, session_id=row.id, user_id=WORKER,
                              boundary=boundary, boundary_at=at,
                              provenance=Provenance.WORKER,
                              actor_label="w@gv25.test")
    db.commit()
    assert produce_and_seal_work_day(
        db, organization_id=ORG, user_id=WORKER,
        work_date=DAY).outcome is ProducerOutcome.SEALED
    db.commit()


def _evaluate(db):
    out = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                  work_date=DAY)
    assert out.is_recorded, f"{out.reason} {out.detail}"
    db.commit()
    return out.record


def _scenario(db):
    """Everything GV-25 replays, set up once."""
    _config(db)
    _policy(db)
    _rollout(db)
    _seal(db)


def _records(db):
    return db.query(EcoDayRecord).filter(
        EcoDayRecord.organization_id == ORG,
        EcoDayRecord.user_id == WORKER,
        EcoDayRecord.work_date == DAY,
    ).order_by(EcoDayRecord.evaluation_generation).all()


# ══════════════════════════════════════════════════════════════════════════════
# GV-25
# ══════════════════════════════════════════════════════════════════════════════
class TestGV25:
    """§22 — replay changes nothing, and the policy identity is load-bearing."""

    # ── the artifact ────────────────────────────────────────────────────────
    def test_the_vector_is_promoted(self):
        """Promoted 2026-09-18, once POLICY-VERSION was satisfied end to end.

        This guard asserted the opposite while the harness existed but no
        promotion had been authorized — the risk then was promoting quietly.
        That risk is gone; the claim inverts, exactly as GV-16's, GV-17's and
        GV-27's did at their own promotions.
        """
        harness = _vector()["harness"]
        assert harness["execution"] == "executable"
        assert harness["blocking_dependencies"] == []

    def test_this_harness_is_registered_and_points_here(self):
        """Registration and execution status cannot be separated.

        The golden suite asserts ``set(EXTERNAL_EXECUTION_TESTS) <=
        set(EXECUTABLE_IDS)``, so a promoted vector must name the suite that
        runs it. This is the other end of that link, by the exact spelling the
        guard resolves.
        """
        src = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-25": ("test_gv25_execution.py", "class TestGV25")' in src
        assert "assert set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)" in src

    # ── the vector ──────────────────────────────────────────────────────────
    def test_gv25_executes(self, db):
        expected = _vector()["expected_fields"]
        handled = []

        _scenario(db)

        # ── A. Day evaluation, replayed ─────────────────────────────────────
        first = _evaluate(db)
        before = (first.id, first.content_hash, first.evaluation_generation)
        again = _evaluate(db)

        assert again.id == first.id                       # the same row
        assert (again.id, again.content_hash,
                again.evaluation_generation) == before    # unchanged
        assert len(_records(db)) == 1
        handled.append("duplicate_records")

        # ── B. Threshold event, replayed ────────────────────────────────────
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=WEEK_KEY)
        assert week.state is WeeklyState.COMPLETE
        assert week.weekly_attributable_shortfall_minutes == (
            REQUIRED_MINUTES - WORKED_MINUTES)

        crossed = evaluate_threshold_event(
            db, organization_id=ORG, user_id=WORKER, workweek_key=WEEK_KEY)
        db.commit()
        assert crossed.outcome is EventOutcome.CROSSED
        replayed = evaluate_threshold_event(
            db, organization_id=ORG, user_id=WORKER, workweek_key=WEEK_KEY)
        db.commit()
        assert replayed.outcome is EventOutcome.ALREADY_CROSSED
        assert replayed.event_id == crossed.event_id

        events = db.query(EcoThresholdEvent).filter(
            EcoThresholdEvent.organization_id == ORG,
            EcoThresholdEvent.user_id == WORKER,
            EcoThresholdEvent.workweek_key == WEEK_KEY).all()
        assert len(events) == 1
        handled.append("duplicate_events")

        # ── C. Durable notification record, replayed ────────────────────────
        # Records only. Owner ruling: dispatch is ALERT-DELIVERY and outside
        # this vector, so nothing is delivered and no attempt is written.
        recorded = record_threshold_notification(
            db, event_id=crossed.event_id, audience=NotificationAudience.WORKER)
        db.commit()
        assert recorded.outcome is NotificationOutcome.RECORDED
        for _ in range(3):
            replay = record_threshold_notification(
                db, event_id=crossed.event_id,
                audience=NotificationAudience.WORKER)
            db.commit()
            assert replay.outcome is NotificationOutcome.ALREADY_RECORDED

        notifications = db.query(EcoNotification).filter(
            EcoNotification.organization_id == ORG,
            EcoNotification.user_id == WORKER).all()
        assert len(notifications) == 1
        handled.append("duplicate_notifications")

        # ── final state identical ───────────────────────────────────────────
        # The canonical projection, not raw row bytes (owner ruling).
        assert _records(db)[0].content_hash == before[1]
        assert [r.content_hash for r in _records(db)] == [before[1]]
        assert expected["final_state_identical"] is True
        handled.append("final_state_identical")

        assert set(handled) == set(expected), (
            f"expected_fields not compared: {set(expected) - set(handled)}")

    # ── policy discrimination (owner ruling) ────────────────────────────────
    def test_case_1_same_policy_replays_to_the_same_record(self, db):
        """The identity holds every other dimension constant, so the only
        thing this can be measuring is the replay itself."""
        _scenario(db)
        first = _evaluate(db)
        again = _evaluate(db)

        assert again.id == first.id
        assert again.content_hash == first.content_hash
        assert (again.ledger_version, again.policy_version,
                again.config_version, again.leave_input_snapshot_key) == \
               (first.ledger_version, first.policy_version,
                first.config_version, first.leave_input_snapshot_key)
        assert len(_records(db)) == 1

    def test_case_2_a_different_policy_is_a_different_evaluation(self, db):
        """`policy_version` is load-bearing, not decorative provenance.

        A RETROACTIVE capture — effective before the work date but after the
        first policy — is what makes the same sealed ledger resolve a different
        policy. Nothing else about the day changes.
        """
        _scenario(db)
        first = _evaluate(db)
        assert first.policy_version == POLICY_KEY
        # Captured BEFORE the second evaluation, so the immutability assertion
        # below compares against a value taken earlier rather than against the
        # row's own current state.
        first_id, first_hash = first.id, first.content_hash
        first_generation = first.evaluation_generation

        # A genuinely DIFFERENT policy, which is all this case needs. The caps
        # are stated because MODEL-NETTING refuses a bank mode that records
        # none (§15.1, D-6.8) — an uncapped bank is a policy gap, not a
        # default. GV-25 is about policy identity, not about netting, so the
        # values simply have to exist.
        _policy(db, key="gv25-policy-v2",
                effective_from=DAY - timedelta(days=2),
                netting_mode="WEEKLY_BANK", grace_minutes=5,

                surplus_cap_per_day_minutes=60,
                surplus_cap_per_period_minutes=60)
        second = _evaluate(db)

        # A new evaluation, referencing the new policy.
        assert second.id != first_id
        assert second.policy_version == "gv25-policy-v2"
        assert second.evaluation_generation > first_generation
        assert second.supersedes == first_id
        assert second.content_hash != first_hash

        # The previous evaluation is untouched and marked superseded. Compared
        # against the values captured before the supersession, not against the
        # row's own current state — which would assert nothing.
        db.expire_all()
        prior = db.get(EcoDayRecord, first_id)
        assert prior.policy_version == POLICY_KEY
        assert prior.content_hash == first_hash
        assert prior.evaluation_generation == first_generation
        assert prior.superseded_by == second.id

        # Exactly one authoritative generation.
        authoritative = [r for r in _records(db) if r.superseded_by is None]
        assert [r.id for r in authoritative] == [second.id]

        # And the SEALED EVIDENCE did not move: same ledger, same version.
        assert second.ledger_version == prior.ledger_version

    def test_the_replay_under_the_new_policy_is_also_idempotent(self, db):
        _scenario(db)
        _evaluate(db)
        _policy(db, key="gv25-policy-v2",
                effective_from=DAY - timedelta(days=2))
        a = _evaluate(db)
        b = _evaluate(db)
        assert b.id == a.id
        assert len(_records(db)) == 2        # one superseded, one current

    # ── what this vector deliberately does not touch ────────────────────────
    def test_no_delivery_was_attempted(self, db):
        """Owner ruling: GV-25 is about durable records. The delivery-attempt
        table stays inert — ALERT-DELIVERY remains out of scope."""
        from app.alert.models import EcoNotificationDeliveryAttempt

        _scenario(db)
        _evaluate(db)
        evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                          workweek_key=WEEK_KEY)
        crossed = evaluate_threshold_event(
            db, organization_id=ORG, user_id=WORKER, workweek_key=WEEK_KEY)
        db.commit()
        record_threshold_notification(db, event_id=crossed.event_id)
        db.commit()

        assert db.query(EcoNotificationDeliveryAttempt).count() == 0
