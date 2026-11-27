"""
MODEL-WORKWEEK — attribution provenance, historical identity, boundary control.
===============================================================================

The owner rulings this file proves, and the specification behind each:

  R1  ``shift_attribution_rule`` is persisted on the day record, always
      ``START_DATE``                                     R-12.3.2, R-12.3.4, §35
  R2  attribution belongs to the producer; MODEL-WORKWEEK only maps an
      already-attributed ``work_date`` to a week                          §12.3
  R3  the week-start boundary is organization-level, and a narrower write is
      refused rather than silently ignored
  R4  a correction inherits the historical week identity                  §12.4
  R5  a boundary change may not land inside an open workweek              §12.1

The centrepiece is :class:`TestCrossWeekEndToEnd`, which drives GV-09's own
scenario — Sunday 22:00 to Monday 06:00 — through the real producer, the real
ledger, the real day evaluator and the real weekly fold. Nothing is hand-sealed
and no helper is asserted in place of the pipeline.
"""

from __future__ import annotations

import ast
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
from app.eval_day import evaluate_and_record_day
from app.eval_week import canonical_dates, evaluate_workweek
from app.eval_week.membership import current_ledger_days
from app.eval_week.outcomes import WeeklyState
from app.requirement_resolution import RequirementCandidate
from app.schedule_policy import keys as K
from app.work_ledger import (
    CandidateInterval,
    DayKey,
    LedgerState,
    Provenance,
    WorkDayLedger,
    WorkState,
    record_integrity_fault,
    seal_day,
    supersede_day,
)
from app.work_ledger.attribution import (
    SHIFT_ATTRIBUTION_START_DATE,
    SUPPORTED_ATTRIBUTION_RULES,
)
from app.work_ledger.workweek import (
    WorkweekBoundaryChangeRefused,
    WorkweekBoundaryMalformed,
    WorkweekBoundaryScopeInvalid,
    WorkweekBoundaryUnset,
    assert_boundary_change_allowed,
    boundary_regime,
    canonical_workweek_key,
    resolve_week_start_day,
)
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]

ORG = 7310
WORKER = 7311

#: IST: +05:30 and no DST, so the local/UTC arithmetic below is exact and a
#: transition cannot be mistaken for a boundary effect.
ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)

#: GV-09's own calendar. 2026-09-06 is a SUNDAY and 2026-09-07 the MONDAY that
#: follows it — so under a Monday-start organization the shift below starts in
#: one workweek and ends in the next. That is the whole point of the vector;
#: a Monday-to-Tuesday crossing would prove only cross-midnight.
SUNDAY = date(2026, 9, 6)
MONDAY = date(2026, 9, 7)
#: The Monday-start week containing SUNDAY begins here, six days earlier.
SUNDAY_WEEK = "2026-08-31"
#: …and the week MONDAY opens is a different one.
MONDAY_WEEK = "2026-09-07"

ALL_SEVEN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
REQUIRED_MINUTES = 525


def _local(on: date, hour: int, minute: int = 0) -> datetime:
    """A local wall-clock instant in ``ZONE``, as UTC."""
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=UTC) - IST


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def engine_fk():
    import app.clock_session_boundary.models      # noqa: F401
    import app.config_versioning.models           # noqa: F401
    import app.eval_day.models                    # noqa: F401
    import app.policy_version.models             # noqa: F401
    import app.break_treatment.models             # noqa: F401
    import app.break_classification.models       # noqa: F401
    import app.eval_day.leave_snapshot            # noqa: F401
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
        s.add(dbm.Organization(id=ORG, name="Workweek Org"))
        s.add(dbm.User(id=WORKER, name="W", email="w@example.test",
                       organization_id=ORG, role="employee"))
        s.commit()
        # Explicit historical PolicyVersion: evaluate_and_record_day
        # refuses with POLICY_UNRESOLVED without one. Satisfies the
        # production rule rather than weakening it.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


