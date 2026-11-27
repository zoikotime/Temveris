"""
GV-01 — "Three measurements diverge (§6.2)", executed end to end.
=================================================================

    Req 525; WRK 270+225; BRK_M 45 unpaid; leave 17:30-18:00; sched end 18:30
    -> variance -60; qualifying 495; gross 30; excluded 30; attributable 0;
       no event

§6.2 is the reason the three-quantity model exists. Transcribed from the spec:

    1. Schedule variance   18:30 -> 17:30 = -60 min (early)
    2. Qualifying minutes  270 + 225      = 495 min
    3. Gross shortfall     525 - 495      =  30 min
    4. Exclusion           17:30-18:00    =  30 min
    5. Attributable        30 - 30        =   0 min

    "A design that alerts on schedule variance fires here. A design that alerts
    on gross shortfall fires here. Both would be wrong: this Worker completed
    the day correctly with approved leave."

So the vector passes only when all three numbers are DIFFERENT and only the
third governs. A harness that compared one of them would pass against an
implementation that had collapsed them, which is the defect §6.2 exists to
catch.

WHAT THIS DRIVES
----------------
The real pipeline: schedule resolver -> working-day classifier -> DayContext ->
``seal_day`` (freezing the scheduled end) -> ``evaluate_and_record_day`` -> a
persisted ``EcoDayRecord``. Nothing is stubbed and no arithmetic is restated —
the expected values are read from the locked artifact.

THE MEAL IS A GAP, NOT AN INTERVAL
-----------------------------------
D-18: breaks are gaps, and the producer emits only WRK and OFF. The 45-minute
unpaid meal is therefore the absence of a WRK interval between 13:00 and 13:45
local, not a BRK_M row — which is also why no break-treatment profile and no
classification authority appear anywhere in this file. GV-02/GV-03 own that
path; GV-01 does not touch it.

TIMEZONE IS LOAD-BEARING, NOT DECORATION
-----------------------------------------
Every instant below is stored UTC and read local through the ledger's frozen
``timezone_iana``. In Asia/Kolkata (+05:30) the Worker's 17:30 departure is
12:00 UTC, and the 18:30 scheduled end is a LOCAL clock time that never became
an instant. Comparing them in UTC would yield -390, not -60. One of the tests
below asserts exactly that, because an implementation that skipped the
conversion would still produce a plausible-looking negative number.

REGISTERED IN ``EXTERNAL_EXECUTION_TESTS``
-------------------------------------------
Registration is promotion-coupled: naming GV-01 in the registry and marking it
``executable`` in the artifact are one act. This class was written first and
left unregistered — the TestGV16/17/25/27 precedent — so promotion had
something already proven to promote.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import pathlib
from zoneinfo import ZoneInfo

import pytest

import database.models as dbm
from database.database import Base

from app.eval_day import EcoDayRecord, EcoDayState, evaluate_and_record_day
from app.eval_day.evaluator import schedule_variance_minutes
from app.leave_intervals import CandidateLeaveInterval, record_leave_intervals
from app.work_ledger import (
    CandidateInterval,
    DayKey,
    LedgerState,
    Provenance,
    WorkDayLedger,
    WorkState,
    seal_day,
)
from app.work_ledger.states import ProvenanceImmutable

UTC = dt.timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, USER = 9610, 9611
DAY = dt.date(2026, 8, 31)          # a Monday
ZONE_NAME = "Asia/Kolkata"          # +05:30, no DST
ZONE = ZoneInfo(ZONE_NAME)
FLOOR = dt.date(2020, 1, 1)

# ── Independently transcribed from §6.2, never imported ─────────────────────
REQUIRED_MINUTES = 525
WRK_A_MINUTES = 270
WRK_B_MINUTES = 225
MEAL_GAP_MINUTES = 45
LEAVE_MINUTES = 30

#: Local clock times. The day runs 08:30 -> 17:30 with a 45-minute meal gap,
#: which is 270 + 45 + 225 = 540 minutes wall-clock and 495 minutes worked.
START_LOCAL = dt.time(8, 30)
DEPARTURE_LOCAL = dt.time(17, 30)
SCHEDULED_END_LOCAL = dt.time(18, 30)
LEAVE_START_LOCAL = dt.time(17, 30)


def _utc(hour: int, minute: int) -> dt.datetime:
    """A local wall-clock time on DAY, as the UTC instant it really is."""
    return dt.datetime(
        DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=ZONE
    ).astimezone(UTC)


DAY_START_UTC = _utc(START_LOCAL.hour, START_LOCAL.minute)
DEPARTURE_UTC = _utc(DEPARTURE_LOCAL.hour, DEPARTURE_LOCAL.minute)
LEAVE_START_UTC = _utc(LEAVE_START_LOCAL.hour, LEAVE_START_LOCAL.minute)


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    import app.alert.models                    # noqa: F401
    import app.break_classification.models     # noqa: F401
    import app.break_treatment.models          # noqa: F401
    import app.eval_event.models               # noqa: F401
    import app.policy_version.models           # noqa: F401

    Base.metadata.create_all(engine)


@pytest.fixture()
def org(db):
    from app.schedule_policy import keys as SCHEDULE_KEYS

    if db.get(dbm.Organization, ORG) is None:
        db.add(dbm.Organization(id=ORG, name="GV01 Org"))
        db.flush()
        db.add(dbm.User(id=USER, name="W", email="w@gv01.test",
                        organization_id=ORG, role="employee"))
        db.flush()

    # §12.1: no day may be sealed under an assumed workweek boundary, so the
    # tenant must state one exactly as a real tenant would (MODEL-WORKWEEK).
    row = (db.query(dbm.AppSettings)
             .filter(dbm.AppSettings.organization_id == ORG,
                     dbm.AppSettings.key
                     == SCHEDULE_KEYS.SETTING_WEEK_START_DAY,
                     dbm.AppSettings.team_key.is_(None),
                     dbm.AppSettings.user_id.is_(None))
             .one_or_none())
    if row is None:
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=None,
            key=SCHEDULE_KEYS.SETTING_WEEK_START_DAY, value="MONDAY"))
    else:
        row.value = "MONDAY"
    db.flush()

    from tests._eco_policy import ensure_test_policy
    ensure_test_policy(db, organization_id=ORG, commit=False)
    db.flush()
    return ORG


def _vector(vid: str = "GV-01") -> dict:
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == vid:
            return v
    raise AssertionError(vid)


def _capture_requirement(db):
    """§6.2's 525 minutes, through Step 6's own versioned-configuration writer."""
    from app.config_versioning import DatabaseConfigVersionProvider
    from app.requirement_resolution import RequirementCandidate

    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG,
        config_version_id="gv01-cfg-v1",
        effective_from=FLOOR,
        candidates=[RequirementCandidate(
            tier="TENANT_FALLBACK", rank=6, minutes=REQUIRED_MINUTES)],
    )
    db.flush()


def _seal(db, *, scheduled_end_local=SCHEDULED_END_LOCAL,
          scheduled_end_source="shift", departure_at=DEPARTURE_UTC):
    """§6.2's ledger: two WRK segments with the unpaid meal as the gap."""
    wrk_a_end = DAY_START_UTC + dt.timedelta(minutes=WRK_A_MINUTES)
    wrk_b_start = wrk_a_end + dt.timedelta(minutes=MEAL_GAP_MINUTES)
    wrk_b_end = wrk_b_start + dt.timedelta(minutes=WRK_B_MINUTES)
    assert wrk_b_end == DEPARTURE_UTC, "fixture drift: the day must end at 17:30"

    return seal_day(
        db,
        DayKey(organization_id=ORG, user_id=USER, work_date=DAY),
        intervals=[
            CandidateInterval(
                seq=1, state=WorkState.WRK.value,
                start_at_utc=DAY_START_UTC, end_at_utc=wrk_a_end,
                provenance=Provenance.WORKER.value),
            CandidateInterval(
                seq=2, state=WorkState.WRK.value,
                start_at_utc=wrk_b_start, end_at_utc=wrk_b_end,
                provenance=Provenance.WORKER.value),
        ],
        timezone_iana=ZONE_NAME,
        is_working_day=True,
        working_day_source="org",
        scheduled_end_local=scheduled_end_local,
        scheduled_end_source=scheduled_end_source,
        departure_at=departure_at,
        departure_provenance="WORKER",
        departure_source_ref="clock_session:gv01",
        created_by="GOLDEN_VECTOR",
    )


