"""
The ECO v2 shadow replay harness — §44.1's P0 exit criterion.
=============================================================

    P0 exit: "All golden vectors pass; replay harness clean for 14 days on
              shadow data."

``tests/_replay_harness.py`` is the runner; this file is its verification. It
builds one deterministic fourteen-day fixture, replays it, and proves both
halves of the contract:

* on UNCHANGED input the window replays byte-for-byte clean, and
* on CHANGED input the harness NOTICES, and says which kind of change it was.

The second half is the one that matters. A harness that only ever reports
CLEAN proves nothing — so every divergence kind that can be provoked is
provoked here, and a deliberately corrupted snapshot is fed through the
comparator to show each classification firing.

THIS IS NOT THE SHADOW GATE
---------------------------
No rollout row is created for a real tenant, no tenant is transitioned, and the
verdict vocabulary has exactly two values — ``HARNESS_CLEAN`` and
``HARNESS_NOT_CLEAN``. §44.2's two complete observed workweeks are a different
claim, which no test can make and none here does.
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
    BOUNDARY_CLOCK_IN, BOUNDARY_CLOCK_OUT, append_boundary_event,
)
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState
from app.rollout.models import RolloutControl
from app.schedule_policy import keys as K
from app.work_ledger import Provenance
from app.work_ledger_producer import set_producer_cutover
from tests._replay_harness import (
    WINDOW_DAYS, WINDOW_START, WORKWEEK_KEYS, DivergenceKind, Snapshot,
    build_report, capture, compare, replay_window, run_replay_harness,
    window_dates,
)

UTC = timezone.utc
BACKEND_TESTS = pathlib.Path(__file__).resolve().parent
TZ = "Asia/Kolkata"
MON_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]

ORG = 9910                  # fully configured
UNCONFIGURED_ORG = 9920     # no working-time requirement
W_SHORT, W_FULL, W_DEFER = 9911, 9912, 9913
W_UNRESOLVED, ADMIN = 9921, 9922

CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)

#: The Saturday a Worker clocks in by accident — §24.2 / GV-28's scenario, and
#: the only way a NOT_A_WORKING_DAY record exists at all: the driver's scope is
#: evidence-derived, so a rest day with no clock evidence produces no artifact.
ACCIDENTAL_SATURDAY = date(2026, 9, 12)

#: The Friday a Worker never clocks out — §24.6, a deferred day.
MISSING_CLOCK_OUT_DAY = date(2026, 9, 18)

#: The day the correction test adds evidence to.
CORRECTED_DAY = date(2026, 9, 15)


# ═════════════════════════════════════════════════════════════════════════════
# Fixture
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def replay_engine():
    """A private engine. Several tables on this path are append-only."""
    for module in ("break_classification", "break_treatment",
                   "clock_session_boundary", "config_versioning",
                   "eco_day_alert", "eco_ledger_incident", "eco_threshold",
                   "eval_day", "eval_event", "alert", "notification_platform",
                   "policy_version", "recipient_authority", "rollout",
                   "work_ledger", "work_ledger_producer"):
        try:
            __import__(f"app.{module}.models")
        except ModuleNotFoundError:
            __import__(f"app.{module}")
    import app.eval_day.leave_snapshot  # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(replay_engine):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(replay_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Replay Org"))
        s.add(dbm.Organization(id=UNCONFIGURED_ORG, name="Unconfigured Org"))
        for uid, org in ((W_SHORT, ORG), (W_FULL, ORG), (W_DEFER, ORG),
                         (W_UNRESOLVED, UNCONFIGURED_ORG),
                         (ADMIN, UNCONFIGURED_ORG)):
            s.add(dbm.User(id=uid, name="W", email=f"{uid}@rp.test",
                           organization_id=org, role="employee"))
        s.commit()
        yield s
    _tz.invalidate_tz_cache()


def _at(on: date, hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(on.year, on.month, on.day, hour, minute,
                    tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def _session(db, *, user_id, clock_in, clock_out, organization_id=ORG):
    """One clock session carrying its §45.D-15 boundary provenance."""
    row = dbm.ClockSession(
        user_id=user_id, organization_id=organization_id,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=1, break_seconds=0, offline_seconds=0,
        is_active=clock_out is None)
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=user_id,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=Provenance.WORKER, actor_label="w@t.test")
    if clock_out is not None:
        append_boundary_event(db, session_id=row.id, user_id=user_id,
                              boundary=BOUNDARY_CLOCK_OUT,
                              boundary_at=clock_out,
                              provenance=Provenance.WORKER,
                              actor_label="w@t.test")
    db.commit()
    return row


def _configure(db, *, organization_id=ORG, requirement_minutes=480,
               threshold_minutes=30):
    """Every gate the producer, EVAL-DAY and EVAL-EVENT require.

    ``requirement_minutes=None`` leaves the working-time requirement
    unconfigured, which is what REQUIREMENT_UNRESOLVED means — a real Tenant
    configuration defect, not a broken fixture.
    """
    from app import _tz
    from app.config_versioning import DatabaseConfigVersionProvider
    from app.config_versioning.models import ConfigValue, ConfigVersion
    from app.eco_threshold import (
        ECO_THRESHOLD_KEY, ECO_THRESHOLD_RANK, ECO_THRESHOLD_TIER,
    )
    from app.requirement_resolution import RequirementCandidate
    from tests._eco_policy import capture_test_policy

    db.add(dbm.OrgSettings(organization_id=organization_id,
                           default_timezone=TZ))
    db.add(dbm.AppSettings(organization_id=organization_id, team_key=None,
                           user_id=None, key=K.SETTING_WORKING_DAYS,
                           value=json.dumps(MON_FRI)))
    db.add(dbm.AppSettings(organization_id=organization_id, team_key=None,
                           user_id=None, key=K.SETTING_WEEK_START_DAY,
                           value="MONDAY"))
    db.commit()
    _tz.invalidate_tz_cache()
    set_producer_cutover(db, organization_id=organization_id,
                         cutover_at=CUTOVER, actor_label="owner@example.test")
    db.commit()
    capture_test_policy(db, organization_id=organization_id)

    version_key = f"rp-cfg-{organization_id}"
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=organization_id, config_version_id=version_key,
        effective_from=date(2026, 1, 1),
        candidates=([RequirementCandidate(tier="TENANT_FALLBACK", rank=6,
                                          minutes=requirement_minutes)]
                    if requirement_minutes is not None else []))
    db.flush()
    if threshold_minutes is not None:
        version = db.query(ConfigVersion).filter(
            ConfigVersion.organization_id == organization_id,
            ConfigVersion.version_key == version_key).one()
        db.add(ConfigValue(
            config_version_id=version.id, organization_id=organization_id,
            scope_user_id=None, tier=ECO_THRESHOLD_TIER,
            rank=ECO_THRESHOLD_RANK, key=ECO_THRESHOLD_KEY,
            value_minutes=threshold_minutes))
    db.commit()

    # SHADOW, not ENABLED: §44.2 runs the full chain and delivers nothing.
    db.add(RolloutControl(organization_id=organization_id,
                          control_key=CONTROL_EARLY_CLOCK_OUT_V2,
                          state=RolloutState.SHADOW.value, state_version=1,
                          last_transition_by="test"))
    db.commit()


@pytest.fixture()
def window(db):
    """The fourteen-day fixture, seeded but not yet evaluated.

    Three Workers in one fully-configured tenant:

    ``W_SHORT``  four hours every working day -> weekly shortfall well over the
                 thirty-minute threshold, so BOTH weeks cross.
    ``W_FULL``   eight hours every working day -> no shortfall, neither week
                 crosses. Plus one accidental Saturday clock-in.
    ``W_DEFER``  eight hours, except one Friday with no clock-out at all.
    """
    _configure(db)
    dates = window_dates()
    for day in dates:
        if day.weekday() >= 5:
            continue
        _session(db, user_id=W_SHORT, clock_in=_at(day, 9),
                 clock_out=_at(day, 13))
        _session(db, user_id=W_FULL, clock_in=_at(day, 9),
                 clock_out=_at(day, 17))
        _session(db, user_id=W_DEFER, clock_in=_at(day, 9),
                 clock_out=(None if day == MISSING_CLOCK_OUT_DAY
                            else _at(day, 17)))
    _session(db, user_id=W_FULL, clock_in=_at(ACCIDENTAL_SATURDAY, 10),
             clock_out=_at(ACCIDENTAL_SATURDAY, 11))
    return dates


@pytest.fixture()
def report(db, window):
    return run_replay_harness(db, organization_id=ORG, dates=window)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The window itself
# ═════════════════════════════════════════════════════════════════════════════
class TestTheWindow:
    def test_it_is_fourteen_dated_days(self, window):
        """Not "run the test fourteen times" — fourteen distinct calendar
        dates, consecutive, with no gaps."""
        assert len(window) == WINDOW_DAYS == 14
        assert window[0] == WINDOW_START
        assert all(b - a == timedelta(days=1)
                   for a, b in zip(window, window[1:]))
        assert len(set(window)) == 14

    def test_it_is_two_complete_workweeks(self, window):
        """§44.2's shape. Monday to Sunday, twice."""
        assert window[0].weekday() == 0 and window[-1].weekday() == 6
        assert window[7].weekday() == 0
        assert WORKWEEK_KEYS == (window[0].isoformat(), window[7].isoformat())

    def test_it_is_entirely_in_the_past(self, window):
        """The producer refuses a day whose local day has not ended. A window
        touching today would measure that gate, not determinism."""
        assert window[-1] < date.today()

    def test_it_spans_working_and_non_working_days(self, window):
        assert sum(1 for d in window if d.weekday() < 5) == 10
        assert sum(1 for d in window if d.weekday() >= 5) == 4