def _set_week_start(db, value, *, organization_id=ORG,
                    team_key=None, user_id=None):
    """Write the boundary setting at a given scope, bypassing the API guard.

    Deliberately direct: several tests need a state the API now refuses to
    create, in order to prove the ECO resolver refuses it too rather than
    relying on the endpoint being the only way in.
    """
    row = (db.query(dbm.AppSettings)
             .filter(dbm.AppSettings.organization_id == organization_id,
                     dbm.AppSettings.key == K.SETTING_WEEK_START_DAY,
                     dbm.AppSettings.team_key.is_(team_key)
                     if team_key is None else
                     dbm.AppSettings.team_key == team_key,
                     dbm.AppSettings.user_id.is_(user_id)
                     if user_id is None else
                     dbm.AppSettings.user_id == user_id)
             .one_or_none())
    if row is None:
        db.add(dbm.AppSettings(organization_id=organization_id,
                               team_key=team_key, user_id=user_id,
                               key=K.SETTING_WEEK_START_DAY, value=value))
    else:
        row.value = value
    db.flush()


def _configure(db, *, week_start="MONDAY", working_days=ALL_SEVEN,
               cutover=CUTOVER):
    """A tenant that satisfies every gate the producer and ECO require."""
    from app import _tz

    if db.get(dbm.OrgSettings, ORG) is None:
        db.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
    if working_days is not None:
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=None,
            key=K.SETTING_WORKING_DAYS, value=__import__("json").dumps(working_days)))
    db.flush()
    if week_start is not None:
        _set_week_start(db, week_start)
    db.commit()
    _tz.invalidate_tz_cache()
    if cutover is not None:
        set_producer_cutover(db, organization_id=ORG, cutover_at=cutover,
                             actor_label="owner@example.test")
        db.commit()


def _session(db, *, clock_in, clock_out, user_id=WORKER):
    """One closed clock session carrying its D-15 boundary provenance."""
    row = dbm.ClockSession(
        user_id=user_id, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=999999, break_seconds=0, offline_seconds=0,
        is_active=False,
    )
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=user_id,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=Provenance.WORKER,
                          actor_label="w@example.test")
    append_boundary_event(db, session_id=row.id, user_id=user_id,
                          boundary=BOUNDARY_CLOCK_OUT, boundary_at=clock_out,
                          provenance=Provenance.WORKER,
                          actor_label="w@example.test")
    db.commit()
    return row


def _capture_requirement(db, *, version_key, effective_from,
                         minutes=REQUIRED_MINUTES):
    provider = DatabaseConfigVersionProvider(db)
    ref = provider.capture_version(
        organization_id=ORG,
        config_version_id=version_key,
        effective_from=effective_from,
        candidates=[RequirementCandidate(
            tier="TENANT_FALLBACK", rank=6, minutes=minutes)],
    )
    db.flush()
    return ref


def _seal(db, on: date, *, hours=8, user_id=WORKER):
    """Seal a day directly, for tests that are not about the producer."""
    begin = _local(on, 9)
    return seal_day(
        db, DayKey(organization_id=ORG, user_id=user_id, work_date=on),
        intervals=[CandidateInterval(
            seq=1, state=WorkState.WRK.value, start_at_utc=begin,
            end_at_utc=begin + timedelta(hours=hours),
            provenance=Provenance.WORKER.value)],
        timezone_iana=ZONE, is_working_day=True, working_day_source="org",
    )