def _record_leave(db, minutes: int = LEAVE_MINUTES):
    """17:30-18:00 local, through the real intraday-leave authority."""
    request = dbm.LeaveRequest(
        user_id=USER, organization_id=ORG, leave_type="Casual Leave",
        from_date=dt.datetime(DAY.year, DAY.month, DAY.day),
        to_date=dt.datetime(DAY.year, DAY.month, DAY.day),
        description="GV-01 fixture", status="approved",
    )
    db.add(request)
    db.flush()
    record_leave_intervals(
        db,
        leave_request=request,
        intervals=[CandidateLeaveInterval(
            seq=1,
            start_at_utc=LEAVE_START_UTC,
            end_at_utc=LEAVE_START_UTC + dt.timedelta(minutes=minutes),
        )],
        timezone_iana=ZONE_NAME, zone=ZONE,
        expected_version=None, recorded_by="GOLDEN_VECTOR",
    )
    db.flush()
    return request


# ═════════════════════════════════════════════════════════════════════════════
# 1. GV-01, executed against the artifact
# ═════════════════════════════════════════════════════════════════════════════

class TestGV01:
    """The vector, through the real production path. Values read, not restated."""

    def _run(self, db):
        _capture_requirement(db)
        _seal(db)
        _record_leave(db)
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY)
        assert result.record is not None, result
        return result.record

    def test_the_three_measurements_diverge(self, db, org):
        """Every expected field, compared against the artifact."""
        expected = _vector()["expected_fields"]
        row = self._run(db)

        assert row.schedule_variance_minutes == expected[
            "schedule_variance_minutes"]
        assert row.qualifying_minutes == expected["qualifying_minutes"]
        assert row.gross_shortfall_minutes == expected["gross_shortfall_minutes"]
        assert row.exclusion_minutes == expected["exclusion_minutes"]
        assert row.attributable_minutes == expected["attributable_minutes"]

        # "no event": nothing may have been emitted for this week.
        from app.eval_event.models import EcoThresholdEvent
        emitted = db.query(EcoThresholdEvent).filter_by(
            organization_id=ORG, user_id=USER).count()
        assert bool(emitted) == expected["threshold_event_emitted"]

    def test_the_three_quantities_are_actually_different(self, db, org):
        """§6.2's whole point. A collapse would make two of them equal."""
        row = self._run(db)
        assert row.schedule_variance_minutes == -60
        assert row.gross_shortfall_minutes == 30
        assert row.attributable_minutes == 0
        assert len({row.schedule_variance_minutes,
                    row.gross_shortfall_minutes,
                    row.attributable_minutes}) == 3

    def test_only_the_attributable_figure_reaches_the_week(self, db, org):
        """Variance never drives the threshold (§10, §35).

        The day is SHORTFALL because `gross` is 30 — the state follows the
        GROSS figure, not the attributable one. That is §6.2 working: the day
        has a real shortfall, approved leave explains all of it, and nothing
        reaches the week. A design that had collapsed the two would either
        report NO_SHORTFALL (losing the fact) or contribute 30 (charging the
        Worker for approved leave).
        """
        row = self._run(db)
        assert row.state == EcoDayState.SHORTFALL.value
        assert row.gross_shortfall_minutes == 30
        assert row.attributable_minutes == 0
        assert row.contributes_to_accumulator_minutes == 0

    def test_it_is_idempotent_on_replay(self, db, org):
        """Re-evaluating identical evidence yields one record, byte-identical."""
        first = self._run(db)
        digest, generation = first.content_hash, first.evaluation_generation

        again = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY)
        assert again.record.id == first.id
        assert again.record.content_hash == digest
        assert again.record.evaluation_generation == generation
        assert db.query(EcoDayRecord).filter_by(
            organization_id=ORG, user_id=USER).count() == 1