# ═════════════════════════════════════════════════════════════════════════════
# 2. The states the window actually exercises
# ═════════════════════════════════════════════════════════════════════════════
class TestWindowCoverage:
    def test_the_states_the_window_produces(self, db, window):
        from app.eval_day.models import EcoDayRecord

        replay_window(db, organization_id=ORG, dates=window)
        states = {r.state for r in db.query(EcoDayRecord).all()}
        # Every state below is produced by the real engine from real evidence.
        # Nothing is fabricated and nothing unsupported is claimed.
        assert {"SHORTFALL", "NO_SHORTFALL", "NOT_A_WORKING_DAY",
                "DEFERRED_PENDING_LEDGER"} <= states

    def test_a_rest_day_with_an_accidental_clock_in_is_recorded(self, db,
                                                                window):
        """§24.2: the record is written "so that the week fold is complete and
        explainable"."""
        from app.eval_day.models import EcoDayRecord

        replay_window(db, organization_id=ORG, dates=window)
        row = db.query(EcoDayRecord).filter(
            EcoDayRecord.user_id == W_FULL,
            EcoDayRecord.work_date == ACCIDENTAL_SATURDAY).one()
        assert row.state == "NOT_A_WORKING_DAY"

    def test_a_rest_day_with_no_evidence_produces_no_artifact(self, db,
                                                              window):
        """The driver's scope is evidence-derived, so a quiet Sunday is
        represented by the ABSENCE of a record. Stated rather than implied:
        absence is a stable outcome and the harness compares it as one."""
        from app.eval_day.models import EcoDayRecord

        replay_window(db, organization_id=ORG, dates=window)
        quiet_sunday = date(2026, 9, 13)
        assert db.query(EcoDayRecord).filter(
            EcoDayRecord.work_date == quiet_sunday).count() == 0

    def test_one_week_crosses_and_one_worker_never_does(self, db, window):
        from app.eval_event.models import EcoThresholdEvent

        replay_window(db, organization_id=ORG, dates=window)
        crossed = {(e.user_id, e.workweek_key) for e in
                   db.query(EcoThresholdEvent).all()}
        assert crossed == {(W_SHORT, WORKWEEK_KEYS[0]),
                           (W_SHORT, WORKWEEK_KEYS[1])}
        assert not any(u == W_FULL for u, _ in crossed)

    def test_a_deferred_day_blocks_its_week_and_nothing_else(self, db, window):
        from app.eval_week import evaluate_workweek

        replay_window(db, organization_id=ORG, dates=window)
        blocked = evaluate_workweek(db, organization_id=ORG, user_id=W_DEFER,
                                    workweek_key=WORKWEEK_KEYS[1])
        clean = evaluate_workweek(db, organization_id=ORG, user_id=W_DEFER,
                                  workweek_key=WORKWEEK_KEYS[0])
        assert blocked.state.value == "DEFERRED_DAY_PRESENT"
        assert clean.state.value == "COMPLETE"

    def test_an_unresolved_requirement_is_reached_in_its_own_tenant(self, db):
        """A tenant that never configured a working-time requirement. Separate
        tenant because the requirement is tenant-scoped at rank 6."""
        from app.eco_day_alert import EcoDayAdministratorAlert
        from app.eval_day.models import EcoDayRecord

        _configure(db, organization_id=UNCONFIGURED_ORG,
                   requirement_minutes=None)
        dates = window_dates()
        for day in dates[:5]:
            _session(db, user_id=W_UNRESOLVED, clock_in=_at(day, 9),
                     clock_out=_at(day, 17),
                     organization_id=UNCONFIGURED_ORG)
        replay_window(db, organization_id=UNCONFIGURED_ORG, dates=dates)

        states = {r.state for r in db.query(EcoDayRecord).filter(
            EcoDayRecord.organization_id == UNCONFIGURED_ORG).all()}
        assert states == {"REQUIREMENT_UNRESOLVED"}
        # §24.3's administrator alert, and never a Worker notification.
        alerts = db.query(EcoDayAdministratorAlert).filter(
            EcoDayAdministratorAlert.organization_id == UNCONFIGURED_ORG).all()
        assert alerts and all(a.audience == "PRIMARY_ADMIN" for a in alerts)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Replay 1 + 2 — unchanged input, clean window