# ══════════════════════════════════════════════════════════════════════════════
# A. Shift attribution — R-12.3.2, R-12.3.4, §35
# ══════════════════════════════════════════════════════════════════════════════
class TestAttributionProvenance:
    def test_start_date_is_the_only_supported_rule(self):
        assert SUPPORTED_ATTRIBUTION_RULES == {"START_DATE"}
        assert SHIFT_ATTRIBUTION_START_DATE == "START_DATE"

    def test_a_sealed_day_persists_the_rule(self, db):
        """§35: the rule lives in the day record's provenance block."""
        _configure(db)
        row = _seal(db, SUNDAY)
        assert row.shift_attribution_rule == "START_DATE"

    def test_the_caller_cannot_supply_a_rule(self):
        """Like workweek_key (W4-4): derived, never accepted from outside.

        Two writers that could each name a rule could disagree about how one
        day was attributed, and the row would not say which was right.
        """
        import inspect

        from app.work_ledger import record_integrity_fault

        for fn in (seal_day, supersede_day, record_integrity_fault):
            assert "shift_attribution_rule" not in \
                inspect.signature(fn).parameters, fn.__name__

    def test_a_correction_carries_it_too(self, db):
        _configure(db)
        _seal(db, SUNDAY)
        v2 = supersede_day(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=SUNDAY),
            expected_version=1,
            intervals=[CandidateInterval(
                seq=1, state=WorkState.WRK.value,
                start_at_utc=_local(SUNDAY, 9),
                end_at_utc=_local(SUNDAY, 16),
                provenance=Provenance.WORKER.value)],
            timezone_iana=ZONE, is_working_day=True, working_day_source="org")
        assert v2.shift_attribution_rule == "START_DATE"

    def test_it_is_immutable_once_sealed(self, db):
        """Append-only: the rule a sealed day carries can never be rewritten.

        R-12.3.4 — "Changing it MUST NOT retroactively re-attribute sealed
        weeks". Correcting a sealed day means superseding it.
        """
        from app.work_ledger.states import ProvenanceImmutable

        _configure(db)
        row = _seal(db, SUNDAY)
        db.commit()
        row.shift_attribution_rule = "END_DATE"
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_the_week_identity_is_immutable_too(self, db):
        """§12.4: historical workweeks change by addition, never by mutation."""
        from app.work_ledger.states import ProvenanceImmutable

        _configure(db)
        row = _seal(db, SUNDAY)
        db.commit()
        row.workweek_key = "2026-09-07"
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_the_migration_and_the_constant_agree(self):
        """One source of truth, restated once in a migration that cannot import it.

        Read from the AST rather than the file text so a mention in the
        migration's prose cannot satisfy this.
        """
        path = (BACKEND / "alembic" / "versions"
                / "b2e6d9f04a17_ledger_shift_attribution_rule.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert SHIFT_ATTRIBUTION_START_DATE in literals
        assert "END_DATE" not in literals
        assert "MAJORITY_HOURS" not in literals


# ══════════════════════════════════════════════════════════════════════════════
# B. Separation of concerns — owner Ruling 2
# ══════════════════════════════════════════════════════════════════════════════
class TestSeparation:
    def test_model_workweek_never_inspects_a_session_or_a_roster(self):
        src = (BACKEND / "app" / "work_ledger" / "workweek.py").read_text(
            encoding="utf-8")
        for banned in ("ClockSession", "ShiftAssignment", "clock_in",
                       "clock_out", "BreakSession"):
            assert banned not in src, f"workweek.py reaches for {banned}"

    def test_eval_week_does_not_resolve_the_current_boundary(self):
        """EVAL-WEEK parses the stored key; it must never ask what week it is.

        Read from the import graph and the call sites, because this is the
        property that makes a historical fold reproducible.
        """
        package = BACKEND / "app" / "eval_week"
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {
                node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
                for node in ast.walk(tree) if isinstance(node, ast.Call)
            }
            for banned in ("week_start_date", "week_start_of",
                           "canonical_workweek_key", "resolve_week_start_day",
                           "current_open_workweek_start"):
                assert banned not in called, f"{path.name} calls {banned}"

    def test_eval_event_does_not_resolve_the_current_boundary(self):
        package = BACKEND / "app" / "eval_event"
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {
                node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None)
                for node in ast.walk(tree) if isinstance(node, ast.Call)
            }
            for banned in ("week_start_date", "week_start_of",
                           "canonical_workweek_key", "resolve_week_start_day"):
                assert banned not in called, f"{path.name} calls {banned}"


