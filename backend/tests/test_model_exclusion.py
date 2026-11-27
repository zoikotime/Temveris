"""
MODEL-EXCLUSION — leave attribution.
====================================

    exclusion  = min(gross_shortfall, leave_overlap)
    attributable = max(gross_shortfall - exclusion, 0)

Leave reduces an attributable shortfall and never creates credit.

Three things are tested:

1. **The ruled arithmetic**, against GV-13/14/15 — whose expected values are
   read from the locked artifact, never restated here — plus the invariants that
   must hold for every input, not only the three scenarios.

2. **The authority.** Only approved leave counts, only current-version
   intervals, only this Worker in this organization. "No leave exists" is a
   truthful zero; "leave exists but its grant cannot be established" fails
   closed, and the two are never conflated.

3. **That nothing widened.** The classifier is still absent, so a day
   containing any non-WRK interval must still refuse — leave storage existing
   does not make BRK_M, BRK_R, TRV, ONC, OFF, LVE or UNK evaluable.

Literals are transcribed rather than imported where a wrong production constant
could otherwise agree with itself.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.config_versioning import ConfigValue, ConfigVersion, compute_content_hash
from app.eval_day import (
    EcoDayRecord,
    EcoDayState,
    REASON_LEAVE_AUTHORITY_UNRESOLVED,
    REASON_NON_WRK_STATE_PRESENT,
    RecordingOutcome,
    approved_leave_overlap,
    evaluate_and_record_day,
)
from app.eval_week import WeeklyState, evaluate_workweek
from app.leave_intervals import LeaveInterval, LeaveIntervalSet
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = timezone.utc
ORG = 91
OTHER_ORG = 92
USER = 9100
OTHER_USER = 9200

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
EVAL_DAY = BACKEND / "app" / "eval_day"
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

# ── Independently transcribed ────────────────────────────────────────────────
MONDAY_WEEK = "2026-08-31"
REQUIRED_MINUTES = 480
APPROVED = "approved"


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import LeaveRequest, Organization, User

    for table in (Organization, User, LeaveRequest):
        table.__table__.create(engine, checkfirst=True)
    from app.eval_day import LeaveInputContribution, LeaveInputSnapshot
    from app.policy_version import PolicyVersion
    from app.break_treatment import BreakTreatmentProfile
    from app.break_classification import (
        BreakClassificationAssertion, ClassificationSnapshot,
        EcoClassificationAuthority,
    )

    for model in (WorkDayLedger, WorkStateInterval, ConfigVersion, ConfigValue,
                  PolicyVersion, BreakTreatmentProfile,
                  EcoClassificationAuthority,
                  BreakClassificationAssertion,
                  ClassificationSnapshot,
                  EcoDayRecord, LeaveIntervalSet, LeaveInterval,
                  LeaveInputSnapshot, LeaveInputContribution):
        model.__table__.create(engine, checkfirst=True)

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


def _vector(vid):
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == vid:
            return v
    raise AssertionError(vid)


def _week_dates(key=MONDAY_WEEK):
    start = date.fromisoformat(key)
    return [start + timedelta(days=i) for i in range(7)]


def _config(db, *, effective_from, minutes=REQUIRED_MINUTES, organization_id=ORG):
    values = [{
        "scope_user_id": None, "tier": "TENANT_FALLBACK", "rank": 6,
        "key": "required_minutes", "value_minutes": minutes, "source_ref": None,
    }]
    key = effective_from.isoformat()
    version = ConfigVersion(
        organization_id=organization_id, version_key=key,
        effective_from=effective_from,
        captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        captured_by_label="SYSTEM",
        content_hash=compute_content_hash(
            organization_id=organization_id, version_key=key,
            effective_from=effective_from, values=values),
    )
    db.add(version)
    db.flush()
    for value in values:
        db.add(ConfigValue(config_version_id=version.id,
                           organization_id=organization_id, **value))
    db.flush()
    return version


def _ledger(db, *, on, state=LedgerState.SEALED.value, organization_id=ORG,
            user_id=USER, version=1):
    led = WorkDayLedger(
        organization_id=organization_id, user_id=user_id,
        work_date=datetime(on.year, on.month, on.day),
        workweek_key=MONDAY_WEEK, ledger_version=version, state=state,
        # D4-A: NOT_A_WORKING_DAY implies is_working_day=False. Derived
        # from the state so a fixture cannot assert both that no work was
        # expected and that the day was a working day.
        timezone_iana="Asia/Kolkata", is_working_day=(state != LedgerState.NOT_A_WORKING_DAY.value), working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST",
    )
    db.add(led)
    db.flush()
    return led


def _interval(db, led, *, seconds, state="WRK", seq=1):
    begin = datetime(led.work_date.year, led.work_date.month,
                     led.work_date.day, 9, 0, tzinfo=UTC)
    row = WorkStateInterval(
        ledger_id=led.id, organization_id=led.organization_id, seq=seq,
        state=state, start_at_utc=begin,
        end_at_utc=begin + timedelta(seconds=seconds),
        duration_seconds=seconds, provenance="WORKER", created_at=NOW,
    )
    db.add(row)
    db.flush()
    return row


def _worked_day(db, *, on, gap_minutes, organization_id=ORG, user_id=USER):
    """A sealed WRK-only day whose gross shortfall is exactly ``gap_minutes``."""
    led = _ledger(db, on=on, organization_id=organization_id, user_id=user_id)
    _interval(db, led, seconds=(REQUIRED_MINUTES - gap_minutes) * 60)
    return led


_request_seq = [7000]


def _leave(db, *, on, minutes, status=APPROVED, organization_id=ORG,
           user_id=USER, offset_minutes=0, count=1, seconds=None):
    """An approved leave grant with intraday intervals, built canonically.

    Intervals are written through the models with the same START_DATE
    ``work_date`` attribution the ledger uses — the rule already in force, not
    a new one.
    """
    from database.models import LeaveRequest

    _request_seq[0] += 1
    request = LeaveRequest(
        id=_request_seq[0], user_id=user_id, organization_id=organization_id,
        leave_type="Casual Leave", from_date=datetime(on.year, on.month, on.day),
        to_date=datetime(on.year, on.month, on.day), description="x",
        status=status,
    )
    db.add(request)
    db.flush()

    interval_set = LeaveIntervalSet(
        organization_id=organization_id, user_id=user_id,
        leave_request_id=request.id, set_version=1, supersedes=None,
        timezone_iana="Asia/Kolkata", recorded_by="TEST", recorded_at_utc=NOW,
    )
    db.add(interval_set)
    db.flush()

    each = seconds if seconds is not None else minutes * 60
    for i in range(count):
        begin = datetime(on.year, on.month, on.day, 18, 0, tzinfo=UTC) + \
            timedelta(minutes=offset_minutes + i * (each // 60 + 5))
        db.add(LeaveInterval(
            set_id=interval_set.id, organization_id=organization_id,
            seq=i + 1, work_date=on, start_at_utc=begin,
            end_at_utc=begin + timedelta(seconds=each),
            duration_seconds=each,
        ))
    db.flush()
    return request, interval_set


def _withdraw(db, *, interval_set):
    """Withdrawal: a NEW set version holding no intervals. Nothing mutated."""
    successor = LeaveIntervalSet(
        organization_id=interval_set.organization_id,
        user_id=interval_set.user_id,
        leave_request_id=interval_set.leave_request_id,
        set_version=interval_set.set_version + 1,
        supersedes=interval_set.id,
        timezone_iana=interval_set.timezone_iana,
        recorded_by="TEST", recorded_at_utc=NOW,
    )
    db.add(successor)
    db.flush()
    return successor


def _record(db, *, on, organization_id=ORG, user_id=USER):
    return evaluate_and_record_day(
        db, organization_id=organization_id, user_id=user_id, work_date=on)


def _overlap(db, *, on, organization_id=ORG, user_id=USER):
    return approved_leave_overlap(
        db, organization_id=organization_id, user_id=user_id, work_date=on)


def _code_tokens(tree: ast.Module) -> set:
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
# GV-13/14/15 — executed against the locked artifact
# ═════════════════════════════════════════════════════════════════════════════
class TestGoldenVectors:
    def _scenario(self, db, *, gap, overlap):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=gap)
        if overlap:
            _leave(db, on=on, minutes=overlap)
        return _record(db, on=on)

    def test_gv13_partial_leave(self, db):
        """gap 30, leave 15 -> excluded 15, attributable 15."""
        expected = _vector("GV-13")["expected_fields"]
        result = self._scenario(
            db, gap=expected["gap_minutes"],
            overlap=expected["leave_overlap_minutes"])

        assert result.is_recorded
        assert result.record.gross_shortfall_minutes == expected["gap_minutes"]
        assert result.record.exclusion_minutes == expected["exclusion_minutes"]
        assert result.record.attributable_minutes == (
            expected["attributable_minutes"])

    def test_gv14_leave_exceeds_gap(self, db):
        """gap 30, leave 45 -> excluded 30, attributable 0, never negative.

        The vector carries ``never_negative: true`` as an expectation in its own
        right, so it is asserted rather than assumed from the number.
        """
        expected = _vector("GV-14")["expected_fields"]
        result = self._scenario(
            db, gap=expected["gap_minutes"],
            overlap=expected["leave_overlap_minutes"])

        assert result.record.exclusion_minutes == expected["exclusion_minutes"]
        assert result.record.attributable_minutes == (
            expected["attributable_minutes"])
        assert expected["never_negative"] is True
        assert result.record.attributable_minutes >= 0

    def test_gv15_leave_with_no_gap(self, db):
        """gap 0, leave 60 -> attributable 0, and no credit created.

        This vector deliberately states no ``exclusion_minutes``: with no gap
        the expectation is that leave produces NOTHING, which it asserts as
        ``credit_created: false``. So the behaviour checked here is that
        nothing anywhere went negative or accumulated.
        """
        expected = _vector("GV-15")["expected_fields"]
        result = self._scenario(
            db, gap=expected["gap_minutes"],
            overlap=expected["leave_overlap_minutes"])

        assert result.record.gross_shortfall_minutes == 0
        assert result.record.attributable_minutes == (
            expected["attributable_minutes"])
        assert expected["credit_created"] is False
        # No credit, anywhere: not in exclusion, not in the weekly contribution.
        assert result.record.exclusion_minutes == 0
        assert result.record.contributes_to_accumulator_minutes == 0
        assert result.record.attributable_minutes >= 0

    def test_the_fixtures_are_read_not_restated(self):
        """Transcribed field-by-field, including which fields each one carries.

        GV-15 states no ``exclusion_minutes`` — asserting one would be testing
        an expectation the artifact does not make.
        """
        gv13 = _vector("GV-13")["expected_fields"]
        assert gv13 == {"gap_minutes": 30, "leave_overlap_minutes": 15,
                        "exclusion_minutes": 15, "attributable_minutes": 15}

        gv14 = _vector("GV-14")["expected_fields"]
        assert gv14 == {"gap_minutes": 30, "leave_overlap_minutes": 45,
                        "exclusion_minutes": 30, "attributable_minutes": 0,
                        "never_negative": True}

        gv15 = _vector("GV-15")["expected_fields"]
        assert gv15 == {"gap_minutes": 0, "leave_overlap_minutes": 60,
                        "attributable_minutes": 0, "credit_created": False}

        # The harness now declares them EXECUTABLE — the owner-ruled
        # re-baseline, after tests/test_eco_golden_vectors.py proved the
        # arithmetic above runs against the real evaluator. The normative
        # expected_fields asserted above are unchanged.
        for vid in ("GV-13", "GV-14", "GV-15"):
            assert _vector(vid)["harness"]["execution"] == "executable"
            assert _vector(vid)["harness"]["blocking_dependencies"] == []


# ═════════════════════════════════════════════════════════════════════════════
# The ruled arithmetic, beyond the three scenarios
# ═════════════════════════════════════════════════════════════════════════════
class TestExclusionArithmetic:
    def _run(self, db, *, gap, overlap):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=gap)
        if overlap:
            _leave(db, on=on, minutes=overlap)
        result = _record(db, on=on)
        assert result.is_recorded
        return result.record

    def test_no_leave_excludes_nothing(self, db):
        record = self._run(db, gap=30, overlap=0)
        assert record.exclusion_minutes == 0
        assert record.attributable_minutes == 30

    @pytest.mark.parametrize("gap,overlap,excluded,attributable", [
        (30, 0, 0, 30),
        (30, 1, 1, 29),
        (30, 15, 15, 15),
        (30, 30, 30, 0),          # exact full coverage
        (30, 31, 30, 0),
        (30, 600, 30, 0),         # far exceeding the gap
        (0, 0, 0, 0),
        (0, 60, 0, 0),            # zero gap
        (480, 480, 480, 0),
        (1, 1, 1, 0),
    ])
    def test_the_formula_holds_across_the_range(
        self, db, gap, overlap, excluded, attributable
    ):
        record = self._run(db, gap=gap, overlap=overlap)
        assert record.exclusion_minutes == excluded
        assert record.attributable_minutes == attributable

    @pytest.mark.parametrize("gap,overlap", [
        (30, 0), (30, 10), (30, 30), (30, 90), (0, 0), (0, 45),
        (120, 60), (7, 3), (1, 1000),
    ])
    def test_the_invariants_hold(self, db, gap, overlap):
        """Ruling 7's properties, for every input rather than three.

        The reconciliation identity is stated in its full form, which §11 grace
        completes rather than weakens. ``attributable == gross - exclusion``
        holds outright unless a grace zeroed the remainder, and a row is only
        allowed to depart from it while ``grace_minutes_applied`` accounts for
        the whole difference. This suite's policy carries ``grace_minutes=0``
        (see ``tests/_eco_policy.py``), so every case below takes the first
        branch — the second is asserted here so that a future change making
        grace bite in this suite fails loudly instead of quietly relaxing the
        identity. ``test_gv24_execution.py`` exercises the second branch.
        """
        record = self._run(db, gap=gap, overlap=overlap)
        gross = record.gross_shortfall_minutes
        exclusion = record.exclusion_minutes
        attributable = record.attributable_minutes
        grace = record.grace_minutes_applied

        assert 0 <= exclusion <= gross
        assert 0 <= attributable
        remainder = gross - exclusion
        if attributable != remainder:
            assert grace is not None and attributable == 0                 and remainder <= grace, (
                    f"attributable {attributable} departs from gross {gross} - "
                    f"exclusion {exclusion} with grace {grace!r} failing to "
                    f"explain it")
        else:
            assert grace == 0, (
                "this suite's policy applies no grace; a non-zero grace here "
                "means the fixture changed under the test")
        if gross == 0:
            assert exclusion == 0 and attributable == 0

    def test_leave_beyond_the_gap_never_creates_credit(self, db):
        record = self._run(db, gap=10, overlap=1000)
        assert record.attributable_minutes == 0
        assert record.exclusion_minutes == 10
        assert record.contributes_to_accumulator_minutes == 0

    def test_multiple_leave_intervals_sum(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=60)
        _leave(db, on=on, minutes=10, count=3)      # 3 x 10 = 30

        record = _record(db, on=on).record
        assert record.exclusion_minutes == 30
        assert record.attributable_minutes == 30

    def test_touching_but_non_overlapping_intervals_both_count(self, db):
        """Adjacent leave is two grants of leave, not a double count of one."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=60)
        _leave(db, on=on, minutes=20, offset_minutes=0)
        _leave(db, on=on, minutes=20, offset_minutes=20)

        assert _record(db, on=on).record.exclusion_minutes == 40

    def test_leave_on_another_date_does_not_count(self, db):
        """Attribution is by the interval's own work_date — the same
        START_DATE rule the ledger uses. No window is computed."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on + timedelta(days=1), minutes=30)

        record = _record(db, on=on).record
        assert record.exclusion_minutes == 0
        assert record.attributable_minutes == 30

    def test_a_cross_midnight_leave_interval_belongs_to_its_start_date(self, db):
        """The leave model attributes an interval crossing local midnight to the
        date it began on, "so leave and work never disagree about which day is
        under discussion". It is not clipped, and the cap keeps it truthful."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        # A 10-hour interval starting 22:00, attributed to ``on``.
        _leave(db, on=on, minutes=600, offset_minutes=240)

        record = _record(db, on=on).record
        assert record.exclusion_minutes == 30      # capped at the gap
        assert record.attributable_minutes == 0

    def test_sub_minute_leave_is_rounded_once_by_the_single_site(self, db):
        """Leave overlap arrives as SECONDS and is converted by the same
        function that rounds qualifying minutes — no second rounding site."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on, minutes=0, seconds=90)   # 1.5 min -> 2 (half-up)

        assert _record(db, on=on).record.exclusion_minutes == 2

    def test_only_one_rounding_implementation_exists(self):
        """Neither new module implements a conversion of its own."""
        for name in ("exclusion.py", "recording.py"):
            tokens = _code_tokens(
                ast.parse((EVAL_DAY / name).read_text(encoding="utf-8")))
            for banned in ("round_half_up_minutes", "round", "floor", "ceil"):
                assert banned not in tokens, f"{name} rounds"


# ═════════════════════════════════════════════════════════════════════════════
# Leave authority
# ═════════════════════════════════════════════════════════════════════════════
class TestLeaveAuthority:
    def test_no_leave_is_a_truthful_zero(self, db):
        on = _week_dates()[0]
        overlap = _overlap(db, on=on)
        assert overlap.is_resolved
        assert overlap.overlap_seconds == 0
        assert overlap.interval_count == 0

    def test_approved_leave_counts(self, db):
        on = _week_dates()[0]
        _leave(db, on=on, minutes=25, status=APPROVED)
        overlap = _overlap(db, on=on)
        assert overlap.is_resolved
        assert overlap.overlap_seconds == 25 * 60
        assert overlap.interval_count == 1
        assert len(overlap.leave_request_ids) == 1

    @pytest.mark.parametrize("status", [
        "draft", "submitted", "under_review", "rejected",
    ])
    def test_recognised_non_approved_leave_contributes_nothing(self, db, status):
        """A determination, not a fallback: the leave was never granted."""
        on = _week_dates()[0]
        _leave(db, on=on, minutes=30, status=status)
        overlap = _overlap(db, on=on)
        assert overlap.is_resolved
        assert overlap.overlap_seconds == 0
        assert overlap.interval_count == 0

    def test_unapproved_leave_does_not_reduce_the_shortfall(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on, minutes=30, status="submitted")

        record = _record(db, on=on).record
        assert record.exclusion_minutes == 0
        assert record.attributable_minutes == 30

    def test_withdrawn_leave_contributes_nothing(self, db):
        """Withdrawal is a new set version holding no intervals; the superseded
        version must not be read."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _, interval_set = _leave(db, on=on, minutes=30)
        assert _overlap(db, on=on).overlap_seconds == 30 * 60

        _withdraw(db, interval_set=interval_set)

        assert _overlap(db, on=on).overlap_seconds == 0
        record = _record(db, on=on).record
        assert record.exclusion_minutes == 0
        assert record.attributable_minutes == 30

    def test_locked_for_payroll_counts_as_granted_leave(self, db):
        """OWNER RULING 1.

        ``locked_for_payroll`` is reachable only from ``approved`` and is
        terminal, and payroll and reports already treat the pair as one. So it
        is approved leave that was additionally locked — and it must NOT
        refuse, as it once did.
        """
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on, minutes=30, status="locked_for_payroll")

        overlap = _overlap(db, on=on)
        assert overlap.is_resolved
        assert overlap.reason != REASON_LEAVE_AUTHORITY_UNRESOLVED
        assert overlap.overlap_seconds == 30 * 60
        assert overlap.interval_count == 1

        result = _record(db, on=on)
        assert result.is_recorded
        assert result.reason is None
        assert result.record.exclusion_minutes == 30
        assert result.record.attributable_minutes == 0

    @pytest.mark.parametrize("minutes,excluded,attributable", [
        (0, 0, 30), (15, 15, 15), (30, 30, 0), (45, 30, 0),
    ])
    def test_locked_for_payroll_arithmetic_matches_approved(
        self, db, minutes, excluded, attributable
    ):
        """Identical to the equivalent ``approved`` case, value for value —
        the ruling changes classification, never arithmetic."""
        results = {}
        for status in ("approved", "locked_for_payroll"):
            on = _week_dates()[0 if status == "approved" else 1]
            _config(db, effective_from=on - timedelta(days=60))
            _worked_day(db, on=on, gap_minutes=30)
            if minutes:
                _leave(db, on=on, minutes=minutes, status=status)
            record = _record(db, on=on).record
            results[status] = (
                record.gross_shortfall_minutes,
                record.exclusion_minutes,
                record.attributable_minutes,
                record.contributes_to_accumulator_minutes,
            )

        assert results["locked_for_payroll"] == results["approved"]
        assert results["approved"] == (30, excluded, attributable, attributable)

    def test_an_unrecognised_status_still_fails_closed(self, db):
        """The fail-closed path is intact for a status nobody has ruled — it
        is simply no longer reachable via ``locked_for_payroll``."""
        on = _week_dates()[0]
        _leave(db, on=on, minutes=30, status="something_new")
        overlap = _overlap(db, on=on)
        assert not overlap.is_resolved
        assert overlap.reason == REASON_LEAVE_AUTHORITY_UNRESOLVED
        assert overlap.overlap_seconds is None

    def test_an_unresolved_authority_is_never_treated_as_zero(self, db):
        """The distinction ruling 11 draws, stated as a test.

        The unresolved example is now an unrecognised status, since every
        member of the platform's own vocabulary is ruled.
        """
        on = _week_dates()[0]
        assert _overlap(db, on=on).overlap_seconds == 0        # no leave
        _leave(db, on=on, minutes=30, status="not_a_real_status")
        assert _overlap(db, on=on).overlap_seconds is None     # unresolved

    def test_no_platform_leave_status_is_left_unruled(self):
        """Every member of ``LeaveStatus`` is now classified one way or the
        other, so no ordinary leave day can refuse on authority grounds."""
        from database.models import LeaveStatus

        from app.eval_day import (
            APPROVED_LEAVE_STATUSES,
            NON_APPROVED_LEAVE_STATUSES,
            UNRULED_LEAVE_STATUSES,
        )

        vocabulary = {s.value for s in LeaveStatus}
        assert vocabulary == (
            set(APPROVED_LEAVE_STATUSES) | set(NON_APPROVED_LEAVE_STATUSES))
        assert set(UNRULED_LEAVE_STATUSES) == set()
        # And the two sets never overlap.
        assert not (set(APPROVED_LEAVE_STATUSES)
                    & set(NON_APPROVED_LEAVE_STATUSES))

    def test_organization_isolation(self, db):
        on = _week_dates()[0]
        _leave(db, on=on, minutes=30, organization_id=ORG, user_id=USER)
        assert _overlap(db, on=on, organization_id=ORG).overlap_seconds == 1800
        assert _overlap(
            db, on=on, organization_id=OTHER_ORG,
            user_id=OTHER_USER).overlap_seconds == 0

    def test_worker_isolation(self, db):
        on = _week_dates()[0]
        _leave(db, on=on, minutes=30, user_id=USER)
        assert _overlap(db, on=on, user_id=USER).overlap_seconds == 1800
        assert _overlap(db, on=on, user_id=OTHER_USER).overlap_seconds == 0

    def test_one_workers_leave_never_reduces_anothers_shortfall(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30, user_id=OTHER_USER)
        _leave(db, on=on, minutes=30, user_id=USER)

        record = _record(db, on=on, user_id=OTHER_USER).record
        assert record.exclusion_minutes == 0
        assert record.attributable_minutes == 30

    def test_no_second_leave_authority_is_created(self):
        """Approval is read from the canonical grant; nothing else is
        consulted, derived or inferred."""
        tokens = _code_tokens(
            ast.parse((EVAL_DAY / "exclusion.py").read_text(encoding="utf-8")))
        assert "LeaveRequest" in tokens          # the canonical authority
        assert "intervals_for_day" in tokens     # the canonical accessor
        for banned in ("is_half_day", "half_day_period", "from_date", "to_date",
                       "leave_days", "resolve_work_schedule", "screenshot",
                       "activity", "idle"):
            assert banned not in tokens, f"exclusion.py reads {banned}"

    def test_the_approved_status_set_is_exactly_the_ruled_two(self):
        """Transcribed, not imported from the production constant."""
        from app.eval_day import APPROVED_LEAVE_STATUSES

        assert set(APPROVED_LEAVE_STATUSES) == {
            "approved", "locked_for_payroll"}


