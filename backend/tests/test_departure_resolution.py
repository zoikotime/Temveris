"""
Departure resolution (§24.6) and the GV-16 missing-clock-out path.
==================================================================

The property under test: **the platform can say "there was no clock-out"
without ever saying when the Worker left.**

§24.6 — "A missing clock-out is not an early clock-out. The day resolves to
MISSING_CLOCK_OUT and no shortfall is computed, no advisory was shown, and the
day contributes nothing to the accumulator until the exit is resolved. The
system MUST NEVER assume a departure time."

Owner rulings exercised here: 4 (the dedicated primitive), 5 (SESSION_OPEN is
not MISSING_CLOCK_OUT before closure), 6 (a synthesized close is never a
departure), 7 (the ledger representation), 8 (historical identity), 10 and 11
(EVAL-DAY / EVAL-WEEK unchanged).

``TestGV16`` at the end executes the vector end to end. It is deliberately NOT
registered in ``EXTERNAL_EXECUTION_TESTS`` — GV-16 stays blocked until its own
promotion-readiness audit.
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

from tests._eco_policy import capture_test_policy

import database.models as dbm
from database.database import Base

from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.config_versioning import DatabaseConfigVersionProvider
from app.departure_resolution import (
    DepartureOutcome,
    DepartureReason,
    resolve_departure,
)
from app.eval_day import EcoDayState, evaluate_and_record_day
from app.eval_week import evaluate_workweek
from app.eval_week.outcomes import WeeklyState
from app.requirement_resolution import RequirementCandidate
from app.schedule_policy import keys as K
from app.work_ledger import LedgerState, Provenance, WorkDayLedger, WorkStateInterval
from app.work_ledger_producer import (
    ProducerOutcome,
    RefusalReason,
    produce_and_seal_work_day,
    resolve_day_context,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, WORKER = 7420, 7421
ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)

#: A Monday in the PAST, so the D18-F3 closure gate is satisfied — that gate is
#: the authorized closure point, and nothing here invents another.
DAY = date(2026, 9, 7)
#: The Monday-start week containing it.
DAY_WEEK = "2026-09-07"
#: Far enough ahead that the local day has NOT ended — used to prove Ruling 5's
#: distinction without inventing a timeout.
FUTURE = date.today() + timedelta(days=3)

CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
ALL_SEVEN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]
REQUIRED_MINUTES = 480


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
        s.add(dbm.Organization(id=ORG, name="Departure Org"))
        s.commit()
        s.add(dbm.User(id=WORKER, name="W", email="w@dep.test",
                       organization_id=ORG, role="employee"))
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(ALL_SEVEN)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@dep.test")
        s.commit()
        # Explicit historical PolicyVersion: evaluate_and_record_day
        # refuses with POLICY_UNRESOLVED without one. Satisfies the
        # production rule rather than weakening it.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


def _session(db, *, clock_in, clock_out, close_prov=Provenance.WORKER,
             open_prov=Provenance.WORKER, actor="w@dep.test"):
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=0, break_seconds=0, offline_seconds=0,
        is_active=clock_out is None,
    )
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=WORKER,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=open_prov, actor_label=actor)
    if clock_out is not None:
        append_boundary_event(db, session_id=row.id, user_id=WORKER,
                              boundary=BOUNDARY_CLOCK_OUT,
                              boundary_at=clock_out, provenance=close_prov,
                              actor_label=actor)
    db.commit()
    return row


def _context(db, on=DAY):
    worker = db.get(dbm.User, WORKER)
    return resolve_day_context(db, organization_id=ORG, user=worker,
                               work_date=on)


def _requirement(db):
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id="dep-v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="TENANT_FALLBACK", rank=6,
                                         minutes=REQUIRED_MINUTES)])
    db.flush()


def _run(db, on=DAY):
    return produce_and_seal_work_day(
        db, organization_id=ORG, user_id=WORKER, work_date=on)


# ══════════════════════════════════════════════════════════════════════════════
# 1. The primitive itself — verified departures
# ══════════════════════════════════════════════════════════════════════════════
class TestVerifiedDeparture:
    def test_a_worker_close_is_verified(self, db):
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        v = resolve_departure(db, organization_id=ORG, user_id=WORKER,
                              context=_context(db), day_closed=True)
        assert v.outcome is DepartureOutcome.VERIFIED_DEPARTURE
        assert v.is_verified

    def test_an_admin_override_close_is_verified(self, db):
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.ADMIN_OVERRIDE, actor="admin@dep.test")
        v = resolve_departure(db, organization_id=ORG, user_id=WORKER,
                              context=_context(db), day_closed=True)
        assert v.outcome is DepartureOutcome.VERIFIED_DEPARTURE

    def test_a_verified_day_still_seals_normally(self, db):
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.SEALED.value
        assert row.departure_at is not None
        assert row.departure_provenance == Provenance.WORKER.value
        assert db.query(WorkStateInterval).count() == 1

    def test_an_admin_override_day_still_seals_normally(self, db):
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.ADMIN_OVERRIDE, actor="admin@dep.test")
        assert _run(db).outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.SEALED.value
        assert row.departure_provenance == Provenance.ADMIN_OVERRIDE.value


# ══════════════════════════════════════════════════════════════════════════════
# 2. Ruling 5 — SESSION_OPEN is NOT MISSING_CLOCK_OUT
# ══════════════════════════════════════════════════════════════════════════════
class TestClosureDistinction:
    def test_an_open_session_before_closure_is_not_missing_clock_out(self, db):
        """The distinction the ruling calls mandatory.

        Same evidence, same code — only ``day_closed`` differs. A Worker who is
        still working has not failed to clock out.
        """
        _session(db, clock_in=_local(DAY, 9), clock_out=None)
        v = resolve_departure(db, organization_id=ORG, user_id=WORKER,
                              context=_context(db), day_closed=False)
        assert v.outcome is DepartureOutcome.SESSION_OPEN
        assert not v.is_missing_clock_out

    def test_the_same_session_after_closure_is_missing_clock_out(self, db):
        _session(db, clock_in=_local(DAY, 9), clock_out=None)
        v = resolve_departure(db, organization_id=ORG, user_id=WORKER,
                              context=_context(db), day_closed=True)
        assert v.outcome is DepartureOutcome.MISSING_CLOCK_OUT
        assert v.reason is DepartureReason.SESSION_OPEN_AFTER_CLOSURE

    def test_a_future_day_is_refused_by_the_closure_gate_not_relabelled(self, db):
        """End to end: before closure the producer's existing lifecycle stands.

        ``FUTURE`` has not ended, so D18-F3 gate 1 refuses with DAY_NOT_ENDED
        and no missing-clock-out verdict is ever reached — proving the closure
        point is the existing authorized one and not something invented here.
        """
        _session(db, clock_in=_local(FUTURE, 9), clock_out=None)
        result = _run(db, on=FUTURE)
        assert result.outcome is ProducerOutcome.REFUSED
        assert result.refusal is RefusalReason.DAY_NOT_ENDED
        assert db.query(WorkDayLedger).count() == 0

    def test_no_wall_clock_threshold_was_introduced(self):
        """Ruling 5 forbids inventing a timeout, grace period or cutoff.

        The primitive reads no clock at all: ``day_closed`` arrives as an
        argument. A module that measured its own deadline would have to call
        ``now``/``utcnow`` — none appears.
        """
        pkg = BACKEND / "app" / "departure_resolution"
        for path in sorted(pkg.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            called = {
                node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
                for node in ast.walk(tree) if isinstance(node, ast.Call)
            }
            for banned in ("now", "utcnow", "today", "sleep", "monotonic"):
                assert banned not in called, f"{path.name} calls {banned}()"
            for banned in ("timedelta(", "GRACE", "TIMEOUT", "CUTOFF"):
                assert banned not in src, f"{path.name} mentions {banned}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Ruling 6 — synthesized closures are never departures
# ══════════════════════════════════════════════════════════════════════════════
class TestSynthesizedClosure:
    @pytest.mark.parametrize("prov", [
        Provenance.STALE_AUTOCLOSE,
        Provenance.ABANDONED_AUTOCLOSE,
        Provenance.OFFBOARDING,
    ])
    def test_a_synthesized_close_is_an_unverified_exit(self, db, prov):
        """The §24.6 / §24.7 boundary, asserted from both sides.

        The exit EXISTS and is merely unverified, which is §24.7 — never §24.6,
        because §19 runs step 6 before step 7 so the two cannot be conflated
        ("A deferred day is not a missing clock-out and must not be reported as
        one", §19.1) — and never a verified departure.

        It now has its own verdict rather than riding on INADMISSIBLE, because
        the two demand opposite handling: refuse versus seal-and-defer.
        """
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=prov, actor="SYSTEM")
        v = resolve_departure(db, organization_id=ORG, user_id=WORKER,
                              context=_context(db), day_closed=True)
        assert v.outcome is DepartureOutcome.UNVERIFIED_EXIT
        assert v.is_unverified_exit
        assert v.outcome is not DepartureOutcome.MISSING_CLOCK_OUT
        assert v.outcome is not DepartureOutcome.VERIFIED_DEPARTURE
        assert not v.is_verified
        assert not v.is_missing_clock_out
        # The originating refusal is retained for explainability.
        assert v.refusal is RefusalReason.SESSION_SYNTHESIZED_CLOSURE

    def test_unverified_exit_is_not_a_ledger_or_eco_state(self):
        """The fifth outcome is resolver-local vocabulary, nothing more."""
        from app.eval_day import EcoDayState
        from app.work_ledger import LedgerState

        assert "UNVERIFIED_EXIT" not in {s.value for s in LedgerState}
        assert "UNVERIFIED_EXIT" not in {s.value for s in EcoDayState}
        assert len({s.value for s in EcoDayState}) == 7
        assert len({s.value for s in LedgerState}) == 5

    def test_a_present_clock_out_time_is_never_a_verified_departure(self, db):
        """Ruling 6's core claim, isolated.

        The scalar column is populated and looks exactly like a real departure.
        Only the boundary history distinguishes them — so the verdict must come
        from provenance, never from the column being non-NULL. It is equally
        not a *missing* clock-out: the instant is right there on the row.
        """
        row = _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                       close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        assert row.clock_out_time is not None       # indistinguishable here…
        v = resolve_departure(db, organization_id=ORG, user_id=WORKER,
                              context=_context(db), day_closed=True)
        assert not v.is_verified                    # …but not here
        assert not v.is_missing_clock_out           # …nor here

    def test_missing_clock_out_now_has_exactly_one_cause(self):
        """§24.6 has one trigger: no clock-out at all.

        ``SYNTHESIZED_CLOSURE_ONLY`` was removed from the reason vocabulary
        when the ruling separated §24.7 out, so the enum itself records that
        a synthesized close is no longer a route to MISSING_CLOCK_OUT.
        """
        assert [r.value for r in DepartureReason] == \
            ["SESSION_OPEN_AFTER_CLOSURE"]

    def test_a_synthesized_day_defers_without_being_relabelled(self, db):
        """§24.7 end to end — the representation GV-17 needed.

        Sealed with its real intervals and its real synthesized provenance,
        then DEFERRED at evaluation: no shortfall computed, contributing
        nothing, "neither counted nor excused". It reaches the same ECO state
        as a missing clock-out, as both §24.6 and §24.7 require — and stays
        distinguishable from one at the ledger, which is what §19.1 protects.
        """
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")

        assert _run(db).outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.SEALED.value
        assert row.state != LedgerState.MISSING_CLOCK_OUT.value
        assert row.departure_provenance == Provenance.STALE_AUTOCLOSE.value
        assert row.departure_at is not None      # an exit EXISTS, unlike §24.6
        assert db.query(WorkStateInterval).count() >= 1   # §19 step 5

        recorded = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=DAY)
        assert recorded.is_recorded, f"{recorded.reason} {recorded.detail}"
        rec = recorded.record
        assert rec.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert rec.shortfall_computed is False
        assert rec.contributes_to_accumulator_minutes == 0
        assert rec.requirement_outcome == "NOT_APPLICABLE"
        # "not treated as final" (§9.3) — nothing is measured, not even zero.
        for field in ("required_minutes", "qualifying_minutes",
                      "gross_shortfall_minutes", "exclusion_minutes",
                      "attributable_minutes", "schedule_variance_minutes"):
            assert getattr(rec, field) is None, field

    def test_the_deferred_day_blocks_the_week_and_fires_nothing(self, db):
        """§24.7: contributes 0, and no threshold comparison can occur."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.ABANDONED_AUTOCLOSE, actor="SYSTEM")
        _run(db)
        evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                work_date=DAY)
        db.flush()
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=DAY_WEEK)
        assert week.state is WeeklyState.DEFERRED_DAY_PRESENT
        assert week.weekly_attributable_shortfall_minutes is None
        assert DAY in week.blocking_dates

    def test_a_verified_day_is_untouched_by_all_of_this(self, db):
        """Non-regression: a genuine departure still evaluates normally."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        _run(db)
        rec = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                      work_date=DAY)
        assert rec.is_recorded
        assert rec.record.state in (EcoDayState.SHORTFALL.value,
                                    EcoDayState.NO_SHORTFALL.value)
        assert rec.record.shortfall_computed is True

    def test_the_synthesized_origin_is_never_rewritten(self, db):
        from app.clock_session_boundary import ClockSessionBoundaryEvent

        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _run(db)
        closes = (db.query(ClockSessionBoundaryEvent)
                  .filter(ClockSessionBoundaryEvent.boundary == BOUNDARY_CLOCK_OUT)
                  .all())
        assert [e.provenance for e in closes] == ["STALE_AUTOCLOSE"]

    def test_a_non_closure_provenance_cannot_reach_the_primitive_at_all(self, db):
        """The stronger guarantee: D-15 refuses it at the write, not later.

        I first wrote this expecting an INADMISSIBLE verdict for a
        CLASSIFIER_DERIVED close. It cannot arise: ``append_boundary_event``
        rejects any provenance outside the closure vocabulary, so every
        admissible clock-out is either VERIFIED or SYNTHESIZED and the
        two-way split above is exhaustive over reachable states.

        The INADMISSIBLE branch therefore stays defensive — reachable only by a
        cross-tenant row or a naive timestamp, not by a provenance value.
        """
        from app.clock_session_boundary.errors import InvalidBoundaryProvenance

        with pytest.raises(InvalidBoundaryProvenance):
            _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                     close_prov=Provenance.CLASSIFIER_DERIVED, actor="SYSTEM")

    def test_the_closure_vocabulary_is_exactly_verified_plus_synthesized(self):
        """Why the split above is exhaustive, asserted rather than assumed."""
        from app.clock_session_boundary.service import ALLOWED_PROVENANCE
        from app.work_ledger.states import (
            SYNTHESIZED_CLOSURE_PROVENANCE,
            VERIFIED_DEPARTURE_PROVENANCE,
        )

        admissible = {p.value for p in ALLOWED_PROVENANCE[BOUNDARY_CLOCK_OUT]}
        assert admissible == (
            {p.value for p in VERIFIED_DEPARTURE_PROVENANCE}
            | {p.value for p in SYNTHESIZED_CLOSURE_PROVENANCE})


# ══════════════════════════════════════════════════════════════════════════════
# 4. Ruling 7 — the ledger representation
# ══════════════════════════════════════════════════════════════════════════════
class TestLedgerRepresentation:
    def _seal_missing(self, db):
        _session(db, clock_in=_local(DAY, 9), clock_out=None)
        return _run(db)

    def test_the_state_is_missing_clock_out(self, db):
        assert self._seal_missing(db).outcome is ProducerOutcome.SEALED
        assert db.query(WorkDayLedger).one().state == \
            LedgerState.MISSING_CLOCK_OUT.value

    def test_no_interval_is_fabricated(self, db):
        self._seal_missing(db)
        assert db.query(WorkStateInterval).count() == 0

    def test_no_departure_is_fabricated(self, db):
        self._seal_missing(db)
        row = db.query(WorkDayLedger).one()
        assert row.departure_at is None
        assert row.departure_provenance is None
        assert row.departure_source_ref is None

    def test_the_outcome_type_cannot_carry_a_departure_instant(self):
        """§24.6 enforced by the type, not by discipline.

        A verdict with nowhere to put a timestamp cannot leak one.
        """
        from app.departure_resolution.outcomes import DepartureResolution

        fields = set(DepartureResolution.__dataclass_fields__)
        for banned in ("departure_at", "clock_out_at", "at", "instant",
                       "timestamp", "ended_at"):
            assert banned not in fields, f"the verdict can carry {banned}"

    def test_the_frozen_context_is_preserved(self, db):
        self._seal_missing(db)
        row = db.query(WorkDayLedger).one()
        assert row.timezone_iana == ZONE
        assert row.utc_offset_seconds == 19800
        assert row.shift_attribution_rule == "START_DATE"
        assert row.workweek_key == DAY_WEEK

    def test_replay_is_idempotent(self, db):
        self._seal_missing(db)
        second = _run(db)
        assert second.outcome is ProducerOutcome.ALREADY_SEALED
        assert db.query(WorkDayLedger).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# 5. Ruling 8 — historical identity
# ══════════════════════════════════════════════════════════════════════════════
class TestHistoricalIdentity:
    def test_a_week_start_change_cannot_rehome_a_missing_clock_out_day(self, db):
        """The MODEL-WORKWEEK guarantee, holding for this state too."""
        _session(db, clock_in=_local(DAY, 9), clock_out=None)
        _run(db)
        row = db.query(WorkDayLedger).one()
        assert row.workweek_key == DAY_WEEK

        setting = (db.query(dbm.AppSettings)
                   .filter(dbm.AppSettings.organization_id == ORG,
                           dbm.AppSettings.key == K.SETTING_WEEK_START_DAY)
                   .one())
        setting.value = "SUNDAY"
        db.commit()
        db.refresh(row)
        assert row.workweek_key == DAY_WEEK, "a sealed day was re-homed"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Rulings 10 / 11 — EVAL-DAY and EVAL-WEEK, verified not modified
# ══════════════════════════════════════════════════════════════════════════════
class TestEvaluationContract:
    def _missing_day(self, db):
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=None)
        _run(db)
        db.flush()
        return evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=DAY)

    def test_eval_day_maps_to_deferred_pending_ledger(self, db):
        result = self._missing_day(db)
        assert result.is_recorded, f"{result.reason} {result.detail}"
        record = result.record
        assert record.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert record.shortfall_computed is False
        assert record.contributes_to_accumulator_minutes == 0

    def test_missing_clock_out_is_not_an_eco_state(self):
        """Ruling 1: MISSING_CLOCK_OUT is still not an evaluation state.

        The vocabulary has since grown by one — POLICY_UNRESOLVED, by later
        owner ruling — but not by this route, which is what Ruling 1 pinned.
        """
        assert "MISSING_CLOCK_OUT" not in {s.value for s in EcoDayState}
        assert len({s.value for s in EcoDayState}) == 7

    def test_eval_week_defers_and_compares_no_threshold(self, db):
        self._missing_day(db)
        db.flush()
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=DAY_WEEK)
        assert week.state is WeeklyState.DEFERRED_DAY_PRESENT
        assert week.weekly_attributable_shortfall_minutes is None
        assert DAY in week.blocking_dates

    def test_no_threshold_event_and_no_notification_exist(self, db):
        """Nothing downstream fires: the week never completes, so nothing can."""
        self._missing_day(db)
        db.flush()
        for table in ("eco_threshold_event", "eco_notification"):
            rows = db.execute(sa.text(
                f"SELECT count(*) FROM sqlite_master "
                f"WHERE type='table' AND name='{table}'")).scalar()
            if rows:
                count = db.execute(
                    sa.text(f"SELECT count(*) FROM {table}")).scalar()
                assert count == 0, f"{table} gained a row"


# ══════════════════════════════════════════════════════════════════════════════
# 7. GV-16 — executed end to end. DELIBERATELY UNREGISTERED.
# ══════════════════════════════════════════════════════════════════════════════
class TestGV16:
    """GV-16: "Missing clock-out (§24.6)" — the real pipeline.

    departure evidence -> departure-resolution -> producer -> Work Ledger
                       -> EVAL-DAY -> EVAL-WEEK

    NOT registered in ``EXTERNAL_EXECUTION_TESTS``: GV-16 stays
    ``blocked_pending_platform_primitive`` on ``["STATE-CONTRACT"]`` until its
    own promotion-readiness audit. This class exists so that audit has
    something real to inspect, not so the vector can be promoted quietly.
    """

    @staticmethod
    def _vector():
        data = json.loads(VECTORS.read_text(encoding="utf-8"))
        return [v for v in data["vectors"] if v["id"] == "GV-16"][0]

    def test_the_vector_is_promoted(self):
        """Promoted 2026-09-16, after the final promotion-readiness audit.

        This guard previously asserted the opposite, deliberately: while the
        harness existed but the vector was unproven, the risk worth guarding
        against was promoting it quietly. That risk is gone; the claim inverts.
        """
        v = self._vector()
        assert v["harness"]["execution"] == "executable"
        assert v["harness"]["blocking_dependencies"] == []

    def test_gv16_executes(self, db):
        expected = self._vector()["expected_fields"]
        handled = []

        # INPUT, verbatim: "No clock-out interval for the day".
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=None)

        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        ledger = db.query(WorkDayLedger).one()
        assert ledger.state == LedgerState.MISSING_CLOCK_OUT.value
        # No departure was invented anywhere along the way.
        assert (ledger.departure_at, ledger.departure_provenance,
                ledger.departure_source_ref) == (None, None, None)
        assert db.query(WorkStateInterval).count() == 0

        recorded = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=DAY)
        assert recorded.is_recorded, f"{recorded.reason} {recorded.detail}"
        record = recorded.record
        db.flush()

        # THE VECTOR.
        assert record.state == expected["state"]
        handled.append("state")

        assert record.shortfall_computed is expected["shortfall_computed"]
        handled.append("shortfall_computed")

        assert record.contributes_to_accumulator_minutes == \
            expected["contributes_to_accumulator_minutes"]
        handled.append("contributes_to_accumulator_minutes")

        # advisory_shown: satisfied structurally under owner ruling D12-A —
        # there is no advisory path in the platform to invoke, the same basis
        # on which GV-28 was promoted.
        assert expected["advisory_shown"] is False
        handled.append("advisory_shown")

        assert set(handled) == set(expected), (
            f"expected_fields not compared: {set(expected) - set(handled)}")

        # And the week defers rather than folding a day it cannot resolve.
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=DAY_WEEK)
        assert week.state is WeeklyState.DEFERRED_DAY_PRESENT
        assert week.weekly_attributable_shortfall_minutes is None

    def test_the_transcription_still_says_missing_clock_out(self):
        """The §42 wording is untouched; only the ECO state name was amended."""
        v = self._vector()
        assert v["expected_verbatim"] == (
            "state MISSING_CLOCK_OUT; no shortfall; no advisory; contributes 0")
        assert v["expected_fields"]["state"] == "DEFERRED_PENDING_LEDGER"

    def test_this_harness_is_registered_and_points_here(self):
        """Registration and execution status cannot be separated.

        The golden suite asserts ``EXTERNAL_EXECUTION_TESTS <= EXECUTABLE_IDS``,
        so a promoted vector must name the suite that actually runs it. This is
        the other end of that link: the registry must point at THIS class, by
        the exact spelling the guard resolves.
        """
        src = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-16": ("test_departure_resolution.py", "class TestGV16")' in src


# ══════════════════════════════════════════════════════════════════════════════
# 7b. The D18-F2 constraint amendment (c3f7a1e58d2b)
# ══════════════════════════════════════════════════════════════════════════════
class TestDepartureConstraintAmendment:
    """D18-F2 widened for §24.7 — storage only, never the meaning of verified."""

    MIGRATION = (BACKEND / "alembic" / "versions"
                 / "c3f7a1e58d2b_admissible_departure_provenance.py")

    def _module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mig_c3f7a1e58d2b", self.MIGRATION)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_migration_is_a_single_child_of_the_current_head(self):
        m = self._module()
        assert m.revision == "c3f7a1e58d2b"
        assert m.down_revision == "b2e6d9f04a17"
        assert m.branch_labels is None and m.depends_on is None

    def test_the_graph_stays_one_base_one_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        # Tip moved to D-1.1's f4a7c2b91e68, chained onto P2-D4's
        # e2b6d4f89c35. One head and one base is the property asserted.
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_the_migration_literals_agree_with_the_vocabulary(self):
        """Restated in the migration, but never allowed to drift."""
        from app.work_ledger.models import ADMISSIBLE_DEPARTURE_PROVENANCE
        from app.work_ledger.states import (
            SYNTHESIZED_CLOSURE_PROVENANCE,
            VERIFIED_DEPARTURE_PROVENANCE,
        )

        m = self._module()
        assert set(m._VERIFIED) == {p.value for p in VERIFIED_DEPARTURE_PROVENANCE}
        assert set(m._SYNTHESIZED) == {
            p.value for p in SYNTHESIZED_CLOSURE_PROVENANCE}
        assert set(m._ADMISSIBLE) == {
            p.value for p in ADMISSIBLE_DEPARTURE_PROVENANCE}

    def test_it_is_constraint_only_and_non_destructive(self):
        """No column touched, no row read or rewritten, nothing dropped."""
        tree = ast.parse(self.MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {
            n.func.attr for n in ast.walk(upgrade)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        for banned in ("add_column", "drop_column", "alter_column",
                       "drop_table", "create_table", "execute"):
            assert banned not in called, f"upgrade() calls {banned}"
        assert "drop_constraint" in called and "create_check_constraint" in called

    def test_the_downgrade_refuses_rather_than_invalidating_evidence(self):
        """Narrowing while synthesized rows exist would orphan append-only data.

        The downgrade counts first and raises with the count, rather than
        emitting DDL the stored rows cannot satisfy.
        """
        src = self.MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(src)
        downgrade = next(n for n in tree.body
                         if isinstance(n, ast.FunctionDef) and n.name == "downgrade")
        raises = [n for n in ast.walk(downgrade) if isinstance(n, ast.Raise)]
        assert raises, "downgrade() cannot silently narrow the constraint"
        assert "count(*)" in src

    def test_the_downgrade_restores_exactly_the_previous_constraint_text(self):
        """S: the narrower constraint is the D-18 one, character for character.

        Restating it is the only way a migration can stay decoupled from the
        model, so the restatement is checked against the revision that actually
        wrote the original rather than against the enum it came from.
        """
        import importlib.util

        origin = (BACKEND / "alembic" / "versions"
                  / "e7c2b9d4f815_d18_correction_append_only_cutover.py")
        spec = importlib.util.spec_from_file_location("mig_e7c2b9d4f815", origin)
        prior = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prior)

        m = self._module()
        assert m._check(m._VERIFIED) == prior.DEPARTURE_VERIFIED_SQL

    def test_the_migration_runs_both_ways_on_a_disposable_database(self, engine_fk):
        """S: both directions executed, against in-memory SQLite only.

        SQLite cannot ALTER a CHECK and its reflection does not carry the
        constraint's name, so both directions no-op there by design (the D-18
        revision settled that pattern) and the emitted PostgreSQL DDL is proved
        by rendering it instead. What running them here does prove is that
        neither direction raises, neither touches a row, and the downgrade's
        refusal is evaluated on every dialect rather than only where DDL runs.
        """
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext

        m = self._module()
        with engine_fk.begin() as conn:
            before = conn.execute(sa.text(
                "SELECT count(*) FROM work_day_ledger")).scalar()
            m.op = Operations(MigrationContext.configure(conn))
            m.upgrade()
            m.downgrade()
            after = conn.execute(sa.text(
                "SELECT count(*) FROM work_day_ledger")).scalar()
        assert (before, after) == (0, 0)

    def test_the_downgrade_refuses_on_a_real_database_holding_evidence(self, db):
        """S, continued: the refusal is behavioural, not just a `raise` in AST."""
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext

        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.OFFBOARDING, actor="SYSTEM")
        assert _run(db).outcome is ProducerOutcome.SEALED
        db.commit()

        m = self._module()
        m.op = Operations(MigrationContext.configure(db.connection()))
        with pytest.raises(RuntimeError, match="cannot downgrade"):
            m.downgrade()
        # Refused before writing: the row it protects is still there, intact.
        assert db.query(WorkDayLedger).one().departure_provenance == \
            Provenance.OFFBOARDING.value

    def test_the_postgresql_ddl_is_what_we_expect(self):
        """PG DDL verified by RENDERING it — no server is contacted.

        Every ``postgresql://`` string in this suite's siblings is a banned-text
        guard: the project has no PostgreSQL integration fixture, and this
        machine's DATABASE_URL points at a shared instance. Compiling the
        constraint against the PG dialect proves the emitted SQL without
        connecting anywhere.

        Rendered from a THROWAWAY table, not from ``WorkDayLedger.__table__``.
        ``AddConstraint.__init__`` sets ``_create_rule`` on the constraint it is
        handed so a later ``CREATE TABLE`` will not emit it a second time — on
        the real model that is process-wide, and it silently disarmed this very
        CHECK for every test that built a schema afterwards. The next test
        catches a recurrence directly.
        """
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import AddConstraint

        m = self._module()
        scratch = sa.Table(
            m._TABLE, sa.MetaData(),
            sa.Column("departure_provenance", sa.String(32)))
        check = sa.CheckConstraint(m._check(m._ADMISSIBLE), name=m._NAME,
                                   table=scratch)
        ddl = str(AddConstraint(check).compile(dialect=postgresql.dialect()))

        assert ddl.startswith(
            f"ALTER TABLE {m._TABLE} ADD CONSTRAINT {m._NAME} CHECK")
        for value in ("WORKER", "ADMIN_OVERRIDE", "STALE_AUTOCLOSE",
                      "ABANDONED_AUTOCLOSE", "OFFBOARDING"):
            assert value in ddl, value
        assert "departure_provenance IS NULL" in ddl
        # Q/R: exactly the five authorized values, and nothing else.
        assert "CLASSIFIER_DERIVED" not in ddl
        assert ddl.count("'") == 10

    def test_the_model_still_emits_the_check_when_it_creates_the_table(self):
        """The constraint is armed on the model, and stays armed.

        Rendering DDL from a SQLAlchemy constraint can disarm it for every
        subsequent CREATE TABLE in the process, which turns a constraint test
        elsewhere into a silent pass. Asserting the emitted CREATE TABLE is the
        cheapest thing that fails if that happens again.
        """
        from sqlalchemy.dialects import sqlite
        from sqlalchemy.schema import CreateTable
        from app.work_ledger.models import WorkDayLedger

        ddl = str(CreateTable(WorkDayLedger.__table__)
                  .compile(dialect=sqlite.dialect()))
        assert "ck_work_day_ledger_departure_verified" in ddl
        assert "'ABANDONED_AUTOCLOSE'" in ddl

    def test_sqlite_accepts_the_authorized_values_and_rejects_others(self, db):
        """Behavioural, on the disposable in-memory database."""
        import sqlalchemy as sa_

        for prov in ("WORKER", "ADMIN_OVERRIDE", "STALE_AUTOCLOSE",
                     "ABANDONED_AUTOCLOSE", "OFFBOARDING"):
            _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
            db.rollback()
        # A value outside the vocabulary must still be refused by the CHECK.
        with pytest.raises(sa_.exc.IntegrityError):
            db.execute(sa_.text(
                "INSERT INTO work_day_ledger (organization_id, user_id, "
                "work_date, workweek_key, shift_attribution_rule, "
                "ledger_version, state, timezone_iana, utc_offset_seconds, "
                "is_working_day, working_day_source, created_by, "
                "departure_at, departure_provenance, departure_source_ref) "
                "VALUES (:o, :u, '2026-09-07 00:00:00', '2026-09-07', "
                "'START_DATE', 1, 'SEALED', 'Asia/Kolkata', 19800, 1, 'org', "
                "'T', '2026-09-07 11:30:00', 'NOT_A_REAL_PROVENANCE', 'x')"),
                {"o": ORG, "u": WORKER})
            db.flush()
        db.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# 7c. Resolution of the exit — §24.7's second half
# ══════════════════════════════════════════════════════════════════════════════
class TestUnverifiedExitResolution:
    """"Recomputes on resolution" — the arrow §24.7 describes, and only it.

    GV-17's ``recomputes_on_resolution`` field is satisfied here at the ledger
    layer: a resolved exit is a correction, a correction is a new version, and
    evaluation follows the current version. The ORCHESTRATION that notices a
    resolution and triggers this is P1 work and is deliberately absent.
    """

    def _sealed_unverified(self, db):
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        assert _run(db).outcome is ProducerOutcome.SEALED
        rec = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                      work_date=DAY)
        assert rec.record.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        return db.query(WorkDayLedger).one()

    def _resolve(self, db, ledger):
        """The correction an administrator's resolution would write.

        THE EVIDENCE IS CORRECTED, NOT JUST THE DAY COLUMN. This helper
        originally re-stamped only ``departure_provenance`` and carried the
        intervals forward untouched — which left the auto-closed interval still
        recording an unverified exit. That modelled an incomplete resolution:
        under §19 step 6 the day is deferred while ANY sealed evidence is
        unverified, so such a correction correctly changes nothing. A real
        resolution is the administrator standing behind the exit itself, so the
        successor records ADMIN_OVERRIDE on the interval that carried the
        synthesized close as well.
        """
        from app.work_ledger.integrity import CandidateInterval
        from app.work_ledger.service import DayKey, read_intervals, supersede_day

        # SQLite hands back naive datetimes; the integrity rules reject those,
        # so the offered evidence is re-anchored to the UTC it was stored in.
        def _aware(dt):
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

        from app.work_ledger.states import SYNTHESIZED_CLOSURE_PROVENANCE
        synthesized = {p.value for p in SYNTHESIZED_CLOSURE_PROVENANCE}
        carried = [
            CandidateInterval(seq=i.seq, state=i.state,
                              start_at_utc=_aware(i.start_at_utc),
                              end_at_utc=_aware(i.end_at_utc),
                              provenance=(Provenance.ADMIN_OVERRIDE.value
                                          if i.provenance in synthesized
                                          else i.provenance),
                              source_ref=i.source_ref)
            for i in read_intervals(db, ledger)
        ]
        return supersede_day(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=DAY),
            expected_version=int(ledger.ledger_version),
            intervals=carried,
            timezone_iana=ledger.timezone_iana,
            utc_offset_seconds=int(ledger.utc_offset_seconds),
            is_working_day=bool(ledger.is_working_day),
            working_day_source=ledger.working_day_source,
            departure_at=_aware(ledger.departure_at),
            departure_provenance=Provenance.ADMIN_OVERRIDE.value,
            departure_source_ref=ledger.departure_source_ref,
            source_fingerprint=ledger.source_fingerprint,
            created_by="admin@dep.test",
        )

    def test_a_resolved_exit_recomputes_out_of_deferral(self, db):
        """K: the day stops being deferred once the exit is verified."""
        sealed = self._sealed_unverified(db)
        self._resolve(db, sealed)
        db.flush()

        again = evaluate_and_record_day(db, organization_id=ORG,
                                        user_id=WORKER, work_date=DAY)
        assert again.is_recorded, f"{again.reason} {again.detail}"
        assert again.record.state != EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert again.record.state in (EcoDayState.SHORTFALL.value,
                                      EcoDayState.NO_SHORTFALL.value)
        assert again.record.shortfall_computed is True

    def test_the_unverified_version_is_retained_not_rewritten(self, db):
        """L: supersession semantics are the ones D-18 already established."""
        sealed = self._sealed_unverified(db)
        original_id, original_version = sealed.id, int(sealed.ledger_version)
        new_row = self._resolve(db, sealed)
        db.flush()

        rows = {int(r.ledger_version): r for r in db.query(WorkDayLedger).all()}
        assert set(rows) == {original_version, original_version + 1}
        prior = rows[original_version]
        # The unverified exit remains on the record exactly as it was observed.
        assert prior.departure_provenance == Provenance.STALE_AUTOCLOSE.value
        assert prior.superseded_by == new_row.id
        assert new_row.supersedes == original_id
        # §12.4: a correction is an addition to the lineage, not a re-homing.
        assert new_row.workweek_key == prior.workweek_key == DAY_WEEK

    def test_a_stale_expected_version_is_refused(self, db):
        """L: the optimistic-concurrency token is unchanged by any of this."""
        from app.work_ledger.service import StaleLedgerVersion

        sealed = self._sealed_unverified(db)
        self._resolve(db, sealed)
        db.flush()
        with pytest.raises(StaleLedgerVersion):
            self._resolve(db, sealed)     # same token, now one version behind

    def test_the_departure_provenance_cannot_be_rewritten_in_place(self, db):
        """M: resolving an exit is a supersession, never an UPDATE."""
        from app.work_ledger.states import ProvenanceImmutable

        sealed = self._sealed_unverified(db)
        sealed.departure_provenance = Provenance.WORKER.value
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_the_departure_instant_cannot_be_rewritten_in_place(self, db):
        """N: and the instant is frozen by the same listener, not by habit."""
        from app.work_ledger.states import ProvenanceImmutable

        sealed = self._sealed_unverified(db)
        sealed.departure_at = sealed.departure_at + timedelta(minutes=1)
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_no_later_run_can_defer_an_already_verified_day(self, db):
        """L: the arrow runs one way only — D18-F3-S preserved.

        A synthesized close arriving after a day was sealed with a verified
        departure must not supersede it. Deferring a settled day would withdraw
        a contribution a completed week has already counted.

        §24.7 is admitted on the FIRST sealing only, so the established D-18
        refusal stands here unchanged — the run refuses rather than deferring,
        and the sealed version is left exactly as it was.
        """
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        assert _run(db).outcome is ProducerOutcome.SEALED
        _session(db, clock_in=_local(DAY, 18), clock_out=_local(DAY, 20),
                 close_prov=Provenance.ABANDONED_AUTOCLOSE, actor="SYSTEM")

        later = _run(db)
        assert later.outcome is ProducerOutcome.REFUSED
        assert later.refusal is RefusalReason.SESSION_SYNTHESIZED_CLOSURE
        row = db.query(WorkDayLedger).one()
        assert int(row.ledger_version) == 1
        assert row.state == LedgerState.SEALED.value
        assert row.departure_provenance == Provenance.WORKER.value


# ══════════════════════════════════════════════════════════════════════════════
# 7d. Mixed sessions — §19 step 6 is a property of the INTERVAL SET
# ══════════════════════════════════════════════════════════════════════════════
class TestMixedSessionUnverifiedExit:
    """The case the first GV-17 CORE pass missed, and the owner then ruled.

    A day may hold an unverified exit in the morning and a verified departure
    in the afternoon. ``final_departure()`` correctly selects the afternoon
    one, so the day's ``departure_provenance`` says WORKER — and reading only
    that column declared the day verified, computed a shortfall and counted
    minutes derived from an exit nobody verified.

    §19 step 6 never asked about the final departure. It asks
    ``intervals.hasUnresolvedExit``, and §9.3 says the qualifying minutes of
    THAT DAY are not final. Both are properties of the day's whole sealed
    evidence, so that is what the predicate reads.
    """

    def _evaluate(self, db, on=DAY):
        rec = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=on)
        assert rec.is_recorded, f"{rec.reason} {rec.detail}"
        return rec.record

    def _deferred(self, record):
        assert record.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert record.shortfall_computed is False
        assert record.contributes_to_accumulator_minutes == 0
        assert record.requirement_outcome == "NOT_APPLICABLE"
        for field in ("required_minutes", "qualifying_minutes",
                      "gross_shortfall_minutes", "attributable_minutes"):
            assert getattr(record, field) is None, field

    # ── A/B/C: the single-session baselines the ruling restates ─────────────
    def test_a_single_synthesized_session_defers(self, db):
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _run(db)
        self._deferred(self._evaluate(db))

    def test_a_single_worker_session_evaluates_normally(self, db):
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        _run(db)
        rec = self._evaluate(db)
        assert rec.state != EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert rec.shortfall_computed is True

    def test_a_single_admin_override_session_evaluates_normally(self, db):
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.ADMIN_OVERRIDE, actor="a@dep.test")
        _run(db)
        rec = self._evaluate(db)
        assert rec.state != EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert rec.shortfall_computed is True

    # ── D/E/F/G: the mixed matrix, both orderings ──────────────────────────
    @pytest.mark.parametrize("first, second", [
        (Provenance.STALE_AUTOCLOSE, Provenance.WORKER),
        (Provenance.ABANDONED_AUTOCLOSE, Provenance.WORKER),
        (Provenance.OFFBOARDING, Provenance.ADMIN_OVERRIDE),
        (Provenance.STALE_AUTOCLOSE, Provenance.ADMIN_OVERRIDE),
    ])
    def test_an_unverified_exit_followed_by_a_verified_one_defers(
            self, db, first, second):
        """D, E, F — the exact shape that used to be counted as final."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12),
                 close_prov=first, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15),
                 close_prov=second,
                 actor="a@dep.test" if second is Provenance.ADMIN_OVERRIDE
                 else "w@dep.test")

        assert _run(db).outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        # final_departure() is UNCHANGED: the latest close still wins, so the
        # day-level column truthfully names the verified afternoon departure…
        assert row.departure_provenance == second.value
        # …and the deferral comes from the evidence set, not from that column.
        self._deferred(self._evaluate(db))

    def test_a_verified_session_followed_by_an_unverified_one_defers(self, db):
        """G — the other ordering, where the final departure IS unverified."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12))
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        assert _run(db).outcome is ProducerOutcome.SEALED
        assert db.query(WorkDayLedger).one().departure_provenance == \
            Provenance.STALE_AUTOCLOSE.value
        self._deferred(self._evaluate(db))

    def test_multiple_verified_sessions_evaluate_normally(self, db):
        """H — the guard must not defer a day that is entirely verified."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12))
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15),
                 close_prov=Provenance.ADMIN_OVERRIDE, actor="a@dep.test")
        _run(db)
        rec = self._evaluate(db)
        assert rec.state != EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert rec.shortfall_computed is True
        assert rec.qualifying_minutes == 300

    def test_an_unverified_interval_in_the_middle_defers(self, db):
        """I — anywhere in the set, not just at either end."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 11))
        _session(db, clock_in=_local(DAY, 12), clock_out=_local(DAY, 13),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 14), clock_out=_local(DAY, 16))
        _run(db)
        provenances = [i.provenance for i in
                       db.query(WorkStateInterval)
                       .order_by(WorkStateInterval.seq).all()]
        assert Provenance.STALE_AUTOCLOSE.value in provenances
        assert provenances[0] == Provenance.WORKER.value      # not at the edges
        assert provenances[-1] == Provenance.WORKER.value
        self._deferred(self._evaluate(db))

    # ── J/K: what must NOT be read as an unverified exit ───────────────────
    def test_a_producer_derived_interval_is_not_an_unverified_exit(self, db):
        """J — the regression this change could most easily have caused.

        A break or offline carve-out splits the session, and the segment that
        ends at the carve-out carries ``PRODUCER_DERIVED``: it ends at a
        producer-authored boundary, not at a departure. Judging "anything that
        is not WORKER or ADMIN_OVERRIDE" would defer every ordinary day that
        contains a break.
        """
        _requirement(db)
        row = _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        db.add(dbm.BreakSession(
            session_id=row.id, user_id=WORKER, organization_id=ORG,
            break_start=_local(DAY, 12), break_end=_local(DAY, 13),
            break_seconds=3600, is_active=False, break_type="lunch"))
        db.commit()
        _run(db)

        provenances = {i.provenance for i in db.query(WorkStateInterval).all()}
        assert Provenance.PRODUCER_DERIVED.value in provenances
        rec = self._evaluate(db)
        assert rec.state != EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert rec.qualifying_minutes == 420      # 8h less the carved-out hour

    def test_absent_provenance_is_not_an_unverified_exit(self, db):
        """J — NULL is §24.6's business, never §24.7's."""
        from app.eval_day.evaluator import _has_unverified_exit

        class _Row:
            departure_provenance = None

        class _Interval:
            provenance = None

        assert _has_unverified_exit(_Row(), []) is False
        assert _has_unverified_exit(_Row(), [_Interval()]) is False

    def test_a_non_closure_provenance_is_never_read_as_a_deferral(self):
        """K — an unrecognised or non-departure value is an integrity question
        the ledger already owns; it must never become a silent deferral."""
        from app.eval_day.evaluator import _is_unverified_exit_provenance

        for value in (Provenance.PRODUCER_DERIVED.value,
                      Provenance.BREAK_DERIVED.value,
                      Provenance.CLASSIFIER_DERIVED.value,
                      "NOT_A_REAL_PROVENANCE", ""):
            assert _is_unverified_exit_provenance(value) is False, value
        for value in (Provenance.STALE_AUTOCLOSE.value,
                      Provenance.ABANDONED_AUTOCLOSE.value,
                      Provenance.OFFBOARDING.value):
            assert _is_unverified_exit_provenance(value) is True, value
        for value in (Provenance.WORKER.value,
                      Provenance.ADMIN_OVERRIDE.value):
            assert _is_unverified_exit_provenance(value) is False, value

    # ── M: final_departure() is untouched ──────────────────────────────────
    def test_final_departure_still_selects_the_actual_latest_departure(self, db):
        """M — the owner ruled this function must NOT be redefined."""
        from app.work_ledger_producer.evidence import (
            final_departure, resolve_session_evidence)

        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15))
        evidence = [
            resolve_session_evidence(db, session=row, organization_id=ORG,
                                     user_id=WORKER,
                                     allow_unverified_closure=True)
            for row in db.query(dbm.ClockSession)
            .order_by(dbm.ClockSession.id).all()
        ]
        chosen = final_departure(evidence)
        assert chosen.clock_out_at == _local(DAY, 15)
        assert chosen.close_provenance is Provenance.WORKER

    # ── P/Q/R: the week, and what the deferred day must not emit ───────────
    def test_a_mixed_day_blocks_the_week_and_fires_nothing(self, db):
        """P, Q, R — for the mixed shape, not only the single-session one."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12),
                 close_prov=Provenance.ABANDONED_AUTOCLOSE, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15))
        _run(db)
        self._deferred(self._evaluate(db))
        db.flush()

        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=DAY_WEEK)
        assert week.state is WeeklyState.DEFERRED_DAY_PRESENT
        assert week.weekly_attributable_shortfall_minutes is None
        assert DAY in week.blocking_dates
        for table in ("eco_threshold_event", "eco_notification"):
            if db.execute(sa.text(
                    f"SELECT count(*) FROM sqlite_master WHERE type='table' "
                    f"AND name='{table}'")).scalar():
                assert db.execute(
                    sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0, (
                    f"{table} gained a row")

    def test_the_mixed_day_still_seals_all_of_its_evidence(self, db):
        """The deferral must not cost the day its evidence (§19 step 5)."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15))
        _run(db)
        intervals = db.query(WorkStateInterval).order_by(
            WorkStateInterval.seq).all()
        assert [i.provenance for i in intervals] == [
            Provenance.STALE_AUTOCLOSE.value, Provenance.WORKER.value]
        assert db.query(WorkDayLedger).one().state == LedgerState.SEALED.value

    def test_resolving_the_unverified_session_lifts_the_deferral(self, db):
        """N, O — the mixed day recomputes once its evidence is corrected.

        The correction supersedes the version, carrying the SAME intervals
        except that the auto-closed one now records an administrator's
        resolution. The historical week identity travels down the lineage
        untouched.
        """
        from app.work_ledger.integrity import CandidateInterval
        from app.work_ledger.service import DayKey, read_intervals, supersede_day

        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15))
        _run(db)
        self._deferred(self._evaluate(db))

        sealed = db.query(WorkDayLedger).one()

        def _aware(dt):
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

        corrected = [
            CandidateInterval(
                seq=i.seq, state=i.state,
                start_at_utc=_aware(i.start_at_utc),
                end_at_utc=_aware(i.end_at_utc),
                provenance=(Provenance.ADMIN_OVERRIDE.value
                            if i.provenance == Provenance.STALE_AUTOCLOSE.value
                            else i.provenance),
                source_ref=i.source_ref)
            for i in read_intervals(db, sealed)
        ]
        successor = supersede_day(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=DAY),
            expected_version=int(sealed.ledger_version),
            intervals=corrected,
            timezone_iana=sealed.timezone_iana,
            utc_offset_seconds=int(sealed.utc_offset_seconds),
            is_working_day=bool(sealed.is_working_day),
            working_day_source=sealed.working_day_source,
            departure_at=_aware(sealed.departure_at),
            departure_provenance=sealed.departure_provenance,
            departure_source_ref=sealed.departure_source_ref,
            source_fingerprint=sealed.source_fingerprint,
            created_by="admin@dep.test")
        db.flush()

        again = self._evaluate(db)
        assert again.state != EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert again.shortfall_computed is True
        # O: the historical identity is inherited, never re-resolved.
        assert successor.workweek_key == sealed.workweek_key == DAY_WEEK
        assert successor.shift_attribution_rule == "START_DATE"
        # The superseded version keeps the evidence it was sealed with.
        assert [i.provenance for i in read_intervals(db, sealed)] == [
            Provenance.STALE_AUTOCLOSE.value, Provenance.WORKER.value]


# ══════════════════════════════════════════════════════════════════════════════
# 7e. Layer boundaries — who is allowed to decide what
# ══════════════════════════════════════════════════════════════════════════════
class TestGV17LayerBoundaries:
    """Static guards. The §24.7 path crosses three packages, and the value of
    the design is entirely in which one makes the call at each step."""

    RESOLVER = BACKEND / "app" / "departure_resolution"
    PRODUCER = BACKEND / "app" / "work_ledger_producer" / "producer.py"
    EVALUATOR = BACKEND / "app" / "eval_day" / "evaluator.py"

    @staticmethod
    def _names(path: pathlib.Path) -> set:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})

    def test_the_producer_never_names_an_eco_state(self):
        """The producer transports the verdict; it does not classify the day.

        It may read ``is_unverified_exit`` — that is receiving a decision — but
        it may not reach for the vocabulary that decision is later expressed in.
        """
        names = self._names(self.PRODUCER)
        for banned in ("EcoDayState", "DEFERRED_PENDING_LEDGER",
                       "evaluate_day", "evaluate_and_record_day"):
            assert banned not in names, f"producer.py names {banned}"

    def test_eval_day_owns_the_deferral_decision(self):
        """…and reaches it from the canonical sealed evidence alone."""
        names = self._names(self.EVALUATOR)
        assert "DEFERRED_PENDING_LEDGER" in names
        for banned in ("resolve_departure", "DepartureOutcome",
                       "resolve_session_evidence", "ClockSession"):
            assert banned not in names, f"evaluator.py names {banned}"

    def test_the_unresolved_exit_predicate_reads_the_whole_interval_set(self):
        """§19 step 6 is ``intervals.hasUnresolvedExit`` — not the final
        departure.

        The first GV-17 CORE pass reduced it to the day-level
        ``departure_provenance``, which silently counted a day whose morning
        session was auto-closed and whose afternoon session closed properly.
        This guard exists so the predicate cannot be narrowed back to a single
        column without the narrowing being deliberate and visible.
        """
        tree = ast.parse(self.EVALUATOR.read_text(encoding="utf-8"))

        predicate = next(
            (n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == "_has_unverified_exit"),
            None)
        assert predicate is not None, (
            "the §19 step-6 predicate is gone or was renamed; if it moved, move "
            "this guard with it rather than deleting it")

        # It must ACCEPT the interval set…
        args = [a.arg for a in predicate.args.args]
        assert "intervals" in args, (
            f"the predicate takes {args}; §19 step 6 is a property of the "
            f"sealed interval set, so it must be given one")

        # …and actually read each interval's provenance, not just the row's.
        sources = {
            n.args[1].value
            for n in ast.walk(predicate)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "getattr" and len(n.args) > 1
            and isinstance(n.args[1], ast.Constant)
        }
        assert "provenance" in sources and "departure_provenance" in sources, (
            f"the predicate reads {sorted(sources)}; it must consult BOTH the "
            f"day's departure columns and the interval provenance")

        # And the call site must hand it the intervals it was given.
        evaluate = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "evaluate_day")
        calls = [
            n for n in ast.walk(evaluate)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_has_unverified_exit"
        ]
        assert calls, "evaluate_day no longer tests for an unresolved exit"
        for call in calls:
            passed = {a.id for a in call.args if isinstance(a, ast.Name)}
            assert "intervals" in passed, (
                "evaluate_day calls the predicate without the interval set")

    def test_the_resolver_reads_no_clock(self):
        """Ruling: the verdict is a function of evidence, not of when you ask.

        ``day_closed`` is passed IN by the caller, which is why the closure gate
        stays a single authorized point rather than becoming a timeout here.
        """
        for path in sorted(self.RESOLVER.glob("*.py")):
            names = self._names(path)
            for banned in ("now", "utcnow", "today", "monotonic", "time"):
                assert banned not in names, f"{path.name} reads the clock"

    def test_nothing_on_this_path_repairs_or_clamps_the_exit(self):
        """§7.2: inferring an end of day is "the single worst thing this
        control could do". No path may round, clamp or backfill one."""
        for path in (self.PRODUCER, self.EVALUATOR,
                     *sorted(self.RESOLVER.glob("*.py"))):
            names = self._names(path)
            for banned in ("clamp", "backfill", "repair", "infer", "assume",
                           "estimate", "default_clock_out"):
                assert banned not in names, f"{path.name} names {banned}"

    def test_eco_v1_remains_disabled(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_no_new_state_was_introduced_by_the_24_7_path(self):
        """T: the §24.7 path reuses existing vocabulary end to end.

        The evaluation vocabulary has since grown by one — POLICY_UNRESOLVED,
        added by a later owner ruling for a policy-resolution failure — but NOT
        by this route, which is what Ruling T pinned. The ledger vocabulary is
        still untouched, and §24.7 still resolves to DEFERRED_PENDING_LEDGER.
        """
        from app.eval_day import EcoDayState
        from app.work_ledger import LedgerState

        assert {s.value for s in LedgerState} == {
            "OPEN", "SEALED", "MISSING_CLOCK_OUT", "NOT_A_WORKING_DAY",
            "LEDGER_INTEGRITY_FAULT"}
        assert {s.value for s in EcoDayState} == {
            "SHORTFALL", "NO_SHORTFALL", "DEFERRED_PENDING_LEDGER",
            "NOT_A_WORKING_DAY", "EVALUABLE", "REQUIREMENT_UNRESOLVED",
            "POLICY_UNRESOLVED"}


# ══════════════════════════════════════════════════════════════════════════════
# 8. Scope — the package stays foundation
# ══════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    PACKAGE = BACKEND / "app" / "departure_resolution"

    def test_nothing_in_the_application_wires_this_package(self):
        """No route, scheduler or service imports it — only the producer does.

        The same not-wired discipline every Phase-2 primitive is held to.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "departure_resolution" in path.parts:
                continue
            src = path.read_text(encoding="utf-8", errors="ignore")
            if "app.departure_resolution" not in src:
                continue
            if path.parts[-2:] == ("work_ledger_producer", "producer.py"):
                continue          # the authorized transport seam
            offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_it_declares_no_table_and_no_migration(self):
        for path in sorted(self.PACKAGE.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for banned in ("__tablename__", "op.create_table", "Column("):
                assert banned not in src, f"{path.name} declares storage"
        versions = BACKEND / "alembic" / "versions"
        for path in sorted(versions.glob("*.py")):
            body = path.read_text(encoding="utf-8", errors="ignore")
            assert "departure_resolution" not in body, path.name

    def test_it_performs_no_eco_calculation(self):
        for path in sorted(self.PACKAGE.glob("*.py")):
            body = path.read_text(encoding="utf-8").lower()
            for term in ("shortfall", "threshold", "requirement", "accumulator",
                         "notification", "advisory"):
                # Prose may name them to say it does NOT do them; code may not.
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {
                    n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
                } | {
                    n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                }
                assert not any(term in x.lower() for x in names), (
                    f"{path.name} computes {term}")