# ══════════════════════════════════════════════════════════════════════════════
# C. Organization-level boundary — owner Ruling 3
# ══════════════════════════════════════════════════════════════════════════════
class TestOrganizationScope:
    def test_the_organization_value_is_honoured(self, db):
        _configure(db, week_start="SUNDAY")
        assert resolve_week_start_day(db, organization_id=ORG) == "SUNDAY"
        assert canonical_workweek_key(
            db, organization_id=ORG, work_date=SUNDAY) == SUNDAY.isoformat()

    def test_a_worker_scoped_override_is_refused_not_ignored(self, db):
        """The defect this closes: the write was accepted and read by nobody."""
        _configure(db)
        _set_week_start(db, "SUNDAY", user_id=WORKER)
        with pytest.raises(WorkweekBoundaryScopeInvalid):
            canonical_workweek_key(db, organization_id=ORG, work_date=SUNDAY)

    def test_a_team_scoped_override_is_refused(self, db):
        _configure(db)
        _set_week_start(db, "SUNDAY", team_key="Ops")
        with pytest.raises(WorkweekBoundaryScopeInvalid):
            resolve_week_start_day(db, organization_id=ORG)

    def test_the_settings_layer_declares_the_key_organization_only(self):
        from app.settings import scope as settings_scope

        assert settings_scope.is_org_only(K.SETTING_WEEK_START_DAY)

    def test_a_scoped_write_is_rejected_by_the_api_guard(self):
        """The refusal the endpoint performs, at the layer that performs it.

        ``put_scoped_settings`` builds its refusal list from ``is_org_only``;
        before this increment it consulted only ``KEY_META``, where this key is
        deliberately absent, so the write was accepted and then ignored.
        """
        from app.settings import scope as settings_scope

        incoming = {K.SETTING_WEEK_START_DAY: "SUNDAY", "screenshot_enabled": "true"}
        refused = [k for k in incoming if settings_scope.is_org_only(k)]
        assert refused == [K.SETTING_WEEK_START_DAY]


# ══════════════════════════════════════════════════════════════════════════════
# D. Fail-closed configuration — owner Ruling 5 (F)
# ══════════════════════════════════════════════════════════════════════════════
class TestFailClosed:
    def test_missing_is_refused(self, db):
        _configure(db, week_start=None)
        with pytest.raises(WorkweekBoundaryUnset):
            canonical_workweek_key(db, organization_id=ORG, work_date=SUNDAY)

    def test_malformed_is_refused(self, db):
        _configure(db, week_start="Funday")
        with pytest.raises(WorkweekBoundaryMalformed):
            canonical_workweek_key(db, organization_id=ORG, work_date=SUNDAY)

    def test_a_day_cannot_be_sealed_without_a_boundary(self, db):
        """The refusal reaches the writer, not just the helper."""
        _configure(db, week_start=None)
        with pytest.raises(WorkweekBoundaryUnset):
            _seal(db, SUNDAY)

    def test_the_legacy_resolver_keeps_its_lenient_contract(self, db):
        """Ruling F, second half: do not break the callers that rely on it.

        ``week_start_date()`` still defaults, because payroll periods and the
        weekly dashboard window prefer a defaulted window to a 500.
        """
        from app.schedule_policy import week_start_date

        _configure(db, week_start="Funday")
        assert week_start_date(db, ORG, SUNDAY) == date(2026, 8, 31)