# ═════════════════════════════════════════════════════════════════════════════
# Non-WRK safety — nothing widened
# ═════════════════════════════════════════════════════════════════════════════
class TestNonWrkSafetyUnchanged:
    @pytest.mark.parametrize("state", [
        "BRK_M", "BRK_R", "TRV", "ONC", "OFF", "LVE", "UNK",
    ])
    def test_a_non_wrk_interval_still_refuses(self, db, state):
        """Leave storage existing does not make any of these evaluable."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        led = _ledger(db, on=on)
        _interval(db, led, seconds=3600, state="WRK", seq=1)
        _interval(db, led, seconds=1800, state=state, seq=2)
        _leave(db, on=on, minutes=30)

        result = _record(db, on=on)
        assert result.outcome is RecordingOutcome.REFUSED
        assert result.reason == REASON_NON_WRK_STATE_PRESENT
        assert db.execute(
            sa.select(sa.func.count()).select_from(EcoDayRecord)
            .where(EcoDayRecord.work_date == on)).scalar() == 0

    def test_qualifying_states_is_still_wrk_only(self):
        from app.eval_day import QUALIFYING_STATES

        assert set(QUALIFYING_STATES) == {"WRK"}

    def test_lve_is_not_treated_as_a_ledger_state_by_the_exclusion(self):
        """The leave-interval model and WorkStateInterval remain distinct: the
        exclusion reads leave intervals, never a ledger LVE interval."""
        tokens = _code_tokens(
            ast.parse((EVAL_DAY / "exclusion.py").read_text(encoding="utf-8")))
        for banned in ("WorkStateInterval", "WorkState", "LVE",
                       "WorkDayLedger"):
            assert banned not in tokens, f"exclusion.py uses {banned}"

    def test_leave_is_never_added_to_qualifying_minutes(self, db):
        """Leave is not working time. It reduces attributable shortfall only."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on, minutes=30)

        record = _record(db, on=on).record
        assert record.qualifying_minutes == REQUIRED_MINUTES - 30
        assert record.gross_shortfall_minutes == 30      # unchanged by leave
        assert record.required_minutes == REQUIRED_MINUTES

    def test_no_legal_determination_is_made(self):
        """No classifier, no jurisdiction, no paid/unpaid, no legal matrix."""
        for name in ("exclusion.py", "recording.py", "evaluator.py"):
            tokens = _code_tokens(
                ast.parse((EVAL_DAY / name).read_text(encoding="utf-8")))
            for banned in ("jurisdiction", "legal_matrix", "LegalMatrix",
                           "paid", "unpaid", "statutory", "compensable",
                           "employment_type", "worker_context",
                           "BRK_M", "BRK_R"):
                assert banned not in tokens, f"{name} decides {banned}"

    def test_the_day_state_rule_is_unchanged(self, db):
        """A fully-excluded day is still SHORTFALL: there WAS a shortfall, and
        it was excluded. The state rule was not touched."""
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on, minutes=60)

        record = _record(db, on=on).record
        assert record.state == EcoDayState.SHORTFALL.value
        assert record.attributable_minutes == 0