# ═════════════════════════════════════════════════════════════════════════════
class TestCleanReplay:
    def test_the_fourteen_day_window_replays_clean(self, report):
        """The §44.1 headline."""
        assert report.verdict == "HARNESS_CLEAN"
        assert report.is_clean
        assert report.failures == 0
        assert report.divergences == ()

    def test_the_aggregate_counts_are_complete(self, report):
        assert report.total_dates == 14
        assert report.total_workers == 3
        assert report.total_cases == 42
        assert report.clean_cases == 42
        assert report.divergent_cases == 0
        assert report.duplicate_cases == 0
        assert report.unexpected_cases == 0
        assert report.missing_cases == 0
        assert report.input_divergences == 0

    def test_every_date_has_evidence_for_every_worker(self, report):
        rows = [e.as_row() for e in report.evidence]
        assert len(rows) == 42
        assert {r["date"] for r in rows} == {
            d.isoformat() for d in window_dates()}
        assert {r["worker"] for r in rows} == {W_SHORT, W_FULL, W_DEFER}
        assert all(r["replay"] == "CLEAN" for r in rows)
        assert all(r["divergence_reason"] is None for r in rows)

    def test_the_evidence_row_carries_every_required_field(self, report):
        row = next(r.as_row() for r in report.evidence
                   if r.evaluation_state == "SHORTFALL")
        assert set(row) == {
            "date", "worker", "ledger_version", "evaluation_generation",
            "evaluation_state", "content_hash", "workweek_key", "week_state",
            "threshold_result", "replay", "divergence_reason"}
        assert row["ledger_version"] == 1
        assert row["evaluation_generation"] == 1
        assert row["content_hash"] and len(row["content_hash"]) == 64
        assert row["week_state"] == "COMPLETE"

    def test_the_report_is_json_serialisable(self, report):
        """Machine-checkable, per Phase 6. Not a printed narrative."""
        text = json.dumps(report.as_dict(), sort_keys=True)
        assert json.loads(text)["verdict"] == "HARNESS_CLEAN"

    def test_no_duplicate_threshold_event_after_replay(self, db, report):
        from app.eval_event.models import EcoThresholdEvent

        assert db.query(EcoThresholdEvent).count() == 2

    def test_no_duplicate_notification_record_after_replay(self, db, report):
        from app.alert.models import EcoNotification

        # Whatever the first pass recorded, the second recorded nothing more.
        before = db.query(EcoNotification).count()
        replay_window(db, organization_id=ORG, dates=window_dates())
        assert db.query(EcoNotification).count() == before

    def test_no_second_ledger_version_or_generation(self, db, report):
        from app.eval_day.models import EcoDayRecord
        from app.work_ledger.models import WorkDayLedger

        assert all(r.ledger_version == 1
                   for r in db.query(WorkDayLedger).all())
        assert all(r.evaluation_generation == 1
                   for r in db.query(EcoDayRecord).all())

    def test_day_hashes_are_stable_across_a_third_pass(self, db, report):
        """Two passes could both be wrong in the same way; a third proves
        stability rather than coincidence."""
        first = capture(db, organization_id=ORG, dates=window_dates())
        replay_window(db, organization_id=ORG, dates=window_dates())
        third = capture(db, organization_id=ORG, dates=window_dates())
        assert compare(first, third) == []

    def test_snapshot_keys_are_stable(self, db, report):
        """Leave and classification snapshot identity, pinned per Phase 9."""
        snap = capture(db, organization_id=ORG, dates=window_dates())
        assert snap.days
        for facts in snap.days.values():
            assert facts["leave_input_snapshot_key"]
            # NULL is a stable value too — the point is that it does not move.
            assert "classification_snapshot_key" in facts
            assert "break_treatment_profile_ref" in facts

    def test_policy_and_config_versions_are_stable(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        evaluated = [f for f in snap.days.values()
                     if f["state"] not in ("NOT_A_WORKING_DAY",
                                           "DEFERRED_PENDING_LEDGER")]
        assert evaluated
        assert {f["policy_version"] for f in evaluated} == {"test-policy-v1"}
        assert {f["config_version"] for f in evaluated} == {f"rp-cfg-{ORG}"}


# ═════════════════════════════════════════════════════════════════════════════
# 4. Replay 3 — a legitimate correction
# ═════════════════════════════════════════════════════════════════════════════
class TestCorrectionReplay:
    def test_new_evidence_produces_a_new_version_and_generation(self, db,
                                                                window):
        """Supersession, not mutation (§23 / INV-3). The correction semantics
        are the ones already implemented; nothing here invents any."""
        from app.eval_day.models import EcoDayRecord
        from app.work_ledger.models import WorkDayLedger

        replay_window(db, organization_id=ORG, dates=window)
        first = capture(db, organization_id=ORG, dates=window)

        _session(db, user_id=W_FULL, clock_in=_at(CORRECTED_DAY, 18),
                 clock_out=_at(CORRECTED_DAY, 19))
        replay_window(db, organization_id=ORG, dates=window)

        ledgers = sorted(
            (r.ledger_version, r.superseded_by is not None)
            for r in db.query(WorkDayLedger).filter(
                WorkDayLedger.user_id == W_FULL).all()
            if r.work_date.date() == CORRECTED_DAY)
        assert ledgers == [(1, True), (2, False)]

        generations = sorted(
            (r.evaluation_generation, r.superseded_by is not None)
            for r in db.query(EcoDayRecord).filter(
                EcoDayRecord.user_id == W_FULL,
                EcoDayRecord.work_date == CORRECTED_DAY).all())
        assert generations == [(1, True), (2, False)]

    def test_the_superseded_artifact_stays_readable_and_unchanged(self, db,
                                                                  window):
        from app.eval_day.models import EcoDayRecord

        replay_window(db, organization_id=ORG, dates=window)
        original = db.query(EcoDayRecord).filter(
            EcoDayRecord.user_id == W_FULL,
            EcoDayRecord.work_date == CORRECTED_DAY).one()
        original_hash = original.content_hash

        _session(db, user_id=W_FULL, clock_in=_at(CORRECTED_DAY, 18),
                 clock_out=_at(CORRECTED_DAY, 19))
        replay_window(db, organization_id=ORG, dates=window)

        still = db.query(EcoDayRecord).filter(
            EcoDayRecord.user_id == W_FULL,
            EcoDayRecord.work_date == CORRECTED_DAY,
            EcoDayRecord.evaluation_generation == 1).one()
        assert still.content_hash == original_hash
        assert still.superseded_by is not None

    def test_the_correction_is_classified_and_attributed(self, db, window):
        """The harness must not report a correction as an engine fault: the
        INPUT changed, and that is the first thing it says."""
        replay_window(db, organization_id=ORG, dates=window)
        first = capture(db, organization_id=ORG, dates=window)
        _session(db, user_id=W_FULL, clock_in=_at(CORRECTED_DAY, 18),
                 clock_out=_at(CORRECTED_DAY, 19))
        replay_window(db, organization_id=ORG, dates=window)
        second = capture(db, organization_id=ORG, dates=window)

        kinds = {d.kind for d in compare(first, second)}
        assert DivergenceKind.INPUT_DIVERGENCE in kinds
        assert DivergenceKind.UNEXPECTED_ARTIFACT in kinds
        # A new generation is a new IDENTITY, never a duplicate.
        assert DivergenceKind.DUPLICATE_ARTIFACT not in kinds

    def test_unrelated_days_are_untouched_by_a_correction(self, db, window):
        replay_window(db, organization_id=ORG, dates=window)
        first = capture(db, organization_id=ORG, dates=window)
        _session(db, user_id=W_FULL, clock_in=_at(CORRECTED_DAY, 18),
                 clock_out=_at(CORRECTED_DAY, 19))
        replay_window(db, organization_id=ORG, dates=window)
        second = capture(db, organization_id=ORG, dates=window)

        touched_days = {d.key for d in compare(first, second)
                        if d.artifact == "day"}
        assert touched_days
        assert all(CORRECTED_DAY.isoformat() in key for key in touched_days)


# ═════════════════════════════════════════════════════════════════════════════
# 5. The harness can FAIL — every classification, provoked
# ═════════════════════════════════════════════════════════════════════════════
def _mutated(snap: Snapshot, *, section: str, mutate) -> Snapshot:
    """A copy of a snapshot with one section altered. Corrupting a SNAPSHOT,
    never the database: the append-only artifacts must stay untouched."""
    parts = {name: dict(getattr(snap, name))
             for name in ("days", "weeks", "events", "notifications",
                          "incidents", "day_alerts")}
    parts["inputs"] = dict(snap.inputs)
    mutate(parts[section])
    return Snapshot(**parts)


class TestDivergenceDetection:
    """A harness that can only report CLEAN proves nothing."""

    def test_content_divergence_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.days))

        def _bend(days):
            days[key] = {**days[key], "content_hash": "0" * 64}

        found = compare(snap, _mutated(snap, section="days", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.CONTENT_DIVERGENCE]

    def test_identity_divergence_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.days))

        def _bend(days):
            days[key] = {**days[key], "ledger_version": 99}

        found = compare(snap, _mutated(snap, section="days", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.IDENTITY_DIVERGENCE]

    def test_state_divergence_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.days))

        def _bend(days):
            days[key] = {**days[key], "state": "NO_SHORTFALL_BUT_DIFFERENT"}

        found = compare(snap, _mutated(snap, section="days", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.STATE_DIVERGENCE]

    def test_provenance_divergence_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.days))

        def _bend(days):
            days[key] = {**days[key], "policy_version": "other-policy"}

        found = compare(snap, _mutated(snap, section="days", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.PROVENANCE_DIVERGENCE]

    def test_missing_artifact_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())

        def _bend(days):
            days.pop(next(iter(days)))

        found = compare(snap, _mutated(snap, section="days", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.MISSING_ARTIFACT]

    def test_duplicate_artifact_is_detected(self, db, report):
        """A second generation for a subject that already had one, with the
        inputs unchanged — the exact defect a replay is looking for."""
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.days))
        subject = key.rsplit("|", 1)[0]

        def _bend(days):
            days[f"{subject}|2"] = dict(days[key])

        found = compare(snap, _mutated(snap, section="days", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.DUPLICATE_ARTIFACT]

    def test_unexpected_artifact_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.days))

        def _bend(days):
            days["99999|2026-01-01|1"] = dict(days[key])

        found = compare(snap, _mutated(snap, section="days", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.UNEXPECTED_ARTIFACT]

    def test_threshold_divergence_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.events))

        def _bend(events):
            events[key] = {**events[key], "weekly_total_minutes": 1}

        found = compare(snap, _mutated(snap, section="events", mutate=_bend))
        assert [d.kind for d in found] == [DivergenceKind.THRESHOLD_DIVERGENCE]

    def test_notification_divergence_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        assert snap.notifications, "the window must record a notification"
        key = next(iter(snap.notifications))

        def _bend(notifications):
            notifications[key] = {**notifications[key],
                                  "content_hash": "0" * 64}

        found = compare(snap, _mutated(snap, section="notifications",
                                       mutate=_bend))
        assert [d.kind for d in found] == [
            DivergenceKind.NOTIFICATION_DIVERGENCE]

    def test_input_divergence_is_detected(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())

        def _bend(inputs):
            inputs["clock_sessions"] = []

        found = compare(snap, _mutated(snap, section="inputs", mutate=_bend))
        assert DivergenceKind.INPUT_DIVERGENCE in {d.kind for d in found}

    def test_every_taxonomy_member_is_reachable(self):
        """The taxonomy is closed and each member is provoked above."""
        assert {k.value for k in DivergenceKind} == {
            "IDENTITY_DIVERGENCE", "CONTENT_DIVERGENCE", "STATE_DIVERGENCE",
            "PROVENANCE_DIVERGENCE", "DUPLICATE_ARTIFACT", "MISSING_ARTIFACT",
            "UNEXPECTED_ARTIFACT", "THRESHOLD_DIVERGENCE",
            "NOTIFICATION_DIVERGENCE", "INPUT_DIVERGENCE"}

    def test_a_divergent_report_says_not_clean(self, db, report):
        snap = capture(db, organization_id=ORG, dates=window_dates())
        key = next(iter(snap.days))

        def _bend(days):
            days[key] = {**days[key], "content_hash": "0" * 64}

        bad = build_report(snap, _mutated(snap, section="days", mutate=_bend),
                           dates=window_dates())
        assert bad.verdict == "HARNESS_NOT_CLEAN"
        assert bad.is_clean is False
        assert bad.failures == 1
        assert bad.divergent_cases >= 1
        assert any(r.as_row()["divergence_reason"] == "CONTENT_DIVERGENCE"
                   for r in bad.evidence)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Scope held