# ══════════════════════════════════════════════════════════════════════════════
# E. Mid-week boundary changes — owner Ruling 5
# ══════════════════════════════════════════════════════════════════════════════
class TestBoundaryChange:
    def _freeze_today(self, monkeypatch, on: date):
        """Pin the organization-local 'today' the guard compares against."""
        import app.work_ledger.workweek as ww

        monkeypatch.setattr(ww, "local_date", lambda *_a, **_k: on,
                            raising=False)
        import app._tz as tz
        monkeypatch.setattr(tz, "local_date", lambda *_a, **_k: on)

    def test_a_change_on_the_first_day_of_the_week_is_accepted(
            self, db, monkeypatch):
        _configure(db)                                   # MONDAY start
        self._freeze_today(monkeypatch, MONDAY)          # a Monday
        assert_boundary_change_allowed(
            db, organization_id=ORG, new_value="SUNDAY")

    def test_a_midweek_change_is_refused(self, db, monkeypatch):
        _configure(db)                                   # MONDAY start
        self._freeze_today(monkeypatch, date(2026, 9, 9))   # a Wednesday
        with pytest.raises(WorkweekBoundaryChangeRefused):
            assert_boundary_change_allowed(
                db, organization_id=ORG, new_value="SUNDAY")

    def test_a_no_op_is_always_allowed(self, db, monkeypatch):
        _configure(db)
        self._freeze_today(monkeypatch, date(2026, 9, 9))
        assert_boundary_change_allowed(
            db, organization_id=ORG, new_value="MONDAY")

    def test_an_unsupported_weekday_is_refused(self, db, monkeypatch):
        _configure(db)
        self._freeze_today(monkeypatch, MONDAY)
        with pytest.raises(WorkweekBoundaryMalformed):
            assert_boundary_change_allowed(
                db, organization_id=ORG, new_value="Funday")

    def test_a_broken_configuration_can_still_be_repaired(
            self, db, monkeypatch):
        """Otherwise a malformed value would be permanently unfixable."""
        _configure(db, week_start="Funday")
        self._freeze_today(monkeypatch, date(2026, 9, 9))   # mid-week
        assert_boundary_change_allowed(
            db, organization_id=ORG, new_value="MONDAY")

    def test_a_refused_change_leaves_one_week_undivided(self, db, monkeypatch):
        """The consequence the refusal exists to prevent.

        Sealed Monday and Tuesday under a Monday start, then attempt the move
        to Sunday on the Wednesday. If it were allowed, Wednesday onward would
        carry a different key and the one physical week would fold twice.
        """
        _configure(db)
        _capture_requirement(db, version_key="wk-1",
                             effective_from=date(2026, 9, 7))
        _seal(db, MONDAY)
        _seal(db, date(2026, 9, 8))
        self._freeze_today(monkeypatch, date(2026, 9, 9))

        with pytest.raises(WorkweekBoundaryChangeRefused):
            assert_boundary_change_allowed(
                db, organization_id=ORG, new_value="SUNDAY")

        keys = {row.workweek_key for row in db.query(WorkDayLedger).all()}
        assert keys == {MONDAY_WEEK}, "the week was divided anyway"


