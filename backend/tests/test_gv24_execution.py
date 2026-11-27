"""
GV-24 — "Grace (§11)" executed end to end.
==========================================

The vector, verbatim:

    scenario   "Grace (§11)"
    inputs     "attributable 3; grace_minutes 5"
    expected   "attributable zeroed to 0; gross retained as 3 in the record"

``TestGV24`` drives the real recording boundary — PolicyVersion capture ->
historical resolution -> sealed ledger -> ``evaluate_and_record_day`` -> a
persisted ``EcoDayRecord``. Nothing is stubbed and no arithmetic is restated:
the expected values are read from the locked artifact.

WHAT §11 ACTUALLY SAYS, AND WHY IT IS A COLUMN
----------------------------------------------
    grace_minutes | integer | Per-day de-minimis below which gross shortfall is
    recorded but not attributed. Default 0.

and the pseudocode, step 13:

    attributable := remaining
    IF attributable <= req.grace_minutes: attributable := 0

So grace ZEROES the attribution while the gross gap stays on the record. That
leaves ``gross - exclusion != attributable``, which is indistinguishable from an
exclusion-based reduction unless the row also says what grace was applied —
hence ``grace_minutes_applied``, which §32's own record schema lists. §11's
stated purpose is that tenants do not get grace implemented "as a magic number
in the threshold comparison, which is where it does the most damage", so no
threshold is consulted anywhere in this path.

THE SOURCE IS THE VERSIONED POLICY, NOT THE REQUIREMENT
-------------------------------------------------------
The pseudocode spells it ``req.grace_minutes``; §11 places the attribute on the
Policy object, and the owner ruled the Policy authoritative. The requirement
model therefore gains nothing, and the value used for a given day is the one
resolved HISTORICALLY against that work date — never the newest policy, never a
live setting. Both halves are asserted below, statically and behaviourally.

REGISTERED IN ``EXTERNAL_EXECUTION_TESTS``
-------------------------------------------
Registration is promotion-coupled: ``test_eco_golden_vectors.py`` asserts
``set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)`` and, one guard later,
``executable_in_artifact == EXECUTABLE_IDS``, so naming GV-24 in the registry
and marking it ``executable`` are necessarily one act. This class was written
first and left unregistered — exactly as ``TestGV16``, ``TestGV17``,
``TestGV25`` and ``TestGV27`` were — so that promotion had something already
proven to promote. GV-24 was promoted on 2026-09-18.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest

from app.recipient_authority import EcoRecipientDesignation
from app.config_versioning import ConfigValue, ConfigVersion, compute_content_hash
from app.eval_day import EcoDayRecord, EcoDayState, evaluate_and_record_day
from app.eval_day.evaluator import DayEvaluation, evaluate_day
from app.leave_intervals import LeaveInterval, LeaveIntervalSet
from app.policy_version import PolicyVersion, capture_policy_version
from app.break_treatment import BreakTreatmentProfile
from app.break_classification import (
    BreakClassificationAssertion, ClassificationSnapshot,
    EcoClassificationAuthority,
)
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG = 9240
USER = 9241

#: A Monday well inside the ledger's ordinary range.
MONDAY_WEEK = "2026-08-31"
DAY = date(2026, 8, 31)
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

# ── Independently transcribed, so a wrong production constant cannot agree
#    with itself ──────────────────────────────────────────────────────────────
REQUIRED_MINUTES = 480
GV24_GRACE = 5
GV24_GROSS = 3

#: Far below every work date these tests use, so one capture governs them all.
FLOOR = date(2020, 1, 1)


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import LeaveRequest, Organization, User

    for table in (Organization, User, LeaveRequest):
        table.__table__.create(engine, checkfirst=True)

    from app.eval_day import LeaveInputContribution, LeaveInputSnapshot

    for model in (WorkDayLedger, WorkStateInterval, ConfigVersion, ConfigValue,
                  PolicyVersion, BreakTreatmentProfile,
                  EcoClassificationAuthority,
                  BreakClassificationAssertion,
                  ClassificationSnapshot,
                  EcoDayRecord, LeaveIntervalSet, LeaveInterval,
                  LeaveInputSnapshot, LeaveInputContribution):
        model.__table__.create(engine, checkfirst=True)


def _vector() -> dict:
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == "GV-24":
            return v
    raise AssertionError("GV-24 is missing from the artifact")


# ══════════════════════════════════════════════════════════════════════════════
# Scenario construction — the same canonical shapes MODEL-EXCLUSION uses
# ══════════════════════════════════════════════════════════════════════════════
def _config(db, *, minutes=REQUIRED_MINUTES, effective_from=FLOOR):
    values = [{
        "scope_user_id": None, "tier": "TENANT_FALLBACK", "rank": 6,
        "key": "required_minutes", "value_minutes": minutes, "source_ref": None,
    }]
    key = f"gv24-{effective_from.isoformat()}-{minutes}"
    version = ConfigVersion(
        organization_id=ORG, version_key=key, effective_from=effective_from,
        captured_at_utc=datetime(2020, 1, 1, tzinfo=UTC),
        captured_by_label="SYSTEM",
        content_hash=compute_content_hash(
            organization_id=ORG, version_key=key,
            effective_from=effective_from, values=values),
    )
    db.add(version)
    db.flush()
    for value in values:
        db.add(ConfigValue(config_version_id=version.id,
                           organization_id=ORG, **value))
    db.flush()
    return version


def _policy(db, *, grace_minutes, key="gv24-policy-v1", effective_from=FLOOR):
    """Capture one immutable PolicyVersion. No commit: the suite rolls back."""
    row = capture_policy_version(
        db, organization_id=ORG, policy_version=key,
        effective_from=effective_from, counts_toward_week=True,
        netting_mode="NONE", grace_minutes=grace_minutes,
        netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
        captured_by="owner@gv24.test")
    db.flush()
    return row


def _worked_day(db, *, gap_minutes, on=DAY):
    """A sealed WRK-only day whose GROSS shortfall is exactly ``gap_minutes``."""
    led = WorkDayLedger(
        organization_id=ORG, user_id=USER,
        work_date=datetime(on.year, on.month, on.day),
        workweek_key=MONDAY_WEEK, ledger_version=1,
        state=LedgerState.SEALED.value, timezone_iana="Asia/Kolkata",
        is_working_day=True, working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST",
    )
    db.add(led)
    db.flush()

    seconds = (REQUIRED_MINUTES - gap_minutes) * 60
    begin = datetime(on.year, on.month, on.day, 9, 0, tzinfo=UTC)
    db.add(WorkStateInterval(
        ledger_id=led.id, organization_id=ORG, seq=1, state="WRK",
        start_at_utc=begin, end_at_utc=begin + timedelta(seconds=seconds),
        duration_seconds=seconds, provenance="WORKER", created_at=NOW))
    db.flush()
    return led


_request_seq = [92400]


def _leave(db, *, minutes, on=DAY):
    """An approved leave grant with intraday intervals, built canonically."""
    from database.models import LeaveRequest

    _request_seq[0] += 1
    request = LeaveRequest(
        id=_request_seq[0], user_id=USER, organization_id=ORG,
        leave_type="Casual Leave", from_date=datetime(on.year, on.month, on.day),
        to_date=datetime(on.year, on.month, on.day), description="x",
        status="approved",
    )
    db.add(request)
    db.flush()

    interval_set = LeaveIntervalSet(
        organization_id=ORG, user_id=USER, leave_request_id=request.id,
        set_version=1, supersedes=None, timezone_iana="Asia/Kolkata",
        recorded_by="TEST", recorded_at_utc=NOW)
    db.add(interval_set)
    db.flush()

    begin = datetime(on.year, on.month, on.day, 18, 0, tzinfo=UTC)
    db.add(LeaveInterval(
        set_id=interval_set.id, organization_id=ORG, seq=1, work_date=on,
        start_at_utc=begin, end_at_utc=begin + timedelta(minutes=minutes),
        duration_seconds=minutes * 60))
    db.flush()
    return interval_set


def _record(db, *, on=DAY):
    result = evaluate_and_record_day(
        db, organization_id=ORG, user_id=USER, work_date=on)
    assert result.is_recorded, f"{result.reason} {result.detail}"
    return result.record


def _run(db, *, gap, grace, overlap=0, on=DAY):
    """The whole scenario: requirement, policy, sealed day, optional leave."""
    _config(db)
    _policy(db, grace_minutes=grace)
    _worked_day(db, gap_minutes=gap, on=on)
    if overlap:
        _leave(db, minutes=overlap, on=on)
    return _record(db, on=on)


def _records(db, *, on=DAY):
    return db.query(EcoDayRecord).filter(
        EcoDayRecord.organization_id == ORG,
        EcoDayRecord.user_id == USER,
        EcoDayRecord.work_date == on,
    ).order_by(EcoDayRecord.evaluation_generation).all()


# ══════════════════════════════════════════════════════════════════════════════
# GV-24
# ══════════════════════════════════════════════════════════════════════════════
class TestGV24:
    """§11 grace — the one authorised way to zero an attributable shortfall."""

    # ── the artifact ────────────────────────────────────────────────────────
    def test_the_vector_is_promoted(self):
        """Promoted 2026-09-18, once MODEL-GRACE was satisfied end to end.

        This guard asserted the opposite while the harness existed but no
        promotion had been authorised — the risk then was promoting quietly.
        That risk is gone; the claim inverts, exactly as GV-16's, GV-17's,
        GV-25's and GV-27's did at their own promotions.
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
        assert '"GV-24": ("test_gv24_execution.py", "class TestGV24")' in src
        assert "assert set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)" in src

    # ── C. the vector itself ────────────────────────────────────────────────
    def test_gv24_executes(self, db):
        """The vector's own numbers, read from the artifact."""
        expected = _vector()["expected_fields"]
        assert expected["grace_minutes"] == GV24_GRACE
        assert expected["gross_shortfall_minutes_retained"] == GV24_GROSS

        record = _run(db, gap=GV24_GROSS, grace=expected["grace_minutes"])

        # "attributable zeroed to 0"
        assert record.attributable_minutes == expected["attributable_minutes"]
        # "gross retained as 3 in the record"
        assert (record.gross_shortfall_minutes
                == expected["gross_shortfall_minutes_retained"])
        # and the field that explains the difference
        assert record.grace_minutes_applied == expected["grace_minutes"]
        assert record.exclusion_minutes == 0
        assert record.required_minutes == REQUIRED_MINUTES
        assert record.qualifying_minutes == REQUIRED_MINUTES - GV24_GROSS

    def test_a_graced_day_is_still_a_recorded_shortfall(self, db):
        """§11: "gross shortfall is RECORDED but not attributed".

        Grace decides what is attributed. It does not decide whether the gap
        happened, so the state stays SHORTFALL and the gross stays on the row.
        """
        record = _run(db, gap=GV24_GROSS, grace=GV24_GRACE)
        assert record.state == EcoDayState.SHORTFALL.value
        assert record.gross_shortfall_minutes == GV24_GROSS

    def test_a_graced_day_contributes_nothing_to_the_week(self, db):
        """What grace zeroes must not reappear in the accumulator.

        This is §11's stated purpose: the de-minimis decision happens ONCE, in
        the day evaluation, rather than as "a magic number in the threshold
        comparison". If the graced minutes still flowed into the weekly total,
        the comparison would be exactly where the decision lived.
        """
        record = _run(db, gap=GV24_GROSS, grace=GV24_GRACE)
        assert record.contributes_to_accumulator_minutes == 0

    # ── A. the grace SOURCE ─────────────────────────────────────────────────
    def test_the_requirement_model_carries_no_grace(self):
        """The ruling: grace is the Policy's, and the requirement gains nothing.

        A static guard, because the failure it prevents is a SECOND source
        appearing later — at which point two answers could disagree and neither
        would be wrong by its own module's lights.
        """
        package = BACKEND / "app" / "requirement_resolution"
        for path in sorted(package.glob("*.py")):
            assert "grace" not in path.read_text(encoding="utf-8").lower(), (
                f"{path.name} mentions grace; §11 places grace on the Policy "
                f"object and the requirement model must not acquire a second "
                f"source for it")

    def test_the_policy_is_the_only_thing_that_changes_the_answer(self, db):
        """Same requirement, same evidence, same leave — only the policy moves.

        Proves the value actually comes from ``PolicyVersion`` rather than from
        anything the requirement or the ledger supplies.
        """
        _config(db)
        _policy(db, grace_minutes=0)
        _worked_day(db, gap_minutes=GV24_GROSS)
        without = _record(db)
        assert without.attributable_minutes == GV24_GROSS
        assert without.grace_minutes_applied == 0

        # A NEW version, because a captured one is immutable. Same requirement,
        # same sealed evidence; only the policy differs.
        _policy(db, grace_minutes=GV24_GRACE, key="gv24-policy-v2",
                effective_from=date(2026, 8, 1))
        with_grace = _record(db)
        assert with_grace.attributable_minutes == 0
        assert with_grace.grace_minutes_applied == GV24_GRACE
        assert (with_grace.gross_shortfall_minutes
                == without.gross_shortfall_minutes == GV24_GROSS)

    def test_grace_without_a_policy_identity_is_refused(self, db):
        """Value and provenance travel together or not at all.

        A grace with no ``policy_version`` is a number nobody can trace to the
        rule that authorised it, which is precisely the failure §11 names.
        """
        led = _worked_day(db, gap_minutes=GV24_GROSS)
        with pytest.raises(ValueError, match="without a policy_version"):
            evaluate_day(ledger=led, intervals=[], provider=None,
                         grace_minutes=GV24_GRACE)

    def test_a_negative_grace_is_refused(self, db):
        led = _worked_day(db, gap_minutes=GV24_GROSS)
        with pytest.raises(ValueError, match="grace_minutes must be >= 0"):
            evaluate_day(ledger=led, intervals=[], provider=None,
                         policy_version="p", grace_minutes=-1)

    # ── B. HISTORICAL resolution ────────────────────────────────────────────
    def test_a_later_policy_does_not_govern_an_earlier_day(self, db):
        """The newest policy is not the applicable one.

        A generous grace captured AFTER the work date must leave that day
        exactly as the policy in force at the time decided it. This is the
        whole point of resolving against ``work_date``.
        """
        _config(db)
        _policy(db, grace_minutes=0, key="gv24-then", effective_from=FLOOR)
        _policy(db, grace_minutes=GV24_GRACE, key="gv24-now",
                effective_from=DAY + timedelta(days=1))
        _worked_day(db, gap_minutes=GV24_GROSS)

        record = _record(db)
        assert record.grace_minutes_applied == 0
        assert record.attributable_minutes == GV24_GROSS
        assert record.policy_version == "gv24-then"

    def test_the_policy_effective_on_the_work_date_governs(self, db):
        """The converse: the applicable version is used, not the oldest one."""
        _config(db)
        _policy(db, grace_minutes=0, key="gv24-old", effective_from=FLOOR)
        _policy(db, grace_minutes=GV24_GRACE, key="gv24-current",
                effective_from=DAY)
        _worked_day(db, gap_minutes=GV24_GROSS)

        record = _record(db)
        assert record.policy_version == "gv24-current"
        assert record.grace_minutes_applied == GV24_GRACE
        assert record.attributable_minutes == 0

    def test_an_unresolved_policy_still_fails_closed(self, db):
        """No policy means no evaluation — and certainly no default grace."""
        _config(db)
        _worked_day(db, gap_minutes=GV24_GROSS)

        record = _record(db)
        assert record.state == EcoDayState.POLICY_UNRESOLVED.value
        assert record.grace_minutes_applied is None
        assert record.attributable_minutes is None

    # ── D. the boundary ─────────────────────────────────────────────────────
    @pytest.mark.parametrize("gap,grace,attributable", [
        (5, 5, 0),          # attributable == grace  -> zeroed
        (3, 5, 0),          # attributable <  grace  -> zeroed
        (6, 5, 6),          # attributable >  grace  -> retained ENTIRELY
        (1, 5, 0),
        (5, 0, 5),          # a grace of zero forgives nothing...
        (0, 0, 0),          # ...including on a day with no gap at all
        (0, 5, 0),
        (480, 5, 480),
    ])
    def test_the_boundary(self, db, gap, grace, attributable):
        """``<=`` and not ``<``: the spec's comparison is inclusive.

        And grace is a THRESHOLD, not a deduction — six minutes against a grace
        of five attributes six, never one.
        """
        record = _run(db, gap=gap, grace=grace)
        assert record.attributable_minutes == attributable
        assert record.gross_shortfall_minutes == gap
        assert record.grace_minutes_applied == grace

    # ── H. gross and exclusion stay independent ─────────────────────────────
    def test_gross_and_exclusion_survive_grace(self, db):
        """Grace zeroes the attribution and touches nothing else.

        The strongest case: a real gap, a real leave exclusion, and a grace that
        zeroes what is left. All four numbers must be independently true.
        """
        record = _run(db, gap=10, grace=GV24_GRACE, overlap=8)

        assert record.gross_shortfall_minutes == 10
        assert record.exclusion_minutes == 8
        assert record.grace_minutes_applied == GV24_GRACE
        assert record.attributable_minutes == 0
        # The row does NOT reconcile arithmetically, and the grace is what says
        # why. That is the entire reason the column exists.
        assert (record.gross_shortfall_minutes - record.exclusion_minutes
                != record.attributable_minutes)

    def test_exclusion_alone_still_reconciles(self, db):
        """Where grace does not bite, the old identity holds exactly."""
        record = _run(db, gap=30, grace=GV24_GRACE, overlap=8)
        assert record.gross_shortfall_minutes == 30
        assert record.exclusion_minutes == 8
        assert record.attributable_minutes == 22
        assert (record.attributable_minutes
                == record.gross_shortfall_minutes - record.exclusion_minutes)

    def test_an_unexplained_row_cannot_be_constructed(self):
        """The invariant is structural, not merely a writer's discipline.

        A ``DayEvaluation`` whose attribution departs from gross - exclusion
        with nothing accounting for it is refused before anything can store it.
        """
        common = dict(organization_id=ORG, user_id=USER, work_date=DAY,
                      ledger_version=1, state=EcoDayState.SHORTFALL,
                      requirement_outcome="RESOLVED", shortfall_computed=True,
                      required_minutes=480, qualifying_minutes=477,
                      gross_shortfall_minutes=3, exclusion_minutes=0,
                      # MODEL-NETTING put bank_credit_minutes in
                      # _MINUTE_FIELDS, so a computed state must now state
                      # it too. 0 is the truthful value here: no fold ran.
                      bank_credit_minutes=0,
                      contributes_to_accumulator_minutes=0)

        with pytest.raises(ValueError, match="does not reconcile"):
            DayEvaluation(attributable_minutes=0, **common)

        # A grace too small to account for the difference is refused too.
        with pytest.raises(ValueError, match="does not explain"):
            DayEvaluation(attributable_minutes=0, grace_minutes_applied=2,
                          **common)

        # And an unevaluated day may not carry a grace at all.
        with pytest.raises(ValueError, match="must not carry"):
            DayEvaluation(
                organization_id=ORG, user_id=USER, work_date=DAY,
                ledger_version=1, state=EcoDayState.POLICY_UNRESOLVED,
                requirement_outcome="NOT_APPLICABLE", shortfall_computed=False,
                grace_minutes_applied=GV24_GRACE)

    # ── E. replay ───────────────────────────────────────────────────────────
    def test_replay_is_byte_identical_and_writes_nothing(self, db):
        """Same ledger, same policy, same leave snapshot -> the same record.

        §22: replay is not a second evaluation. No new generation, no
        supersession, and the same content hash — which now covers the grace,
        so a replay that silently changed it could not read as identical.
        """
        first = _run(db, gap=GV24_GROSS, grace=GV24_GRACE, overlap=0)
        assert first.evaluation_generation == 1

        again = _record(db)
        assert again.id == first.id
        assert again.content_hash == first.content_hash
        assert again.grace_minutes_applied == GV24_GRACE
        assert len(_records(db)) == 1
        assert _records(db)[0].superseded_by is None

    def test_the_grace_is_inside_the_content_hash(self, db):
        """Two evaluations differing only by grace are not the same result."""
        led = _worked_day(db, gap_minutes=GV24_GROSS)
        fields = {
            "organization_id": ORG, "user_id": USER, "work_date": DAY,
            "ledger_version": led.ledger_version,
            "state": EcoDayState.SHORTFALL, "requirement_outcome": "RESOLVED",
            "shortfall_computed": True, "policy_version": "p",
            "required_minutes": 480, "qualifying_minutes": 477,
            "gross_shortfall_minutes": 3, "exclusion_minutes": 0,
            # MODEL-NETTING put bank_credit_minutes in _MINUTE_FIELDS, so a
            # computed state must state it. 0: no fold ran for this day.
            "bank_credit_minutes": 0,
            "contributes_to_accumulator_minutes": 0,
        }
        forgiven = DayEvaluation(attributable_minutes=0,
                                 grace_minutes_applied=5, **fields)
        charged = DayEvaluation(attributable_minutes=3,
                                grace_minutes_applied=0, **fields)
        assert forgiven.result_fields() != charged.result_fields()
        assert "grace_minutes_applied" in forgiven.result_fields()

    # ── F. policy discrimination ────────────────────────────────────────────
    def test_a_new_policy_version_produces_a_new_generation(self, db):
        """Policy identity is part of the snapshot identity.

        A different governing policy is a different evaluation of the same
        evidence, so it supersedes rather than conflicting or replaying.
        """
        _config(db)
        _policy(db, grace_minutes=0, key="gv24-gen1", effective_from=FLOOR)
        _worked_day(db, gap_minutes=GV24_GROSS)
        first = _record(db)

        _policy(db, grace_minutes=GV24_GRACE, key="gv24-gen2",
                effective_from=date(2026, 8, 1))
        second = _record(db)

        assert second.id != first.id
        assert second.evaluation_generation == first.evaluation_generation + 1
        assert second.supersedes == first.id
        assert second.content_hash != first.content_hash
        db.refresh(first)
        assert first.superseded_by == second.id
        # The superseded record keeps its own answer, unrewritten.
        assert first.grace_minutes_applied == 0
        assert first.attributable_minutes == GV24_GROSS

    def test_grace_cannot_change_inside_a_captured_version(self, db):
        """There is no "current mutable policy" to read.

        A version is append-only, so the only way grace can change is a NEW
        version — which is exactly why the test above is a supersession and not
        a silent rewrite of an existing day's governing value.
        """
        from app.policy_version.errors import (
            PolicyVersionCollision, PolicyVersionImmutable,
        )

        row = _policy(db, grace_minutes=0, key="gv24-frozen")
        row.grace_minutes = GV24_GRACE
        with pytest.raises(PolicyVersionImmutable):
            db.flush()
        db.rollback()

        _policy(db, grace_minutes=0, key="gv24-frozen")
        with pytest.raises(PolicyVersionCollision):
            _policy(db, grace_minutes=GV24_GRACE, key="gv24-frozen")

    # ── G. no downstream side effects ───────────────────────────────────────
    def test_applying_grace_emits_no_event_and_no_notification(self, db):
        """EVAL-DAY has no side effects, and grace does not give it any.

        Threshold events and durable notifications belong to EVAL-EVENT and
        ALERT. §11 is explicit that grace must not become "a magic number in
        the threshold comparison", so the day evaluation neither consults a
        threshold nor emits anything when grace bites.
        """
        from app.alert.models import EcoNotification
        from app.eval_event.models import EcoThresholdEvent

        for model in (EcoThresholdEvent, EcoRecipientDesignation,
                      EcoNotification):
            model.__table__.create(db.get_bind(), checkfirst=True)

        record = _run(db, gap=GV24_GROSS, grace=GV24_GRACE)
        assert record.grace_minutes_applied == GV24_GRACE

        assert db.query(EcoThresholdEvent).count() == 0
        assert db.query(EcoNotification).count() == 0

    def test_the_evaluator_reads_no_threshold(self):
        """A static guard on the locus §11 warns about.

        ``evaluate_day`` must not import, query or compare a threshold. The
        word survives only in the comment explaining why it is absent, so the
        guard strips comments before looking.
        """
        source = (BACKEND / "app" / "eval_day" / "evaluator.py").read_text(
            encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#"))
        for token in ("eco_threshold", "threshold_minutes", "ECO_THRESHOLD_KEY"):
            assert token not in code, (
                f"evaluate_day reached for {token}; §11 exists so the "
                f"de-minimis decision never lands in a threshold comparison")