# ═════════════════════════════════════════════════════════════════════════════
# 2. Scheduled-end capture (D-1.1)
# ═════════════════════════════════════════════════════════════════════════════

class TestScheduledEndCapture:

    def test_the_scheduled_end_is_frozen_on_the_ledger(self, db, org):
        led = _seal(db)
        assert led.scheduled_end_local == SCHEDULED_END_LOCAL

    def test_the_source_tier_is_preserved_verbatim(self, db, org):
        """D11-A: the resolver's label, never re-coded into something else."""
        led = _seal(db, scheduled_end_source="assignment")
        assert led.scheduled_end_source == "assignment"

    def test_the_classifier_carries_both_halves_of_one_resolution(self):
        """"Resolved once": the scheduled end rides out of the SAME call.

        Asserted structurally rather than by mocking: `classify_working_day`
        contains exactly one `resolve_work_schedule` call, and the value it
        returns carries the scheduled end. If a second resolution were ever
        added the count changes and this fails.
        """
        from app.working_day_classifier import classifier as mod

        src = inspect.getsource(mod)
        assert src.count("resolve_work_schedule(") == 1, (
            "the classifier must resolve the schedule exactly once; the "
            "scheduled end and the working-day verdict come from one call")

        tree = ast.parse(src)
        fields = {
            kw.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "WorkingDayClassification"
            for kw in node.keywords
        }
        assert "scheduled_end_local" in fields
        assert "scheduled_end_source" in fields

    def test_the_producer_never_reaches_the_schedule_resolver(self):
        """It must consume the DayContext, not re-resolve. D2-A, restated."""
        for name in ("producer.py", "context.py"):
            src = (BACKEND / "app" / "work_ledger_producer" / name).read_text(
                encoding="utf-8")
            assert "resolve_work_schedule" not in src, name

    def test_a_later_schedule_change_cannot_move_a_sealed_day(self, db, org):
        """The freeze, demonstrated: the sealed value survives a roster edit.

        The "roster edit" is simulated at the only place it could reach the
        sealed row — a direct write — because the evaluation path provably
        cannot consult the resolver (the test above). The write is REFUSED.
        """
        led = _seal(db)
        db.flush()
        led.scheduled_end_local = dt.time(19, 0)
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_the_fault_path_also_freezes_the_schedule(self, db, org):
        """record_integrity_fault is a seal path and carries the evidence."""
        from app.work_ledger import record_integrity_fault

        row = record_integrity_fault(
            db, DayKey(organization_id=ORG, user_id=USER, work_date=DAY),
            reason="OVERLAPPING_INTERVALS",
            timezone_iana=ZONE_NAME, is_working_day=True,
            working_day_source="org",
            scheduled_end_local=SCHEDULED_END_LOCAL,
            scheduled_end_source="shift",
        )
        assert row.scheduled_end_local == SCHEDULED_END_LOCAL
        assert row.scheduled_end_source == "shift"