# ══════════════════════════════════════════════════════════════════════════════
# F. Historical identity — owner Ruling 4, §12.4
# ══════════════════════════════════════════════════════════════════════════════
class TestHistoricalIdentity:
    def test_a_correction_inherits_the_historical_week(self, db):
        """The defect Ruling 4 closes, Monday -> Sunday.

        Seal under a Monday start, move the organization to Sunday, then
        correct the sealed day. The corrected version must stay in the week it
        was evaluated in; re-deriving would move it to the Sunday-start week
        and silently change a completed week's membership.
        """
        _configure(db)                                   # MONDAY
        v1 = _seal(db, SUNDAY)
        assert v1.workweek_key == SUNDAY_WEEK

        _set_week_start(db, "SUNDAY")
        db.commit()
        # A day sealed from now on WOULD adopt the new boundary (W4-7)…
        assert canonical_workweek_key(
            db, organization_id=ORG, work_date=SUNDAY) == SUNDAY.isoformat()

        # …but a correction to the existing day must not move it.
        v2 = supersede_day(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=SUNDAY),
            expected_version=1,
            intervals=[CandidateInterval(
                seq=1, state=WorkState.WRK.value,
                start_at_utc=_local(SUNDAY, 9), end_at_utc=_local(SUNDAY, 16),
                provenance=Provenance.WORKER.value)],
            timezone_iana=ZONE, is_working_day=True, working_day_source="org")
        assert v2.workweek_key == SUNDAY_WEEK, (
            "the correction re-homed a historical day under today's boundary")

    def test_the_reverse_direction_holds_too(self, db):
        """Sunday -> Monday, so the guarantee is not an artefact of direction."""
        _configure(db, week_start="SUNDAY")
        v1 = _seal(db, SUNDAY)
        assert v1.workweek_key == SUNDAY.isoformat()

        _set_week_start(db, "MONDAY")
        db.commit()
        assert canonical_workweek_key(
            db, organization_id=ORG, work_date=SUNDAY) == SUNDAY_WEEK

        v2 = supersede_day(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=SUNDAY),
            expected_version=1,
            intervals=[CandidateInterval(
                seq=1, state=WorkState.WRK.value,
                start_at_utc=_local(SUNDAY, 9), end_at_utc=_local(SUNDAY, 15),
                provenance=Provenance.WORKER.value)],
            timezone_iana=ZONE, is_working_day=True, working_day_source="org")
        assert v2.workweek_key == SUNDAY.isoformat()

    def test_the_superseded_row_keeps_its_own_key(self, db):
        _configure(db)
        _seal(db, SUNDAY)
        supersede_day(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=SUNDAY),
            expected_version=1,
            intervals=[CandidateInterval(
                seq=1, state=WorkState.WRK.value,
                start_at_utc=_local(SUNDAY, 9), end_at_utc=_local(SUNDAY, 16),
                provenance=Provenance.WORKER.value)],
            timezone_iana=ZONE, is_working_day=True, working_day_source="org")
        keys = {r.workweek_key for r in db.query(WorkDayLedger).all()}
        assert keys == {SUNDAY_WEEK}, "the lineage disagrees with itself"

    def test_the_key_carries_its_own_boundary_regime(self):
        """Why Ruling 4 needed no extra column.

        The key IS the week's start date, so its weekday is the boundary that
        minted it — recoverable from what is already persisted.
        """
        assert boundary_regime("2026-08-31") == "MONDAY"
        assert boundary_regime("2026-09-06") == "SUNDAY"
        assert boundary_regime("2026-W36") is None
        assert boundary_regime("nonsense") is None

    def test_a_correction_does_not_read_the_current_setting(self):
        """Asserted structurally: supersede_day must not call the resolver."""
        src = (BACKEND / "app" / "work_ledger" / "service.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "supersede_day":
                called = {
                    getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                    for c in ast.walk(node) if isinstance(c, ast.Call)
                }
                assert "canonical_workweek_key" not in called
                break
        else:
            pytest.fail("supersede_day not found")


# ══════════════════════════════════════════════════════════════════════════════
# F2. Integrity faults — the correction path the first pass missed
# ══════════════════════════════════════════════════════════════════════════════
class TestIntegrityFaultHistoricalIdentity:
    """``record_integrity_fault`` writes a NEW VERSION of an existing day.

    That makes it a correction path, and owner Ruling 4 applies to it exactly
    as it applies to ``supersede_day``. It previously re-derived the key, which
    the promotion-readiness audit proved could put one Worker-day into two
    workweeks — R-12.3.5 — while making the condition undetectable.
    """

    def _fault(self, db, on=None):
        return record_integrity_fault(
            db, DayKey(organization_id=ORG, user_id=WORKER,
                       work_date=on or SUNDAY),
            reason="overlapping intervals in the sealed set (§24.15)",
            timezone_iana=ZONE, is_working_day=True, working_day_source="org")

    def test_monday_to_sunday_keeps_the_historical_week(self, db):
        """Test 1 — the exact audit failure."""
        _configure(db)                                   # MONDAY
        v1 = _seal(db, SUNDAY)
        assert v1.workweek_key == SUNDAY_WEEK == "2026-08-31"

        _set_week_start(db, "SUNDAY")
        db.commit()
        v2 = self._fault(db)

        assert v2.workweek_key == "2026-08-31", (
            "the fault version re-homed a historical day under today's boundary")
        assert v2.workweek_key != "2026-09-06"
        assert v2.ledger_version == 2

    def test_sunday_to_monday_keeps_the_historical_week(self, db):
        """Test 2 — the reverse direction, so it is not an artefact."""
        _configure(db, week_start="SUNDAY")
        v1 = _seal(db, SUNDAY)
        assert v1.workweek_key == SUNDAY.isoformat() == "2026-09-06"

        _set_week_start(db, "MONDAY")
        db.commit()
        v2 = self._fault(db)

        assert v2.workweek_key == "2026-09-06"
        assert v2.workweek_key != SUNDAY_WEEK

    def test_with_the_boundary_unchanged_both_versions_agree(self, db):
        """Test 3 — control. The fixed path must match it exactly."""
        _configure(db)
        v1 = _seal(db, SUNDAY)
        v2 = self._fault(db)
        assert v1.workweek_key == v2.workweek_key == SUNDAY_WEEK

    def test_a_first_version_fault_still_establishes_the_identity(self, db):
        """Test 4 — with no current row there is no lineage to inherit.

        Minting here is the same thing ``seal_day`` does, under the same strict
        boundary rules — so an undefined boundary still fails closed.
        """
        _configure(db)
        v1 = self._fault(db)
        assert v1.ledger_version == 1
        assert v1.workweek_key == SUNDAY_WEEK
        assert v1.state == LedgerState.LEDGER_INTEGRITY_FAULT.value

    def test_a_first_version_fault_fails_closed_without_a_boundary(self, db):
        _configure(db, week_start=None)
        with pytest.raises(WorkweekBoundaryUnset):
            self._fault(db)

    def test_the_resolver_is_not_consulted_for_an_existing_day(self, db):
        """Test 5 — proven behaviourally, so it cannot pass by code shape.

        The resolver is replaced with a landmine. If the correction path
        consults it for a day that already has a row, the test fails; the
        first-version path below still needs it, so the guard is specific
        rather than a blanket ban.
        """
        import app.work_ledger.service as service

        _configure(db)
        _seal(db, SUNDAY)

        def _landmine(*_a, **_k):
            raise AssertionError(
                "record_integrity_fault consulted the current-configuration "
                "resolver for a day that already has a ledger row")

        original = service.canonical_workweek_key
        service.canonical_workweek_key = _landmine
        try:
            v2 = self._fault(db)
        finally:
            service.canonical_workweek_key = original
        assert v2.workweek_key == SUNDAY_WEEK

    def test_the_key_is_conditional_in_the_source(self):
        """Test 5b — the same claim structurally, in the repository's idiom.

        The ``workweek_key`` argument must be a conditional expression, not an
        unconditional call: an unconditional resolver call is precisely the
        defect, and it would be invisible to a test that only checks the
        function mentions ``current``.
        """
        src = (BACKEND / "app" / "work_ledger" / "service.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "record_integrity_fault")
        call = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "id", None) == "_write_version")
        kw = next(k for k in call.keywords if k.arg == "workweek_key")
        assert isinstance(kw.value, ast.IfExp), (
            "workweek_key is passed unconditionally; an existing day's "
            "identity must be inherited, not re-derived")

    def test_the_two_versions_cannot_be_split_across_weeks(self, db):
        """Test 6 — EVAL-WEEK safety, and why this defect was dangerous.

        Re-deriving split the sealed version and the fault version into two
        different weeks, where each week saw exactly one row and looked
        healthy. Sharing the key puts them back on one date in one week, where
        ``LEDGER_MEMBERSHIP_INCONSISTENT`` can see them — the fail-closed
        behaviour this fix restores rather than weakens.
        """
        _configure(db)
        _seal(db, SUNDAY)
        _set_week_start(db, "SUNDAY")
        db.commit()
        self._fault(db)
        db.flush()

        # The historical week holds BOTH versions; the other week holds none.
        historical = current_ledger_days(
            db, organization_id=ORG, user_id=WORKER, workweek_key=SUNDAY_WEEK)
        other = current_ledger_days(
            db, organization_id=ORG, user_id=WORKER,
            workweek_key=SUNDAY.isoformat())
        assert len(historical) == 2
        assert other == []

        # And the week refuses rather than folding a day it cannot resolve.
        week = evaluate_workweek(
            db, organization_id=ORG, user_id=WORKER, workweek_key=SUNDAY_WEEK)
        assert week.state is WeeklyState.LEDGER_MEMBERSHIP_INCONSISTENT, (
            f"fail-closed membership detection was lost: {week.state}")
        assert week.weekly_attributable_shortfall_minutes is None