# ═════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_the_verdict_cannot_claim_the_shadow_gate(self):
        """§44.2's two observed workweeks are not a claim any test can make."""
        import ast

        source = (BACKEND_TESTS / "_replay_harness.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)

        # DOCSTRINGS ARE EXCLUDED. The module docstring names
        # ``SHADOW_GATE_SATISFIED`` precisely in order to say it is not a value
        # this module can produce — a text scan would flag the sentence that
        # states the rule. What matters is the values the code EMITS.
        docstrings = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))                     and body and isinstance(body[0], ast.Expr)                     and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        emitted = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and id(n) not in docstrings}

        # VERDICT literals only. The per-row status vocabulary
        # ("CLEAN" / "DIVERGENT") is a different thing and is not a verdict.
        assert {v for v in emitted if v.startswith("HARNESS")} == {
            "HARNESS_CLEAN", "HARNESS_NOT_CLEAN"}
        for word in ("GATE", "SATISFIED", "SHADOW_CLEAN", "WORKWEEKS_OBSERVED"):
            assert not any(word in v for v in emitted), word

    def test_the_harness_is_test_only(self):
        """No production subsystem was created: nothing under ``app/`` imports
        it, and it adds no command, scheduler or worker."""
        import ast
        import pathlib

        root = BACKEND_TESTS.parent
        offenders = []
        for path in (root / "app").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                module = getattr(node, "module", None)
                if module and "_replay_harness" in module:
                    offenders.append(path.name)
        assert offenders == [], offenders
        assert not list(root.glob("run_replay*.py"))

    def test_the_harness_calculates_nothing(self):
        """It invokes the driver and reads artifacts. It must never compute a
        measurement of its own."""
        import ast

        tree = ast.parse((BACKEND_TESTS / "_replay_harness.py").read_text(
            encoding="utf-8"))
        called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for banned in ("round_half_up_minutes", "compute_result_hash",
                       "evaluate_and_record_day", "evaluate_threshold_event",
                       "produce_and_seal_work_day", "seal_day",
                       "record_threshold_notification"):
            assert banned not in called, banned
        assert "run_eco_calculation_pass" in called
        assert "evaluate_workweek" in called  # read-only, for the week result

    def test_no_notification_is_delivered_under_shadow(self, db, window,
                                                       monkeypatch):
        """§44.2: full evaluation, full event emission, NO notifications."""
        import app.email_service as mailer

        sent = []
        monkeypatch.setattr(mailer, "_send",
                            lambda **kw: (sent.append(kw), True)[1])
        run_replay_harness(db, organization_id=ORG, dates=window)
        assert sent == []

    def test_the_rollout_stays_in_shadow(self, db, report):
        row = db.query(RolloutControl).filter(
            RolloutControl.organization_id == ORG).one()
        assert row.state == "SHADOW"
        assert row.state_version == 1

    def test_tenant_isolation_holds(self, db, window):
        """A second tenant's artifacts never enter this tenant's snapshot."""
        _configure(db, organization_id=UNCONFIGURED_ORG,
                   requirement_minutes=None)
        for day in window[:3]:
            _session(db, user_id=W_UNRESOLVED, clock_in=_at(day, 9),
                     clock_out=_at(day, 17), organization_id=UNCONFIGURED_ORG)
        replay_window(db, organization_id=UNCONFIGURED_ORG, dates=window)

        mine = run_replay_harness(db, organization_id=ORG, dates=window)
        assert mine.verdict == "HARNESS_CLEAN"
        assert {e.user_id for e in mine.evidence} == {W_SHORT, W_FULL, W_DEFER}

    def test_the_report_carries_no_worker_contact_data(self, report):
        text = json.dumps(report.as_dict())
        for leaked in ("@rp.test", "@example.test"):
            assert leaked not in text, leaked