# ═════════════════════════════════════════════════════════════════════════════
# 3. Frozen-evidence immutability — all three transitions (D-1.1)
# ═════════════════════════════════════════════════════════════════════════════

class TestFrozenEvidence:
    """After a seal: NULL->value, value->NULL and value->other are all refused."""

    def test_null_to_value_is_rejected_after_seal(self, db, org):
        led = _seal(db, scheduled_end_local=None, scheduled_end_source="system")
        db.flush()
        assert led.scheduled_end_local is None
        led.scheduled_end_local = dt.time(18, 30)
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_value_to_null_is_rejected_after_seal(self, db, org):
        led = _seal(db)
        db.flush()
        led.scheduled_end_local = None
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_value_to_different_value_is_rejected_after_seal(self, db, org):
        led = _seal(db)
        db.flush()
        led.scheduled_end_local = dt.time(20, 0)
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_the_source_tier_is_frozen_too(self, db, org):
        led = _seal(db)
        db.flush()
        led.scheduled_end_source = "team"
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_an_identical_rewrite_is_not_a_change(self, db, org):
        led = _seal(db)
        db.flush()
        led.scheduled_end_local = SCHEDULED_END_LOCAL
        db.flush()                       # no-op, must not raise
        assert led.scheduled_end_local == SCHEDULED_END_LOCAL

    def test_both_columns_are_in_the_frozen_contract(self):
        from app.work_ledger.models import _FROZEN_LEDGER_EVIDENCE

        assert "scheduled_end_local" in _FROZEN_LEDGER_EVIDENCE
        assert "scheduled_end_source" in _FROZEN_LEDGER_EVIDENCE