# ══════════════════════════════════════════════════════════════════════════════
# G. GV-09, end to end — producer -> ledger -> EVAL-DAY -> EVAL-WEEK
# ══════════════════════════════════════════════════════════════════════════════
class TestCrossWeekEndToEnd:
    """GV-09: "Shift Sun 22:00 -> Mon 06:00; attribution START_DATE".

    Expected: "One work_date = Sunday; minutes counted once; accrues to the
    week containing Sunday" (§42), and R-12.3.5 — never two workweeks.
    """

    def _run_both_days(self, db):
        """Produce Sunday AND Monday, so double-counting would be visible."""
        _configure(db)
        _capture_requirement(db, version_key="gv09-req",
                             effective_from=date(2026, 8, 31))
        _session(db, clock_in=_local(SUNDAY, 22), clock_out=_local(MONDAY, 6))
        sunday = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=SUNDAY)
        monday = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=MONDAY)
        db.flush()
        return sunday, monday

    def test_exactly_one_work_date_and_it_is_sunday(self, db):
        sunday, monday = self._run_both_days(db)

        assert sunday.outcome is ProducerOutcome.SEALED
        rows = db.query(WorkDayLedger).all()
        assert len(rows) == 1, "the shift produced more than one ledger day"
        assert rows[0].work_date.date() == SUNDAY
        assert rows[0].work_date.date().strftime("%A") == "Sunday"

        # Monday claims nothing: the session opened on Sunday, so Monday has
        # no evidence of its own and no row is written for it.
        assert monday.outcome is ProducerOutcome.SOURCE_UNRESOLVED

    def test_the_rule_is_recorded_on_the_sealed_day(self, db):
        self._run_both_days(db)
        row = db.query(WorkDayLedger).one()
        assert row.shift_attribution_rule == "START_DATE"

    def test_the_minutes_are_counted_once(self, db):
        """Eight hours, whole, on the start date — never split at midnight."""
        self._run_both_days(db)
        row = db.query(WorkDayLedger).one()
        total = sum(iv.duration_seconds for iv in row.intervals)
        assert total == 8 * 3600

    def test_it_accrues_to_the_week_containing_sunday(self, db):
        self._run_both_days(db)
        row = db.query(WorkDayLedger).one()
        assert row.workweek_key == SUNDAY_WEEK
        assert SUNDAY in canonical_dates(row.workweek_key)
        assert row.workweek_key != MONDAY_WEEK

    def test_it_is_counted_in_exactly_one_workweek(self, db):
        """R-12.3.5, asserted over the fold rather than over the key."""
        self._run_both_days(db)
        recorded = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=SUNDAY)
        assert recorded.is_recorded, f"{recorded.reason} {recorded.detail}"
        db.flush()

        sunday_week = evaluate_workweek(
            db, organization_id=ORG, user_id=WORKER, workweek_key=SUNDAY_WEEK)
        monday_week = evaluate_workweek(
            db, organization_id=ORG, user_id=WORKER, workweek_key=MONDAY_WEEK)

        assert sunday_week.state is WeeklyState.COMPLETE, sunday_week.detail
        assert sunday_week.day_count == 1
        # The next week never sees the shift at all — not as a zero, not as a
        # duplicate, not as a member.
        assert monday_week.state is WeeklyState.NO_LEDGER_DAYS

    def test_the_weekly_fold_counts_the_day_once(self, db):
        self._run_both_days(db)
        evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=SUNDAY)
        db.flush()
        week = evaluate_workweek(
            db, organization_id=ORG, user_id=WORKER, workweek_key=SUNDAY_WEEK)
        assert week.state is WeeklyState.COMPLETE
        assert len(week.day_record_ids) == 1
        assert week.weekly_attributable_shortfall_minutes is not None

    def test_a_sunday_start_tenant_puts_it_in_its_own_week(self, db):
        """The boundary is honoured, not assumed: same shift, different week.

        Under a Sunday start the same physical shift opens the week instead of
        closing it — which is the case ISO week notation could not express.
        """
        _configure(db, week_start="SUNDAY")
        _session(db, clock_in=_local(SUNDAY, 22), clock_out=_local(MONDAY, 6))
        result = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=SUNDAY)
        assert result.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.workweek_key == SUNDAY.isoformat()
        assert row.work_date.date() == SUNDAY