# ═════════════════════════════════════════════════════════════════════════════
# Idempotency, recording, and the snapshot-identity finding
# ═════════════════════════════════════════════════════════════════════════════
class TestIdempotencyAndRecording:
    def test_identical_evaluation_remains_idempotent(self, db):
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on, minutes=15)

        first = _record(db, on=on)
        second = _record(db, on=on)
        assert first.record.id == second.record.id
        assert second.record.exclusion_minutes == 15

    def test_a_different_config_snapshot_is_a_separate_record(self, db):
        week = _week_dates()
        on = week[3]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        _leave(db, on=on, minutes=15)
        first = _record(db, on=on)

        _config(db, effective_from=week[0], minutes=600)
        second = _record(db, on=on)

        assert second.record.id != first.record.id
        assert first.record.exclusion_minutes == 15

    def test_leave_recorded_after_the_day_supersedes_rather_than_conflicts(
        self, db
    ):
        """THE REPORTED DEFECT, now closed by Owner Ruling 2.

        Leave was an authoritative input that the snapshot identity did not
        name, so a day evaluated before its leave was captured and
        re-evaluated after had the SAME identity and a DIFFERENT result — a
        ``RESULT_CONFLICT``, which meant the corrected answer could never be
        recorded at all.

        The leave input snapshot key is now the seventh identity component, so
        the second evaluation is a genuinely different snapshot: a NEW record,
        the predecessor superseded and retained, and no conflict.
        """
        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)

        before = _record(db, on=on)
        assert before.record.exclusion_minutes == 0
        first_id = before.record.id

        _leave(db, on=on, minutes=15)
        after = _record(db, on=on)

        # A new record, not a refusal.
        assert after.is_recorded
        assert after.reason is None
        assert after.record.id != first_id
        assert after.record.exclusion_minutes == 15
        assert after.record.attributable_minutes == 15

        # Different leave input -> different identity component.
        assert (after.record.leave_input_snapshot_key
                != before.record.leave_input_snapshot_key)

        # Both retained; exactly one authoritative.
        rows = db.execute(
            sa.select(EcoDayRecord)
            .where(EcoDayRecord.work_date == on)
            .order_by(EcoDayRecord.evaluation_generation)
        ).scalars().all()
        assert [r.exclusion_minutes for r in rows] == [0, 15]
        assert [r.evaluation_generation for r in rows] == [1, 2]
        assert rows[0].superseded_by == rows[1].id
        assert rows[1].superseded_by is None
        assert rows[1].supersedes == rows[0].id

    def test_the_superseded_record_remains_reproducible(self, db):
        """Ruling M: the historical record and its frozen input both survive.

        The earlier evaluation can still be explained — its own leave snapshot
        says what it used — without re-reading current leave state.
        """
        from app.eval_day import read_contributions, read_leave_snapshot

        on = _week_dates()[0]
        _config(db, effective_from=on - timedelta(days=30))
        _worked_day(db, on=on, gap_minutes=30)
        before = _record(db, on=on)
        first_key = before.record.leave_input_snapshot_key

        _leave(db, on=on, minutes=15)
        _record(db, on=on)

        historical = read_leave_snapshot(
            db, organization_id=ORG, snapshot_key=first_key)
        assert historical is not None
        assert historical.overlap_seconds == 0
        assert read_contributions(db, snapshot=historical) == ()