# ═════════════════════════════════════════════════════════════════════════════
# 4. The "system" source (D-2.1)
# ═════════════════════════════════════════════════════════════════════════════

class TestSystemSource:
    """No tier configured a shift end. The day seals; the variance does not exist."""

    def test_the_day_still_seals(self, db, org):
        led = _seal(db, scheduled_end_local=None, scheduled_end_source="system")
        assert led.state == LedgerState.SEALED.value

    def test_it_stores_null_end_and_a_system_tier(self, db, org):
        led = _seal(db, scheduled_end_local=None, scheduled_end_source="system")
        assert led.scheduled_end_local is None
        assert led.scheduled_end_source == "system"

    def test_the_variance_is_null(self, db, org):
        _capture_requirement(db)
        _seal(db, scheduled_end_local=None, scheduled_end_source="system")
        _record_leave(db)
        row = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY).record
        assert row.schedule_variance_minutes is None

    def test_null_is_never_rendered_as_zero(self, db, org):
        """The distinction the whole ruling turns on.

        Zero asserts the Worker left exactly on time. A day with no configured
        shift end is a day where nobody recorded what "on time" meant.
        """
        _capture_requirement(db)
        _seal(db, scheduled_end_local=None, scheduled_end_source="system")
        row = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY).record
        assert row.schedule_variance_minutes is not 0  # noqa: F632
        assert row.schedule_variance_minutes is None

    def test_the_working_day_rule_is_not_extended_to_the_schedule(self):
        """D6-A stays scoped to the working-day determination.

        The classifier still REFUSES a `system` working-day source, and still
        does NOT refuse a `system` scheduled-end source. Both halves asserted,
        because dropping either would be a silent policy change.
        """
        from app.working_day_classifier.classifier import (
            UNRESOLVED_SCHEDULED_END_SOURCE,
            UNRESOLVED_WORKING_DAY_SOURCE,
        )

        src = (BACKEND / "app" / "working_day_classifier"
               / "classifier.py").read_text(encoding="utf-8")
        assert UNRESOLVED_WORKING_DAY_SOURCE == "system"
        assert UNRESOLVED_SCHEDULED_END_SOURCE == "system"
        # The working-day source returns an UNCLASSIFIED refusal ...
        assert "SOURCE_NOT_CONFIGURED" in src
        # ... while the scheduled end only nulls the value.
        assert "end_local = None" in src


# ═════════════════════════════════════════════════════════════════════════════
# 5. The variance calculation (§18 step 14)
# ═════════════════════════════════════════════════════════════════════════════

class _Stub:
    def __init__(self, **kw):
        self.scheduled_end_local = kw.get("scheduled_end_local")
        self.departure_at = kw.get("departure_at")
        self.timezone_iana = kw.get("timezone_iana", ZONE_NAME)