# ═════════════════════════════════════════════════════════════════════════════
# EVAL-WEEK regression — end to end, no threshold, no event
# ═════════════════════════════════════════════════════════════════════════════
class TestEvalWeekEndToEnd:
    def test_daily_attributable_flows_into_weekly_w(self, db):
        week = _week_dates()
        _config(db, effective_from=week[0] - timedelta(days=30))

        # Five working days 10 minutes short; leave covers 4 minutes on two.
        for i, on in enumerate(week[:5]):
            _worked_day(db, on=on, gap_minutes=10)
            if i < 2:
                _leave(db, on=on, minutes=4)
            _record(db, on=on)
        for on in week[5:]:
            _ledger(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
            _record(db, on=on)

        result = evaluate_workweek(
            db, organization_id=ORG, user_id=USER, workweek_key=MONDAY_WEEK)

        assert result.state is WeeklyState.COMPLETE
        # 2 days at 6 + 3 days at 10 = 42
        assert result.weekly_attributable_shortfall_minutes == 42

    def test_full_leave_coverage_removes_a_day_from_w(self, db):
        week = _week_dates()
        _config(db, effective_from=week[0] - timedelta(days=30))
        for on in week[:5]:
            _worked_day(db, on=on, gap_minutes=10)
            _leave(db, on=on, minutes=10)
            _record(db, on=on)
        for on in week[5:]:
            _ledger(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value)
            _record(db, on=on)

        result = evaluate_workweek(
            db, organization_id=ORG, user_id=USER, workweek_key=MONDAY_WEEK)
        assert result.state is WeeklyState.COMPLETE
        assert result.weekly_attributable_shortfall_minutes == 0

    def test_eval_week_was_not_modified(self):
        """No threshold, no event, no notification, and still no configuration
        resolution."""
        eval_week = BACKEND / "app" / "eval_week"
        for path in sorted(eval_week.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("resolve_threshold", "version_as_of",
                           "app.config_versioning", "app.eco_threshold",
                           "approved_leave_overlap", "exclusion_minutes"):
                assert banned not in tokens, f"{path.name} uses {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# Scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_no_migration_was_added(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # 71 since §45.D-15 added c4e9b7d2a583
        # (clock_session_boundary_event). This test guards the MODEL-EXCLUSION
        # increment's scope — that it added no migration of its own — so the
        # pin moves when a later authorised increment adds one. The
        # eco_day_record assertions below are what hold that scope.
        # 91 since §24.15's D-9.1 added d9a4c7e35f18 (eco_ledger_incident).
        assert len(list((BACKEND / "alembic" / "versions").glob("*.py"))) == 94
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]

    def test_no_schema_change_to_the_day_record(self):
        columns = set(EcoDayRecord.__table__.c.keys())
        assert "exclusion_minutes" in columns
        assert "attributable_minutes" in columns
        for banned in ("leave_set_version", "leave_request_id",
                       "leave_overlap_minutes"):
            assert banned not in columns, f"EcoDayRecord gained {banned}"

    def test_no_scheduler_or_route_was_added(self):
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("APIRouter", "BaseModel", "add_job", "apscheduler",
                           "celery"):
                assert banned not in tokens, f"{path.name} uses {banned}"

    def test_the_golden_vectors_are_untouched(self):
        data = json.loads(VECTORS.read_text(encoding="utf-8"))
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

    def test_eco_v1_and_rollout_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("app.rollout", "RolloutControl"):
                assert banned not in tokens, f"{path.name} uses {banned}"

    def test_no_production_database_is_reachable(self):
        for path in sorted(EVAL_DAY.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("create_engine", "DATABASE_URL", "postgresql://",
                           "sessionmaker", "load_dotenv"):
                assert banned not in body, f"{path.name} mentions {banned}"

    def test_the_terminology_guard_is_satisfied(self):
        from app.early_clock_out import find_prohibited_terms, format_hits, scan_text

        assert find_prohibited_terms(REASON_LEAVE_AUTHORITY_UNRESOLVED) == []
        path = EVAL_DAY / "exclusion.py"
        hits = scan_text(path.read_text(encoding="utf-8"))
        assert hits == [], format_hits(hits, source=path.name)


# ═════════════════════════════════════════════════════════════════════════════
# The overlap result type
# ═════════════════════════════════════════════════════════════════════════════
class TestOverlapResultType:
    def test_a_refused_overlap_cannot_carry_seconds(self):
        from app.eval_day import LeaveOverlap, LeaveOverlapOutcome

        with pytest.raises(ValueError):
            LeaveOverlap(
                outcome=LeaveOverlapOutcome.REFUSED,
                reason=REASON_LEAVE_AUTHORITY_UNRESOLVED,
                overlap_seconds=0,
            )

    def test_a_refused_overlap_must_state_a_reason(self):
        from app.eval_day import LeaveOverlap, LeaveOverlapOutcome

        with pytest.raises(ValueError):
            LeaveOverlap(outcome=LeaveOverlapOutcome.REFUSED)

    def test_a_resolved_overlap_must_carry_seconds(self):
        from app.eval_day import LeaveOverlap, LeaveOverlapOutcome

        with pytest.raises(ValueError):
            LeaveOverlap(outcome=LeaveOverlapOutcome.RESOLVED)

    def test_negative_seconds_are_refused(self):
        from app.eval_day import LeaveOverlap, LeaveOverlapOutcome

        with pytest.raises(ValueError):
            LeaveOverlap(
                outcome=LeaveOverlapOutcome.RESOLVED, overlap_seconds=-1)