class TestVarianceCalculation:

    def test_early_departure_is_negative(self):
        assert schedule_variance_minutes(_Stub(
            scheduled_end_local=dt.time(18, 30),
            departure_at=_utc(17, 30))) == -60

    def test_an_exact_departure_is_zero(self):
        assert schedule_variance_minutes(_Stub(
            scheduled_end_local=dt.time(18, 30),
            departure_at=_utc(18, 30))) == 0

    def test_late_departure_is_positive(self):
        assert schedule_variance_minutes(_Stub(
            scheduled_end_local=dt.time(18, 30),
            departure_at=_utc(19, 15))) == 45

    def test_a_missing_scheduled_end_is_none(self):
        assert schedule_variance_minutes(_Stub(
            scheduled_end_local=None, departure_at=_utc(17, 30))) is None

    def test_a_missing_departure_is_none(self):
        assert schedule_variance_minutes(_Stub(
            scheduled_end_local=dt.time(18, 30), departure_at=None)) is None

    def test_the_frozen_timezone_is_used_for_the_conversion(self):
        """The load-bearing one.

        17:30 Asia/Kolkata is 12:00 UTC. Compared in UTC the answer would be
        12:00 - 18:30 = -390. Only a conversion through the ledger's own frozen
        zone yields -60, so this fails loudly for an implementation that
        skipped it.
        """
        row = _Stub(scheduled_end_local=dt.time(18, 30),
                    departure_at=_utc(17, 30), timezone_iana=ZONE_NAME)
        assert schedule_variance_minutes(row) == -60
        assert schedule_variance_minutes(row) != -390

        # The same instant under a different frozen zone is a different local
        # time and therefore a different variance. Proves the zone is read.
        utc_row = _Stub(scheduled_end_local=dt.time(18, 30),
                        departure_at=_utc(17, 30), timezone_iana="UTC")
        assert schedule_variance_minutes(utc_row) == -390

    def test_a_naive_departure_is_read_as_utc(self):
        """SQLite drops tzinfo on read; Postgres keeps it.

        A naive value therefore has to mean the UTC instant that was written,
        or the same evidence would yield a variance on one backend and NULL on
        the other. `work_ledger.service._utc` and `employees.router._utc_aware`
        both already resolve it this way, and this follows them rather than
        inventing a third convention.
        """
        aware = schedule_variance_minutes(_Stub(
            scheduled_end_local=dt.time(18, 30), departure_at=_utc(17, 30)))
        naive = schedule_variance_minutes(_Stub(
            scheduled_end_local=dt.time(18, 30),
            departure_at=_utc(17, 30).replace(tzinfo=None)))
        assert naive == aware == -60

    def test_an_unusable_zone_refuses(self):
        assert schedule_variance_minutes(_Stub(
            scheduled_end_local=dt.time(18, 30),
            departure_at=_utc(17, 30),
            timezone_iana="Not/AZone")) is None

    def test_there_is_exactly_one_calculation_site(self):
        """No second layer may re-derive it.

        Targets the minutes-of-day arithmetic specifically (`hour * 60 +`)
        rather than the mere co-occurrence of the two column names: the ledger
        service legitimately mentions both while doing no comparison at all,
        and a guard that flagged it would be a guard nobody could keep green.
        """
        offenders = []
        for path in sorted((BACKEND / "app").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            if "scheduled_end_local" in body and "* 60 +" in body:
                offenders.append(path.name)
        assert offenders == ["evaluator.py"], (
            f"the §18 step-14 arithmetic must live at exactly one site; "
            f"found it in {offenders}")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Historical rows, and the absence of a backfill
# ═════════════════════════════════════════════════════════════════════════════

class TestNoBackfill:

    def test_a_row_sealed_without_the_columns_stays_null(self, db, org):
        """A pre-migration row, modelled exactly: both columns unset."""
        led = _seal(db, scheduled_end_local=None, scheduled_end_source=None)
        assert led.scheduled_end_local is None
        assert led.scheduled_end_source is None

    def test_such_a_row_yields_a_null_variance_forever(self, db, org):
        _capture_requirement(db)
        _seal(db, scheduled_end_local=None, scheduled_end_source=None)
        row = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY).record
        assert row.schedule_variance_minutes is None

    def test_the_two_nulls_remain_distinguishable(self, db, org):
        """NULL source = predates the column. "system" = nobody configured one."""
        historical = _seal(db, scheduled_end_local=None,
                           scheduled_end_source=None)
        # A different Worker-day rather than a rollback: rolling back would
        # discard the tenant fixture too, and the point is that the two states
        # coexist and stay readable side by side.
        unconfigured = seal_day(
            db,
            DayKey(organization_id=ORG, user_id=USER,
                   work_date=DAY + dt.timedelta(days=1)),
            intervals=[CandidateInterval(
                seq=1, state=WorkState.WRK.value,
                start_at_utc=DAY_START_UTC + dt.timedelta(days=1),
                end_at_utc=DAY_START_UTC + dt.timedelta(days=1, minutes=60),
                provenance=Provenance.WORKER.value)],
            timezone_iana=ZONE_NAME, is_working_day=True,
            working_day_source="org",
            scheduled_end_local=None, scheduled_end_source="system",
            created_by="GOLDEN_VECTOR",
        )
        assert historical.scheduled_end_source is None
        assert unconfigured.scheduled_end_source == "system"
        assert historical.scheduled_end_local is None
        assert unconfigured.scheduled_end_local is None

    def test_the_migration_performs_no_backfill(self):
        path = (BACKEND / "alembic" / "versions"
                / "f4a7c2b91e68_ledger_scheduled_end_evidence.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        upgrade = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {
            node.func.attr
            for node in ast.walk(upgrade)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert "execute" not in called, "the upgrade runs SQL; no backfill is authorised"
        assert called & {"add_column"}, "the upgrade must add the columns"
        for banned in ("update", "bulk_update", "execute"):
            assert banned not in called, banned


# ═════════════════════════════════════════════════════════════════════════════
# 7. Nothing else moved
# ═════════════════════════════════════════════════════════════════════════════

class TestVarianceChangesNothingElse:
    """§10 / §35: recorded, never used in comparisons."""

    COMPARED = (
        "required_minutes", "qualifying_minutes", "gross_shortfall_minutes",
        "exclusion_minutes", "attributable_minutes",
        "contributes_to_accumulator_minutes", "state",
    )

    def test_the_other_measurements_are_identical_without_a_scheduled_end(
            self, db, org):
        """Removing the scheduled end changes the variance and NOTHING else.

        Two different dates carrying identical evidence, so both records
        coexist and are compared directly rather than across a rollback.
        """
        _capture_requirement(db)
        _seal(db)
        _record_leave(db)
        with_end = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY).record

        other = DAY + dt.timedelta(days=7)      # the next Monday
        shift = dt.timedelta(days=7)
        seal_day(
            db,
            DayKey(organization_id=ORG, user_id=USER, work_date=other),
            intervals=[
                CandidateInterval(
                    seq=1, state=WorkState.WRK.value,
                    start_at_utc=DAY_START_UTC + shift,
                    end_at_utc=DAY_START_UTC + shift
                    + dt.timedelta(minutes=WRK_A_MINUTES),
                    provenance=Provenance.WORKER.value),
                CandidateInterval(
                    seq=2, state=WorkState.WRK.value,
                    start_at_utc=DAY_START_UTC + shift + dt.timedelta(
                        minutes=WRK_A_MINUTES + MEAL_GAP_MINUTES),
                    end_at_utc=DEPARTURE_UTC + shift,
                    provenance=Provenance.WORKER.value),
            ],
            timezone_iana=ZONE_NAME, is_working_day=True,
            working_day_source="org",
            scheduled_end_local=None, scheduled_end_source="system",
            departure_at=DEPARTURE_UTC + shift,
            departure_provenance="WORKER",
            departure_source_ref="clock_session:gv01b",
            created_by="GOLDEN_VECTOR",
        )
        request = dbm.LeaveRequest(
            user_id=USER, organization_id=ORG, leave_type="Casual Leave",
            from_date=dt.datetime(other.year, other.month, other.day),
            to_date=dt.datetime(other.year, other.month, other.day),
            description="GV-01 fixture", status="approved")
        db.add(request)
        db.flush()
        record_leave_intervals(
            db, leave_request=request,
            intervals=[CandidateLeaveInterval(
                seq=1, start_at_utc=LEAVE_START_UTC + shift,
                end_at_utc=LEAVE_START_UTC + shift
                + dt.timedelta(minutes=LEAVE_MINUTES))],
            timezone_iana=ZONE_NAME, zone=ZONE,
            expected_version=None, recorded_by="GOLDEN_VECTOR")
        db.flush()

        without = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=other).record

        assert with_end.schedule_variance_minutes == -60
        assert without.schedule_variance_minutes is None
        for field in self.COMPARED:
            assert getattr(without, field) == getattr(with_end, field), field

    def test_no_threshold_decision_reads_the_variance(self):
        """Structural: nothing compares it."""
        offenders = []
        for path in sorted((BACKEND / "app").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            for line in path.read_text(
                    encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "schedule_variance" not in line:
                    continue
                if any(op in line for op in (" > ", " < ", " >= ", " <= ")):
                    offenders.append(
                        f"{path.relative_to(BACKEND)}: {stripped}")
        assert offenders == [], (
            f"schedule variance is compared somewhere; §35 says it is "
            f"'never used in comparisons': {offenders}")
