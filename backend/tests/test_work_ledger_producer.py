"""
§45 — the forward-only Work Ledger producer, and owner decisions D-18A..E.
==========================================================================

Proves the producer contract:

* forward-only, gated on a durable per-tenant cutover it reads itself (D-18C);
* every session boundary gated on a D-15 provenance event, never inferred;
* only a VERIFIED departure is sealable — synthesized closures are refused and
  never relabelled (D-17);
* WRK split around complete breaks, breaks left as GAPS, no BRK_M/BRK_R (D-18);
* OFF from OfflineInterval applied windows (D-18B);
* producer-authored boundaries carry PRODUCER_DERIVED (D-18A);
* timezone and working-day results resolved strictly and frozen (D-18D/E);
* nothing repaired, clamped, unioned or silently skipped;
* no backfill, and no cached total ever read as worked time.

Foreign keys are enforced on a DEDICATED engine: the shared test engine leaves
``PRAGMA foreign_keys`` off, and several refusals here would pass vacuously
without it.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

import database.models as dbm
from database.database import Base

from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.schedule_policy import keys as K
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval
from app.work_ledger.states import Provenance, ProvenanceClass, WorkState
from app.work_ledger.states import classify_provenance
from app.work_ledger_producer import (
    CUTOVER_HISTORY_TABLE,
    PERMITTED_WORKING_DAY_SOURCES,
    SUPPORTED_DIALECTS,
    ProducerCutoverImmutable,
    ProducerCutoverMovedBackward,
    ProducerOutcome,
    RefusalReason,
    WorkLedgerProducerCutoverHistory,
    append_only_statements,
    get_producer_cutover,
    produce_and_seal_work_day,
    resolve_day_context,
    resolve_session_evidence,
    set_producer_cutover,
    source_evidence_fingerprint,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = (BACKEND / "alembic" / "versions"
             / "d5f1a8c3b724_producer_cutover_and_working_day.py")

ORG = 9901
OTHER_ORG = 9902
WORKER = 9911
OTHER = 9912

TZ_IST = "Asia/Kolkata"          # +05:30, no DST
TZ_LONDON = "Europe/London"      # DST

#: A Monday, so it is a working day under a MON-FRI week — and a date in
#: the PAST, because the D18-F3 closure gate refuses a day that has not ended.
DAY = date(2026, 9, 7)
#: Comfortably before DAY's local start.
CUTOVER = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
#: DAY's organization-local start and end in IST, as UTC instants.
DAY_START_IST = datetime(2026, 9, 6, 18, 30, tzinfo=UTC)
DAY_END_IST = datetime(2026, 9, 7, 18, 30, tzinfo=UTC)

MON_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def fk_engine():
    import app.clock_session_boundary.models   # noqa: F401
    import app.work_ledger.models              # noqa: F401
    import app.work_ledger_producer.models     # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(fk_engine):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(fk_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Org P"))
        s.add(dbm.Organization(id=OTHER_ORG, name="Org Q"))
        s.add(dbm.User(id=WORKER, name="W", email="w@example.test",
                       organization_id=ORG, role="employee"))
        s.add(dbm.User(id=OTHER, name="O", email="o@example.test",
                       organization_id=ORG, role="employee"))
        s.commit()
        yield s
    _tz.invalidate_tz_cache()


def _configure(db, *, tz=TZ_IST, working_days=MON_FRI, cutover=CUTOVER,
               week_start="MONDAY"):
    """The four gates, satisfied: timezone, working week, cutover, boundary.

    ``week_start`` joins them because the ECO workweek path now resolves
    ``schedule.week_start_day`` strictly and refuses an organization that has
    not defined one (§12.1). MONDAY is what the lenient resolver used to
    default to, so every expectation in this file groups days exactly as it
    did; pass ``None`` to exercise the unset-boundary refusal.
    """
    from app import _tz
    if tz is not None:
        db.add(dbm.OrgSettings(organization_id=ORG, default_timezone=tz))
    if working_days is not None:
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=None,
            key=K.SETTING_WORKING_DAYS, value=json.dumps(working_days)))
    if week_start is not None:
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=None,
            key=K.SETTING_WEEK_START_DAY, value=week_start))
    db.commit()
    _tz.invalidate_tz_cache()
    if cutover is not None:
        set_producer_cutover(db, organization_id=ORG, cutover_at=cutover,
                             actor_label="owner@example.test")
        db.commit()




def _strip_docstrings(source: str) -> str:
    """Source with every docstring removed, for code-only scans."""
    tree = ast.parse(source)
    drop = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            drop.update(range(body[0].lineno, body[0].end_lineno + 1))
    return chr(10).join(
        line for n, line in enumerate(source.splitlines(), 1)
        if n not in drop and not line.strip().startswith("#")
    )


def _code_only(module_name: str) -> str:
    """A producer module's source with every docstring removed.

    The module docstrings deliberately NAME the forbidden inputs in order to
    document that they are forbidden, so a plain substring scan would flag the
    documentation rather than a read.
    """
    module = __import__(f"app.work_ledger_producer.{module_name}",
                        fromlist=["x"])
    src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            spans.append((body[0].lineno, body[0].end_lineno))
    drop = {n for a, b in spans for n in range(a, b + 1)}
    kept = [
        line for n, line in enumerate(src.splitlines(), 1)
        if n not in drop and not line.strip().startswith("#")
    ]
    return chr(10).join(kept)


def _at(day: date, hh: int, mm: int = 0, tz_name: str = TZ_IST) -> datetime:
    """A local wall-clock instant on ``day``, as aware UTC."""
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(tz_name)
    return datetime(day.year, day.month, day.day, hh, mm,
                    tzinfo=zone).astimezone(UTC)


def _session(db, *, clock_in, clock_out,
             open_prov=Provenance.WORKER, close_prov=Provenance.WORKER,
             user_id=WORKER, organization_id=ORG,
             actor="w@example.test", with_events=True):
    row = dbm.ClockSession(
        user_id=user_id, organization_id=organization_id,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=999999, break_seconds=888, offline_seconds=777,
        is_active=clock_out is None,
    )
    db.add(row)
    db.flush()
    if with_events:
        append_boundary_event(
            db, session_id=row.id, user_id=user_id,
            boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
            provenance=open_prov, actor_label=actor)
        if clock_out is not None:
            append_boundary_event(
                db, session_id=row.id, user_id=user_id,
                boundary=BOUNDARY_CLOCK_OUT, boundary_at=clock_out,
                provenance=close_prov, actor_label=actor)
    db.commit()
    return row


def _break(db, session, start, end, *, user_id=WORKER, btype="lunch"):
    row = dbm.BreakSession(
        user_id=user_id, session_id=session.id, break_start=start,
        break_end=end, break_type=btype, break_seconds=123456,
    )
    db.add(row)
    db.commit()
    return row


def _offline(db, session, start, end, *, user_id=WORKER):
    row = dbm.OfflineInterval(
        user_id=user_id, session_id=session.id, organization_id=ORG,
        offline_start=start, offline_end=end,
        claimed_seconds=99999, applied_seconds=88888,
        capped=True, cap_reason="session_total",
    )
    db.add(row)
    db.commit()
    return row


def _run(db, *, work_date=DAY, user_id=WORKER, organization_id=ORG):
    return produce_and_seal_work_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date)


def _intervals(db, ledger_id):
    return (db.query(WorkStateInterval)
            .filter(WorkStateInterval.ledger_id == ledger_id)
            .order_by(WorkStateInterval.seq).all())


# ═════════════════════════════════════════════════════════════════════════════
# D-18A — the ninth provenance value
# ═════════════════════════════════════════════════════════════════════════════
class TestProducerDerivedProvenance:
    def test_it_is_in_the_canonical_vocabulary(self):
        assert Provenance.PRODUCER_DERIVED.value == "PRODUCER_DERIVED"
        assert "PRODUCER_DERIVED" in {p.value for p in Provenance}

    def test_it_classifies_as_derived(self):
        assert classify_provenance(Provenance.PRODUCER_DERIVED) \
            is ProvenanceClass.DERIVED
        assert classify_provenance("PRODUCER_DERIVED") is ProvenanceClass.DERIVED

    def test_it_is_never_verified_departure_evidence(self):
        from app.work_ledger.states import (
            SYNTHESIZED_CLOSURE_PROVENANCE, VERIFIED_DEPARTURE_PROVENANCE,
            is_verified_departure,
        )
        assert Provenance.PRODUCER_DERIVED not in VERIFIED_DEPARTURE_PROVENANCE
        assert Provenance.PRODUCER_DERIVED not in SYNTHESIZED_CLOSURE_PROVENANCE
        assert is_verified_departure(Provenance.PRODUCER_DERIVED) is False

    def test_it_joins_the_derived_boundary_set(self):
        from app.work_ledger.states import DERIVED_BOUNDARY_PROVENANCE
        assert Provenance.PRODUCER_DERIVED in DERIVED_BOUNDARY_PROVENANCE

    def test_parsing_and_validation_accept_it(self):
        from app.work_ledger.states import is_valid_provenance, parse_provenance
        assert is_valid_provenance("PRODUCER_DERIVED") is True
        assert parse_provenance("PRODUCER_DERIVED") is Provenance.PRODUCER_DERIVED

    def test_the_vocabulary_closed_guard_recognises_it(self):
        from app.work_ledger.provenance import assert_provenance_vocabulary_closed

        class _Row:
            id = 1
            provenance = "PRODUCER_DERIVED"

        assert_provenance_vocabulary_closed([_Row()])   # must not raise

    def test_there_is_exactly_one_provenance_enum(self):
        """No duplicate vocabulary: the boundary package re-exports, and the
        producer imports, the identical object."""
        from app.clock_session_boundary import Provenance as boundary_enum
        from app.work_ledger.states import Provenance as canonical
        assert boundary_enum is canonical

    def test_the_five_d15_session_close_values_are_unchanged(self):
        from app.clock_session_boundary import CLOSE_BOUNDARY_PROVENANCE
        assert {p.value for p in CLOSE_BOUNDARY_PROVENANCE} == {
            "WORKER", "ADMIN_OVERRIDE", "STALE_AUTOCLOSE",
            "ABANDONED_AUTOCLOSE", "OFFBOARDING",
        }
        assert Provenance.PRODUCER_DERIVED not in CLOSE_BOUNDARY_PROVENANCE

    def test_it_is_rejected_by_the_d15_boundary_recorder(self, db):
        from app.clock_session_boundary import InvalidBoundaryProvenance
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        for boundary in (BOUNDARY_CLOCK_IN, BOUNDARY_CLOCK_OUT):
            with pytest.raises(InvalidBoundaryProvenance):
                append_boundary_event(
                    db, session_id=s.id, user_id=WORKER, boundary=boundary,
                    boundary_at=_at(DAY, 9),
                    provenance=Provenance.PRODUCER_DERIVED, actor_label="x")

    def test_no_migration_was_created_for_the_new_value(self):
        """`work_state_interval.provenance` is VARCHAR(32) with no enum/CHECK,
        so the vocabulary is enforced in the application. A migration for the
        value would have been pure churn."""
        col = WorkStateInterval.__table__.c.provenance
        assert isinstance(col.type, sa.String) and col.type.length == 32
        checks = {c.name for c in WorkStateInterval.__table__.constraints
                  if isinstance(c, sa.CheckConstraint)}
        assert checks == {"ck_work_state_interval_duration"}
        body = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(body)
        up = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        touched = [
            n.args[0] for n in ast.walk(up) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) in ("add_column", "alter_column")
        ]
        assert all(getattr(t, "id", None) == "LEDGER_TABLE" for t in touched)


# ═════════════════════════════════════════════════════════════════════════════
# D-18C — the durable cutover
# ═════════════════════════════════════════════════════════════════════════════
class TestCutover:
    def test_it_has_no_default(self, db):
        assert get_producer_cutover(db, ORG) is None
        col = WorkLedgerProducerCutoverHistory.__table__.c.producer_cutover_at
        assert col.nullable is False
        assert col.default is None and col.server_default is None

    def test_there_is_no_revision_token(self):
        """D18-F1 removed it: append-only needs no optimistic token."""
        assert "revision" not in WorkLedgerProducerCutoverHistory.__table__.c

    def test_many_rows_per_organization_are_allowed(self):
        """It is a history. A unique constraint would defeat the design."""
        uniques = {
            tuple(sorted(c.name for c in con.columns))
            for con in WorkLedgerProducerCutoverHistory.__table__.constraints
            if isinstance(con, sa.UniqueConstraint)
        }
        assert ("organization_id",) not in uniques

    def test_initial_set_succeeds(self, db):
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        assert get_producer_cutover(db, ORG) == CUTOVER

    def test_moving_it_later_succeeds(self, db):
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        later = CUTOVER + timedelta(days=7)
        set_producer_cutover(db, organization_id=ORG, cutover_at=later)
        db.commit()
        assert get_producer_cutover(db, ORG) == later

    def test_moving_it_earlier_is_refused(self, db):
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        with pytest.raises(ProducerCutoverMovedBackward):
            set_producer_cutover(db, organization_id=ORG,
                                 cutover_at=CUTOVER - timedelta(seconds=1))
        db.rollback()
        assert get_producer_cutover(db, ORG) == CUTOVER

    def test_setting_the_same_instant_is_idempotent(self, db):
        """No second history row, and no history noise."""
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        assert db.query(WorkLedgerProducerCutoverHistory).count() == 1
        assert get_producer_cutover(db, ORG) == CUTOVER

    def test_moving_it_later_appends_rather_than_rewriting(self, db):
        """D18-F1: history, not a mutable current value."""
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        later = CUTOVER + timedelta(days=7)
        set_producer_cutover(db, organization_id=ORG, cutover_at=later)
        db.commit()
        rows = (db.query(WorkLedgerProducerCutoverHistory)
                .order_by(WorkLedgerProducerCutoverHistory.id).all())
        assert len(rows) == 2
        assert [r.producer_cutover_at.replace(tzinfo=UTC) for r in rows] == [
            CUTOVER, later]
        assert get_producer_cutover(db, ORG) == later

    def test_a_recorded_value_cannot_be_rewritten_via_the_orm(self, db):
        """Defence in depth behind the database triggers."""
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        row = db.query(WorkLedgerProducerCutoverHistory).one()
        with pytest.raises(ProducerCutoverImmutable):
            row.producer_cutover_at = CUTOVER - timedelta(days=1)
        db.rollback()

    def test_history_cannot_be_deleted_via_the_orm(self, db):
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        row = db.query(WorkLedgerProducerCutoverHistory).one()
        with pytest.raises(ProducerCutoverImmutable):
            db.delete(row)
            db.flush()
        db.rollback()
        assert get_producer_cutover(db, ORG) == CUTOVER

    def test_concurrent_writers_both_append_and_the_later_wins(self, db,
                                                               fk_engine):
        """Append-only needs no optimistic token: there is no lost update."""
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()

        ahead = CUTOVER + timedelta(days=30)
        with SaSession(fk_engine, expire_on_commit=False) as other:
            set_producer_cutover(other, organization_id=ORG, cutover_at=ahead)
            other.commit()

        # A now-stale forward move is refused because it is no longer forward.
        with pytest.raises(ProducerCutoverMovedBackward):
            set_producer_cutover(db, organization_id=ORG,
                                 cutover_at=CUTOVER + timedelta(days=1))
        db.rollback()
        assert get_producer_cutover(db, ORG) == ahead

    def test_organizations_are_isolated(self, db):
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        assert get_producer_cutover(db, OTHER_ORG) is None
        other_mark = CUTOVER + timedelta(days=90)
        set_producer_cutover(db, organization_id=OTHER_ORG,
                             cutover_at=other_mark)
        db.commit()
        assert get_producer_cutover(db, ORG) == CUTOVER
        assert get_producer_cutover(db, OTHER_ORG) == other_mark

    def test_a_naive_instant_is_refused(self, db):
        with pytest.raises(ValueError):
            set_producer_cutover(db, organization_id=ORG,
                                 cutover_at=datetime(2026, 9, 1, 0, 0))

    def test_the_producer_reads_the_durable_value_not_a_parameter(self):
        import inspect
        params = inspect.signature(produce_and_seal_work_day).parameters
        assert set(params) == {
            "db", "organization_id", "user_id", "work_date"}
        for banned in ("cutover", "intervals", "provenance", "timezone",
                       "is_working_day", "working_day_source"):
            assert banned not in params


# ═════════════════════════════════════════════════════════════════════════════
# The three forward-only gates
# ═════════════════════════════════════════════════════════════════════════════
class TestForwardOnlyGates:
    def test_missing_cutover_refuses(self, db):
        _configure(db, cutover=None)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.REFUSED
        assert result.refusal is RefusalReason.CUTOVER_NOT_SET
        assert db.query(WorkDayLedger).count() == 0

    def test_a_day_before_the_cutover_refuses(self, db):
        _configure(db, cutover=datetime(2026, 9, 30, 0, 0, tzinfo=UTC))
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.REFUSED
        assert result.refusal is RefusalReason.BEFORE_CUTOVER
        assert db.query(WorkDayLedger).count() == 0

    def test_the_comparison_is_on_the_local_day_start(self, db):
        """A cutover exactly at DAY's local start is 'at', not 'before',
        so the day proceeds."""
        _configure(db, cutover=DAY_START_IST)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        assert _run(db).outcome is ProducerOutcome.SEALED

    def test_one_second_after_the_local_day_start_refuses(self, db):
        _configure(db, cutover=DAY_START_IST + timedelta(seconds=1))
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.refusal is RefusalReason.BEFORE_CUTOVER

    def test_moving_the_cutover_later_never_makes_a_day_eligible(self, db):
        _configure(db, cutover=CUTOVER)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        set_producer_cutover(db, organization_id=ORG,
                             cutover_at=datetime(2026, 10, 1, tzinfo=UTC))
        db.commit()
        assert _run(db).refusal is RefusalReason.BEFORE_CUTOVER


# ═════════════════════════════════════════════════════════════════════════════
# D-18E — strict timezone
# ═════════════════════════════════════════════════════════════════════════════
class TestTimezoneStrictness:
    @pytest.mark.parametrize("value,reason", [
        (None, RefusalReason.TIMEZONE_NOT_CONFIGURED),
        ("", RefusalReason.TIMEZONE_NOT_CONFIGURED),
        ("   ", RefusalReason.TIMEZONE_NOT_CONFIGURED),
        ("California, Sacramento", RefusalReason.TIMEZONE_INVALID),
        ("Not/AZone", RefusalReason.TIMEZONE_INVALID),
    ])
    def test_unusable_configuration_refuses(self, db, value, reason):
        from app.work_ledger_producer.context import resolve_timezone
        from app import _tz
        if value is not None:
            db.add(dbm.OrgSettings(organization_id=ORG, default_timezone=value))
            db.commit()
        _tz.invalidate_tz_cache()
        assert resolve_timezone(db, ORG) is reason

    def test_no_orgsettings_row_at_all_refuses(self, db):
        from app.work_ledger_producer.context import resolve_timezone
        assert resolve_timezone(db, ORG) is \
            RefusalReason.TIMEZONE_NOT_CONFIGURED

    def test_it_never_seals_on_the_documented_fallback(self, db):
        """`_tz` falls back to Asia/Kolkata for an unconfigured tenant. The
        producer must refuse instead — otherwise five unconfigured tenants get
        sealed on India's clock, the exact defect `_tz`'s logging exposes."""
        from app import _tz
        _configure(db, tz=None)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        assert _tz.org_timezone_name(db, ORG) == _tz.FALLBACK_TZ_NAME
        result = _run(db)
        assert result.refusal is RefusalReason.TIMEZONE_NOT_CONFIGURED
        assert db.query(WorkDayLedger).count() == 0

    def test_a_valid_zone_is_accepted_and_frozen(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        assert result.timezone_iana == TZ_IST
        assert result.utc_offset_seconds == 5 * 3600 + 1800
        row = db.query(WorkDayLedger).one()
        assert row.timezone_iana == TZ_IST
        assert row.utc_offset_seconds == 19800

    def test_helper_divergence_refuses(self, db, monkeypatch):
        from app import _tz
        from app.work_ledger_producer import context as ctx
        _configure(db)
        monkeypatch.setattr(ctx._tz, "org_timezone_name",
                            lambda db_, org: "Europe/Paris")
        assert ctx.resolve_timezone(db, ORG) is \
            RefusalReason.TIMEZONE_HELPER_DIVERGENCE

    @pytest.mark.parametrize("day,expected_offset", [
        (date(2026, 3, 23), 0),        # GMT
        (date(2026, 3, 30), 3600),     # BST — after the transition
        (date(2025, 10, 27), 0),       # GMT again, after BST 2025 ended
    ])
    def test_the_frozen_offset_is_the_one_in_force_that_day(
            self, db, day, expected_offset):
        """DST: the offset is evaluated at that day's local start, so a day
        either side of a transition records its own offset."""
        _configure(db, tz=TZ_LONDON,
                   cutover=datetime(2025, 1, 1, tzinfo=UTC))
        _session(db,
                 clock_in=_at(day, 9, tz_name=TZ_LONDON),
                 clock_out=_at(day, 17, tz_name=TZ_LONDON))
        result = _run(db, work_date=day)
        assert result.outcome is ProducerOutcome.SEALED, result.refusal
        assert result.timezone_iana == TZ_LONDON
        assert result.utc_offset_seconds == expected_offset


# ═════════════════════════════════════════════════════════════════════════════
# D-18D — the frozen working day
# ═════════════════════════════════════════════════════════════════════════════
class TestWorkingDayFreeze:
    def test_the_columns_are_not_null_with_no_default(self):
        for name in ("is_working_day", "working_day_source"):
            col = WorkDayLedger.__table__.c[name]
            assert col.nullable is False, name
            assert col.default is None and col.server_default is None, name
        assert WorkDayLedger.__table__.c.working_day_source.type.length == 32

    def test_an_org_tier_source_is_preserved_verbatim(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        assert result.working_day_source == "org"
        assert result.is_working_day is True
        row = db.query(WorkDayLedger).one()
        assert row.working_day_source == "org"
        assert row.is_working_day is True

    def test_an_employee_tier_source_is_preserved_verbatim(self, db):
        _configure(db)
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=WORKER,
            key=K.SETTING_WORKING_DAYS, value=json.dumps(MON_FRI)))
        db.commit()
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        assert _run(db).working_day_source == "employee"

    def test_a_system_source_refuses(self, db):
        """No tier configured the working week: the Worker would be measured
        against a schedule nobody chose."""
        _configure(db, working_days=None)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.refusal is RefusalReason.WORKING_DAY_UNRESOLVED
        assert db.query(WorkDayLedger).count() == 0

    def test_the_system_source_is_never_converted_to_another(self, db):
        from app.work_ledger_producer.context import resolve_working_day
        _configure(db, working_days=None)
        worker = db.get(dbm.User, WORKER)
        assert resolve_working_day(db, worker, DAY) is \
            RefusalReason.WORKING_DAY_UNRESOLVED

    def test_an_unknown_source_refuses(self, db, monkeypatch):
        """The resolver can emit `legacy_flexible_days`, which is outside the
        owner-ruled vocabulary. Refused rather than mapped onto a permitted
        label — "never infer a more specific source"."""
        from app.work_ledger_producer import context as ctx

        class _Sched:
            is_working_day = True
            sources = {"is_working_day": "legacy_flexible_days"}

        monkeypatch.setattr(
            "app.schedule_policy.resolver.resolve_work_schedule",
            lambda *a, **k: _Sched())
        _configure(db)
        worker = db.get(dbm.User, WORKER)
        assert ctx.resolve_working_day(db, worker, DAY) is \
            RefusalReason.WORKING_DAY_SOURCE_UNKNOWN

    def test_the_permitted_vocabulary_is_the_resolvers_own(self):
        assert PERMITTED_WORKING_DAY_SOURCES == {
            "assignment", "shift", "employee", "legacy_user", "team", "org",
            "system",
        }

    def test_a_non_working_day_is_frozen_and_carried_into_the_ledger_state(self, db):
        """Work on a Sunday is real evidence, and the day is still not a
        working day.

        Owner ruling D13-A: the producer PASSES THROUGH the classifier's
        verdict, so the ledger is sealed ``NOT_A_WORKING_DAY`` rather than
        ``SEALED``. It still decides nothing — the verdict came from
        ``working_day_classifier`` — and the frozen context is unchanged.

        Before D13-A this asserted ``state == SEALED``, which left
        ``LedgerState.NOT_A_WORKING_DAY`` with no production writer and GV-28
        unexecutable.
        """
        sunday = date(2026, 9, 6)
        _configure(db)
        _session(db, clock_in=_at(sunday, 9), clock_out=_at(sunday, 12))
        result = _run(db, work_date=sunday)
        assert result.outcome is ProducerOutcome.SEALED
        assert result.is_working_day is False
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.NOT_A_WORKING_DAY.value
        assert row.is_working_day is False
        # The source label is preserved verbatim, never re-coded (D11-A).
        assert row.working_day_source == "org"
        # The accidental clock-in is still sealed as evidence — the day is
        # classified by the schedule, never by whether work exists.
        assert _intervals(db, row.id), "the session's evidence was discarded"

    def test_a_working_day_still_seals_as_sealed(self, db):
        """The pass-through must not change the ordinary case."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        assert result.is_working_day is True
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.SEALED.value
        assert row.is_working_day is True

    def test_a_later_resolver_change_does_not_move_a_sealed_value(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        assert _run(db).outcome is ProducerOutcome.SEALED

        # Reconfigure the working week so MONDAY is no longer a working day.
        row = (db.query(dbm.AppSettings)
               .filter(dbm.AppSettings.key == K.SETTING_WORKING_DAYS).one())
        row.value = json.dumps(["TUESDAY"])
        db.commit()

        sealed = db.query(WorkDayLedger).one()
        assert sealed.is_working_day is True
        assert sealed.working_day_source == "org"


# ═════════════════════════════════════════════════════════════════════════════
# D-15 boundary provenance is mandatory
# ═════════════════════════════════════════════════════════════════════════════
class TestBoundaryProvenanceIsRequired:
    def test_a_session_with_no_boundary_events_refuses(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                 with_events=False)
        result = _run(db)
        assert result.refusal is RefusalReason.BOUNDARY_PROVENANCE_MISSING
        assert db.query(WorkDayLedger).count() == 0

    def test_a_missing_close_event_refuses(self, db):
        _configure(db)
        row = dbm.ClockSession(
            user_id=WORKER, organization_id=ORG,
            clock_in_time=_at(DAY, 9), clock_out_time=_at(DAY, 17),
            total_seconds=0, break_seconds=0, is_active=False)
        db.add(row)
        db.flush()
        append_boundary_event(
            db, session_id=row.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_IN, boundary_at=_at(DAY, 9),
            provenance=Provenance.WORKER, actor_label="w@example.test")
        db.commit()
        assert _run(db).refusal is RefusalReason.BOUNDARY_PROVENANCE_MISSING

    def test_an_event_describing_a_different_instant_refuses(self, db):
        """The event must describe the instant the session row holds. A
        divergence means the row was written by a path that recorded no
        origin — which is what D-15 exists to make impossible."""
        _configure(db)
        row = dbm.ClockSession(
            user_id=WORKER, organization_id=ORG,
            clock_in_time=_at(DAY, 9), clock_out_time=_at(DAY, 17),
            total_seconds=0, break_seconds=0, is_active=False)
        db.add(row)
        db.flush()
        append_boundary_event(
            db, session_id=row.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_IN, boundary_at=_at(DAY, 9),
            provenance=Provenance.WORKER, actor_label="w@example.test")
        append_boundary_event(
            db, session_id=row.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_OUT, boundary_at=_at(DAY, 16),  # != 17:00
            provenance=Provenance.WORKER, actor_label="w@example.test")
        db.commit()
        assert _run(db).refusal is RefusalReason.BOUNDARY_PROVENANCE_MISMATCH

    def test_the_latest_sequence_is_authoritative(self, db):
        """An admin correction appends. The producer reads the latest event, so
        an ADMIN_OVERRIDE close supersedes the Worker's earlier one."""
        _configure(db)
        row = dbm.ClockSession(
            user_id=WORKER, organization_id=ORG,
            clock_in_time=_at(DAY, 9), clock_out_time=_at(DAY, 18),
            total_seconds=0, break_seconds=0, is_active=False)
        db.add(row)
        db.flush()
        append_boundary_event(
            db, session_id=row.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_IN, boundary_at=_at(DAY, 9),
            provenance=Provenance.WORKER, actor_label="w@example.test")
        # seq 1: the Worker's own close, at a now-superseded instant.
        append_boundary_event(
            db, session_id=row.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_OUT, boundary_at=_at(DAY, 17),
            provenance=Provenance.WORKER, actor_label="w@example.test")
        # seq 2: the admin's correction, matching the column.
        append_boundary_event(
            db, session_id=row.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_OUT, boundary_at=_at(DAY, 18),
            provenance=Provenance.ADMIN_OVERRIDE,
            actor_label="a@example.test")
        db.commit()

        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        ivs = _intervals(db, result.ledger_id)
        assert [i.provenance for i in ivs] == ["ADMIN_OVERRIDE"]

    def test_provenance_is_never_inferred_from_the_timestamp(self, db):
        """A zero-length session is indistinguishable BY VALUE from an instant
        Worker clock-out; only the event says which it was."""
        _configure(db)
        instant = _at(DAY, 9)
        _session(db, clock_in=instant, clock_out=instant,
                 close_prov=Provenance.ABANDONED_AUTOCLOSE)
        result = _run(db)
        assert result.refusal in (
            RefusalReason.NON_POSITIVE_INTERVAL,
            RefusalReason.SESSION_SYNTHESIZED_CLOSURE,
        )


# ═════════════════════════════════════════════════════════════════════════════
# D-17 — only verified departures are sealable
# ═════════════════════════════════════════════════════════════════════════════
class TestClosureEligibility:
    def test_a_worker_close_seals(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        assert [i.provenance for i in _intervals(db, result.ledger_id)] \
            == ["WORKER"]

    def test_an_admin_close_seals_and_keeps_its_provenance(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                 close_prov=Provenance.ADMIN_OVERRIDE,
                 actor="a@example.test")
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        assert [i.provenance for i in _intervals(db, result.ledger_id)] \
            == ["ADMIN_OVERRIDE"]

    @pytest.mark.parametrize("prov", [
        Provenance.STALE_AUTOCLOSE,
        Provenance.ABANDONED_AUTOCLOSE,
        Provenance.OFFBOARDING,
    ])
    def test_a_synthesized_closure_is_sealed_deferred_never_relabelled(
            self, db, prov):
        """§24.7: recorded as the unverified exit it is — still never relabelled.

        This guard has tracked three behaviours as the owner rulings landed:
        refusal, then (wrongly) MISSING_CLOCK_OUT, then refusal again, and now
        a sealed §24.7 day. The claim it exists to protect has never changed
        and is asserted below in full: a synthesized close is **never** a
        verified departure and **never** a missing clock-out.

        What changed is that the ledger can now record the exit that actually
        happened, so the day is sealed with its real provenance and deferred,
        instead of being dropped because there was nowhere to put it.
        """
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                 close_prov=prov, actor="SYSTEM")
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        # SEALED, not MISSING_CLOCK_OUT — §19.1.
        assert row.state == LedgerState.SEALED.value
        assert row.state != LedgerState.MISSING_CLOCK_OUT.value
        # The real exit is preserved, not borrowed and not invented.
        # (SQLite hands back a naive datetime; compare the instant, not tzinfo.)
        assert row.departure_provenance == prov.value
        assert row.departure_at is not None
        assert row.departure_at.replace(tzinfo=UTC) == _at(DAY, 17)
        assert row.departure_source_ref is not None
        # And it is still not a verified departure.
        from app.work_ledger.states import VERIFIED_DEPARTURE_PROVENANCE
        assert row.departure_provenance not in {
            p.value for p in VERIFIED_DEPARTURE_PROVENANCE}
        # §19 step 5: the day carries its real sealed intervals.
        assert db.query(WorkStateInterval).count() >= 1

    def test_the_synthesized_origin_survives_the_refusal(self, db):
        """The producer refuses; D-15 keeps the origin permanently. Nothing is
        rewritten to WORKER."""
        from app.clock_session_boundary import ClockSessionBoundaryEvent
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _run(db)
        closes = (db.query(ClockSessionBoundaryEvent)
                  .filter(ClockSessionBoundaryEvent.boundary
                          == BOUNDARY_CLOCK_OUT).all())
        assert [e.provenance for e in closes] == ["STALE_AUTOCLOSE"]

    def test_an_open_session_after_closure_is_missing_clock_out(self, db):
        """Owner Ruling 5: past the closure point, an open session IS §24.6.

        This previously expected a bare SESSION_STILL_OPEN refusal. The D18-F3
        closure gate still runs first and still decides WHEN the question may
        be asked — ``DAY`` is in the past, so the local day has ended and the
        Worker demonstrably never clocked out. That is a missing clock-out, not
        a Worker who is still working.
        """
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=None)
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.MISSING_CLOCK_OUT.value
        assert row.departure_at is None
        assert db.query(WorkStateInterval).count() == 0

    def test_the_evidence_layer_still_refuses_an_open_session(self, db):
        """SESSION_OPEN remains the evidence-level refusal, reachable directly.
        Both guards exist; the closure gate simply gets there first."""
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=None)
        assert resolve_session_evidence(
            db, session=s, organization_id=ORG, user_id=WORKER
        ) is RefusalReason.SESSION_OPEN

    def test_the_producer_transports_missing_clock_out_but_never_decides_it(
            self, db):
        """Owner Ruling 7 authorises the transport; Ruling 2 keeps the decision out.

        This used to assert the producer could not emit MISSING_CLOCK_OUT at
        all. It now can — but only by carrying a verdict formed elsewhere, the
        same way it already carries the classifier's working-day verdict. The
        claim therefore inverts in outcome and HOLDS in principle, which is
        what D2-A/D13-A actually protect.

        Asserted structurally: the producer must call the departure primitive
        and must not re-derive the condition itself. A module that decided this
        for itself would inspect provenance sets here — it does not.
        """
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=None)
        _run(db)
        states = {r.state for r in db.query(WorkDayLedger).all()}
        assert LedgerState.MISSING_CLOCK_OUT.value in states

        src = pathlib.Path(
            __import__("app.work_ledger_producer.producer",
                       fromlist=["x"]).__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        called = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "resolve_departure" in called, (
            "the producer no longer asks the departure primitive — it would be "
            "deciding the verdict itself, which D2-A and Ruling 2 forbid")
        # The decision inputs stay in the primitive: the producer never reads
        # the provenance vocabulary to work this out for itself.
        assert "SYNTHESIZED_CLOSURE_PROVENANCE" not in src
        assert "VERIFIED_DEPARTURE_PROVENANCE" not in src


# ═════════════════════════════════════════════════════════════════════════════
# Interval construction: WRK, break gaps, OFF
# ═════════════════════════════════════════════════════════════════════════════
class TestIntervalConstruction:
    def test_a_plain_session_is_one_wrk_interval(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        ivs = _intervals(db, result.ledger_id)
        assert len(ivs) == 1
        assert ivs[0].state == WorkState.WRK.value
        assert ivs[0].duration_seconds == 8 * 3600
        assert ivs[0].source_ref.startswith("clock_session:")

    def test_a_break_splits_wrk_and_leaves_a_gap(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13), btype="lunch")
        result = _run(db)
        ivs = _intervals(db, result.ledger_id)

        assert [i.state for i in ivs] == [WorkState.WRK.value] * 2
        assert ivs[0].duration_seconds == 3 * 3600
        assert ivs[1].duration_seconds == 4 * 3600
        # The break period itself is a GAP: no interval covers it.
        assert ivs[0].end_at_utc != ivs[1].start_at_utc

    def test_the_split_boundary_carries_producer_derived(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13))
        result = _run(db)
        ivs = _intervals(db, result.ledger_id)
        # First ends at a producer-authored carve-out; second at the departure.
        assert ivs[0].provenance == "PRODUCER_DERIVED"
        assert ivs[1].provenance == "WORKER"

    def test_no_break_state_is_ever_emitted(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13), btype="lunch")
        _break(db, s, _at(DAY, 15), _at(DAY, 15, 15), btype="break")
        result = _run(db)
        states = {i.state for i in _intervals(db, result.ledger_id)}
        assert WorkState.BRK_M.value not in states
        assert WorkState.BRK_R.value not in states
        assert states == {WorkState.WRK.value}

    def test_the_worker_selected_break_label_is_never_read(self, db):
        """`lunch` and `break` are UI values with no legal meaning here."""
        code = _code_only("evidence")
        assert "break_type" not in code
        assert "BRK_M" not in code and "BRK_R" not in code

    def test_an_offline_window_becomes_off_and_splits_wrk(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        result = _run(db)
        ivs = _intervals(db, result.ledger_id)

        assert [i.state for i in ivs] == [
            WorkState.WRK.value, WorkState.OFF.value, WorkState.WRK.value]
        off = ivs[1]
        assert off.provenance == "PRODUCER_DERIVED"
        assert off.duration_seconds == 1800
        assert off.source_ref == f"offline_interval:{_only_offline(db).id}"
        # No gap: OFF abuts the WRK either side of it.
        assert ivs[0].end_at_utc == off.start_at_utc
        assert off.end_at_utc == ivs[2].start_at_utc

    def test_off_uses_the_applied_window_and_ignores_the_counters(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))   # claimed 99999s
        result = _run(db)
        off = [i for i in _intervals(db, result.ledger_id)
               if i.state == WorkState.OFF.value][0]
        assert off.duration_seconds == 1800          # not 99999, not 88888

    def test_cached_totals_are_never_read(self, db):
        """The fixture sets total_seconds=999999, break_seconds=888 and
        offline_seconds=777. None may influence the evidence."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        ivs = _intervals(db, result.ledger_id)
        assert sum(i.duration_seconds for i in ivs) == 8 * 3600

        banned = ("total_seconds", "break_seconds", "offline_seconds",
                  "claimed_seconds", "applied_seconds", "capped",
                  "cap_reason", "is_active")
        for module in ("evidence", "producer", "context"):
            tree = ast.parse(_code_only(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr not in banned:
                    continue
                receiver = ast.unparse(node.value)
                # `timedelta.total_seconds()` on the resolved UTC offset is a
                # different thing entirely from a cached column of the same
                # name; the receiver is what distinguishes them.
                if node.attr == "total_seconds" and (
                        "offset" in receiver or receiver.endswith(")")):
                    continue
                raise AssertionError(
                    f"{module} reads {receiver}.{node.attr}")

    def test_multiple_sessions_in_one_local_day(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 12))
        _session(db, clock_in=_at(DAY, 14), clock_out=_at(DAY, 17))
        result = _run(db)
        ivs = _intervals(db, result.ledger_id)
        assert [i.duration_seconds for i in ivs] == [3 * 3600, 3 * 3600]
        assert [i.seq for i in ivs] == [1, 2]

    def test_a_session_spanning_local_midnight_is_attributed_to_its_start(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 22),
                 clock_out=_at(DAY + timedelta(days=1), 6))
        result = _run(db)
        assert result.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.work_date.date() == DAY          # START_DATE attribution
        ivs = _intervals(db, result.ledger_id)
        assert ivs[0].duration_seconds == 8 * 3600

    def test_no_forbidden_state_is_ever_emitted(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13))
        _offline(db, s, _at(DAY, 15), _at(DAY, 15, 10))
        result = _run(db)
        states = {i.state for i in _intervals(db, result.ledger_id)}
        assert states <= {WorkState.WRK.value, WorkState.OFF.value}
        for banned in (WorkState.TRV, WorkState.ONC, WorkState.LVE,
                       WorkState.UNK, WorkState.BRK_M, WorkState.BRK_R):
            assert banned.value not in states


def _only_offline(db):
    return db.query(dbm.OfflineInterval).one()


# ═════════════════════════════════════════════════════════════════════════════
# Refusals: malformed, ambiguous, mismatched
# ═════════════════════════════════════════════════════════════════════════════
class TestRefusals:
    def test_an_open_break_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), None)
        result = _run(db)
        assert result.refusal is RefusalReason.BREAK_OPEN
        assert db.query(WorkDayLedger).count() == 0

    def test_a_non_positive_break_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 12))
        assert _run(db).refusal is RefusalReason.BREAK_MALFORMED

    def test_a_break_outside_its_session_refuses_and_is_not_clamped(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 8), _at(DAY, 10))
        assert _run(db).refusal is RefusalReason.BREAK_MALFORMED
        assert db.query(WorkDayLedger).count() == 0

    def test_overlapping_breaks_are_refused_not_unioned(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13))
        _break(db, s, _at(DAY, 12, 30), _at(DAY, 13, 30))
        assert _run(db).refusal is RefusalReason.BREAK_MALFORMED

    def test_a_malformed_offline_window_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _offline(db, s, _at(DAY, 12), _at(DAY, 11))
        assert _run(db).refusal is RefusalReason.OFFLINE_MALFORMED

    def test_an_offline_window_outside_the_session_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _offline(db, s, _at(DAY, 17, 30), _at(DAY, 18))
        assert _run(db).refusal is RefusalReason.OFFLINE_MALFORMED

    def test_a_break_overlapping_an_offline_window_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13))
        _offline(db, s, _at(DAY, 12, 30), _at(DAY, 12, 45))
        assert _run(db).refusal is RefusalReason.OFFLINE_MALFORMED

    def test_a_cross_worker_break_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13), user_id=OTHER)
        assert _run(db).refusal is RefusalReason.CROSS_WORKER_MISMATCH

    def test_a_cross_worker_session_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                     user_id=OTHER, actor="o@example.test")
        outcome = resolve_session_evidence(
            db, session=s, organization_id=ORG, user_id=WORKER)
        assert outcome is RefusalReason.CROSS_WORKER_MISMATCH

    def test_a_cross_org_session_refuses(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        outcome = resolve_session_evidence(
            db, session=s, organization_id=OTHER_ORG, user_id=WORKER)
        assert outcome is RefusalReason.CROSS_ORG_MISMATCH

    def test_a_worker_from_another_org_refuses(self, db):
        _configure(db)
        assert _run(db, organization_id=OTHER_ORG).refusal is \
            RefusalReason.CROSS_ORG_MISMATCH

    def test_a_day_with_no_session_is_source_unresolved(self, db):
        _configure(db)
        result = _run(db)
        assert result.outcome is ProducerOutcome.SOURCE_UNRESOLVED
        assert result.refusal is None
        assert db.query(WorkDayLedger).count() == 0

    def test_overlapping_sessions_produce_an_integrity_fault(self, db):
        """Two sessions overlapping in time yield overlapping canonical
        intervals. Nothing is merged or truncated: the day is refused and the
        fault recorded."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 13))
        _session(db, clock_in=_at(DAY, 12), clock_out=_at(DAY, 17))
        result = _run(db)
        assert result.outcome is ProducerOutcome.LEDGER_INTEGRITY_FAULT
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.LEDGER_INTEGRITY_FAULT.value
        assert "OVERLAP" in (row.integrity_fault_reason or "").upper()
        assert db.query(WorkStateInterval).count() == 0

    def test_a_naive_candidate_boundary_is_refused_by_the_ledger(
            self, db, monkeypatch):
        """The awareness guard is `validate_intervals`, and it is wired: a
        naive boundary reaching the seal is a fault, not a coerced value."""
        from app.work_ledger.integrity import CandidateInterval
        from app.work_ledger_producer import producer as prod
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))

        monkeypatch.setattr(prod, "build_intervals", lambda ev: [
            CandidateInterval(
                seq=1, state=WorkState.WRK.value,
                start_at_utc=datetime(2026, 9, 14, 9, 0),   # naive
                end_at_utc=datetime(2026, 9, 14, 17, 0),
                provenance="WORKER", source_ref="x"),
        ])
        result = _run(db)
        assert result.outcome is ProducerOutcome.LEDGER_INTEGRITY_FAULT
        assert "NAIVE" in (result.detail or "").upper()

    def test_nothing_is_repaired_clamped_or_unioned(self):
        for module in ("evidence", "producer"):
            src = pathlib.Path(
                __import__(f"app.work_ledger_producer.{module}",
                           fromlist=["x"]).__file__).read_text(encoding="utf-8")
            code = "\n".join(l for l in src.splitlines()
                             if not l.strip().startswith("#"))
            body = code.split('"""', 2)[-1]
            for banned in ("min(", "union", "clamp", "truncate"):
                assert banned not in body, f"{module} contains {banned!r}"


# ═════════════════════════════════════════════════════════════════════════════
# Sealing, idempotency, immutability
# ═════════════════════════════════════════════════════════════════════════════
class TestSealingAndIdempotency:
    def test_it_seals_through_the_existing_service(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = _run(db)
        row = db.query(WorkDayLedger).one()
        assert row.ledger_version == 1
        assert row.state == LedgerState.SEALED.value
        assert row.created_by == "WORK_LEDGER_PRODUCER"
        assert row.sealed_at is not None
        assert row.workweek_key                      # derived by seal_day
        assert result.ledger_id == row.id

    def test_a_repeat_run_is_idempotent(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        first = _run(db)
        db.commit()
        second = _run(db)
        db.commit()

        assert first.outcome is ProducerOutcome.SEALED
        assert second.outcome is ProducerOutcome.ALREADY_SEALED
        assert second.ledger_id == first.ledger_id
        assert db.query(WorkDayLedger).count() == 1
        assert db.query(WorkStateInterval).count() == 1

    def test_a_repeat_run_does_not_create_a_new_version(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _run(db)
        db.commit()
        for _ in range(3):
            _run(db)
            db.commit()
        assert [r.ledger_version for r in db.query(WorkDayLedger).all()] == [1]

    def test_changed_source_evidence_supersedes(self, db):
        """D18-F3-S Choice C: source changed, frozen context identical."""
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        first = _run(db)
        db.commit()

        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        result = _run(db)
        db.commit()
        assert result.outcome is ProducerOutcome.SUPERSEDED
        assert result.superseded_from_version == 1
        assert result.ledger_version == 2
        assert db.query(WorkDayLedger).count() == 2
        old = db.get(WorkDayLedger, first.ledger_id)
        assert old.superseded_by == result.ledger_id

    def test_a_sealed_ledger_is_not_mutated_by_a_later_run(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        first = _run(db)
        db.commit()
        before = [(i.seq, i.state, i.provenance, i.duration_seconds)
                  for i in _intervals(db, first.ledger_id)]
        _run(db)
        db.commit()
        assert [(i.seq, i.state, i.provenance, i.duration_seconds)
                for i in _intervals(db, first.ledger_id)] == before

    def test_supersession_uses_the_existing_mechanism(self, db):
        """The producer introduces no second versioning system."""
        from app.work_ledger_producer import producer as prod
        src = pathlib.Path(prod.__file__).read_text(encoding="utf-8")
        assert "from app.work_ledger.service import" in src
        assert "supersede_day" in src

    def test_a_refused_day_leaves_no_partial_ledger(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), None)          # open break -> refuse
        result = _run(db)
        assert result.outcome is ProducerOutcome.REFUSED
        assert db.query(WorkDayLedger).count() == 0
        assert db.query(WorkStateInterval).count() == 0

    def test_the_producer_does_not_commit(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _run(db)
        db.rollback()
        assert db.query(WorkDayLedger).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# The producer boundary itself
# ═════════════════════════════════════════════════════════════════════════════
class TestProducerBoundary:
    def test_no_api_accepts_candidate_intervals(self):
        import inspect
        import app.work_ledger_producer as pkg

        for name in pkg.__all__:
            obj = getattr(pkg, name)
            if not callable(obj) or isinstance(obj, type):
                continue
            params = inspect.signature(obj).parameters
            assert "intervals" not in params, name
            assert "candidate_intervals" not in params, name

    def test_the_producer_owns_interval_construction(self):
        from app.work_ledger_producer import build_intervals
        import inspect
        params = inspect.signature(build_intervals).parameters
        assert list(params) == ["evidence"]

    def test_no_backfill_path_exists(self):
        """No 'rebuild all days', no startup sweep, no migration invocation."""
        import app.work_ledger_producer as pkg
        for name in pkg.__all__:
            low = name.lower()
            for banned in ("backfill", "rebuild", "replay", "sweep",
                           "all_days", "historical"):
                assert banned not in low, name

        # The migration must not invoke the producer: no import of it, and no
        # call. It names the cutover TABLE, which is not the same thing.
        body = MIGRATION.read_text(encoding="utf-8")
        assert "produce_and_seal" not in body
        tree = ast.parse(body)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "work_ledger_producer" not in node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "work_ledger_producer" not in alias.name

    def test_the_producer_is_wired_by_exactly_one_caller(self):
        """This asserted "nothing wires the producer" until the driver existed.

        That was the right claim while the producer had no authorized
        invocation, and keeping it would now mean either a false claim or
        refusing the P0 calculation driver -- whose entire purpose is to be
        that invocation, because §44.2's shadow period cannot produce data
        without one.

        The property actually worth protecting is preserved and made stricter:
        there is EXACTLY ONE caller, it is ``app/eco_driver``, and nothing
        schedules it. A second caller, a route, or a startup task fails here.

        Asserted by AST CALL scan rather than substring: ``eco_driver`` names
        this function in prose to explain what it orchestrates, and a text scan
        would flag the sentence that documents the wiring.
        """
        root = BACKEND / "app"
        callers = []
        for path in list(root.rglob("*.py")) + [BACKEND / "main.py"]:
            if "work_ledger_producer" in path.parts:
                continue
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                      for n in ast.walk(tree) if isinstance(n, ast.Call)}
            if "produce_and_seal_work_day" in called:
                callers.append(path.relative_to(BACKEND).as_posix())
        assert callers == ["app/eco_driver/service.py"], callers

        # And nothing schedules THAT: no lifespan hook, no background task.
        main = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
        main_names = {getattr(n, "id", None) or getattr(n, "attr", None)
                      for n in ast.walk(main)}
        assert "run_eco_calculation_pass" not in main_names

    def test_no_later_eco_phase_is_touched(self):
        import app.work_ledger_producer as pkg
        src_dir = pathlib.Path(pkg.__file__).parent
        for path in src_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
            for banned in ("app.eval_day", "app.eval_week", "app.early_clock_out",
                           "app.notifications", "app.rollout",
                           "app.leave_intervals", "app.eco_threshold"):
                assert not any(m == banned or m.startswith(banned + ".")
                               for m in imported), f"{path.name} -> {banned}"

    def test_time_adjustment_is_never_read(self):
        import app.work_ledger_producer as pkg
        src_dir = pathlib.Path(pkg.__file__).parent
        for path in src_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            code = "\n".join(l for l in text.splitlines()
                             if not l.strip().startswith("#"))
            body = code.split('"""', 2)[-1]
            assert "TimeAdjustment" not in body, path.name
            assert "ShiftAssignment" not in body, path.name
            assert "LeaveInputSnapshot" not in body, path.name

    def test_the_dead_d15_writer_remains_guarded(self):
        """The producer must not have quietly mounted app.timesheets."""
        root = BACKEND / "app"
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or "timesheets" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mod = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                elif isinstance(node, ast.Import):
                    mod = ",".join(a.name for a in node.names)
                if mod:
                    assert "timesheets" not in mod, path.name


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════
class TestMigration:
    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location("d18_mig", MIGRATION)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_it_is_a_single_parent_child_of_the_d15_head(self):
        m = self._module()
        assert m.revision == "d5f1a8c3b724"
        assert m.down_revision == "c4e9b7d2a583"
        assert m.branch_labels is None and m.depends_on is None

    def test_the_graph_has_one_base_and_one_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        versions = BACKEND / "alembic" / "versions"
        # 91 since §24.15's D-9.1 added d9a4c7e35f18, the ECO platform
        # incident. It is chained onto the day-alert revision c2e8f5b71d94 and
        # creates one new table; it alters nothing this suite owns. Before it,
        # D-1.1's f4a7c2b91e68 (the §18 step-14 scheduled_end_local /
        # scheduled_end_source columns on this very table), P2-D4's
        # e2b6d4f89c35, MODEL-WORKWEEK's b2e6d9f04a17, ALERT's a8f4c2e91b07
        # and EVAL-EVENT's f3d8a1c65b29.
        assert len(list(versions.glob("*.py"))) == 94
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        # D-1.1's f4a7c2b91e68 is now the tip, chained onto P2-D4's
        # e2b6d4f89c35. Still one head and one base — the property that matters.
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_it_creates_no_rows_and_opens_no_connection(self):
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("INSERT", "bulk_insert", "create_engine",
                       "DATABASE_URL", "postgresql://", "sessionmaker",
                       "load_dotenv", "session.add"):
            assert banned not in body, banned

    def test_it_runs_and_reverses_offline(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        dbm.Organization.__table__.create(eng, checkfirst=True)
        m = self._module()
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                m.upgrade()
                m.upgrade()                       # idempotent
                names = sa.inspect(conn).get_table_names()
                assert "work_ledger_producer_cutover" in names
                assert conn.execute(sa.text(
                    "SELECT COUNT(*) FROM work_ledger_producer_cutover"
                )).scalar() == 0
                m.downgrade()
                assert "work_ledger_producer_cutover" not in \
                    sa.inspect(conn).get_table_names()

    def test_it_refuses_to_fabricate_a_working_day_for_existing_rows(self):
        """NOT NULL with no default cannot be added to a table holding rows
        without inventing a status for sealed history, which D-18D forbids. So
        it raises rather than backfilling."""
        body = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(body)
        up_src = ast.unparse(next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "upgrade"))
        # A loud refusal, driven by an actual row count — not a default.
        assert "RuntimeError" in up_src
        assert "COUNT(*)" in up_src
        up = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "upgrade")
        for node in ast.walk(up):
            if isinstance(node, ast.Call) and \
                    getattr(node.func, "attr", None) == "add_column":
                rendered = ast.unparse(node)
                assert "server_default" not in rendered

    def test_the_cutover_foreign_key_is_non_cascading(self):
        fk = list(WorkLedgerProducerCutoverHistory.__table__.c.organization_id
                  .foreign_keys)[0]
        assert fk.ondelete == "RESTRICT"

    def test_both_dialects_compile(self):
        from sqlalchemy.dialects import postgresql, sqlite
        from sqlalchemy.schema import CreateTable
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            ddl = str(CreateTable(
                WorkLedgerProducerCutoverHistory.__table__).compile(
                    dialect=dialect))
            assert "ON DELETE RESTRICT" in ddl
            # A history table, so NO unique constraint on organization_id.
            assert "UNIQUE" not in ddl.upper()


# ═════════════════════════════════════════════════════════════════════════════
# Observability
# ═════════════════════════════════════════════════════════════════════════════
class TestObservability:
    def test_every_outcome_is_distinguishable(self):
        assert {o.value for o in ProducerOutcome} == {
            "SEALED", "ALREADY_SEALED", "REFUSED", "LEDGER_INTEGRITY_FAULT",
            "SOURCE_UNRESOLVED", "SUPERSEDED",
        }

    def test_a_refusal_must_carry_a_reason(self):
        from app.work_ledger_producer.outcomes import ProducerResult
        with pytest.raises(ValueError):
            ProducerResult(outcome=ProducerOutcome.REFUSED,
                           organization_id=ORG, user_id=WORKER, work_date=DAY)

    def test_a_refusal_cannot_carry_a_ledger_id(self):
        from app.work_ledger_producer.outcomes import ProducerResult
        with pytest.raises(ValueError):
            ProducerResult(outcome=ProducerOutcome.REFUSED,
                           organization_id=ORG, user_id=WORKER, work_date=DAY,
                           refusal=RefusalReason.SESSION_OPEN, ledger_id=1)

    def test_a_seal_reports_its_frozen_context_and_sources(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        result = _run(db)
        assert result.interval_counts == {"WRK": 2, "OFF": 1}
        assert any(r.startswith("offline_interval:") for r in result.source_refs)
        assert any(r.startswith("clock_session:") for r in result.source_refs)
        assert result.producer_cutover_at == CUTOVER
        assert result.timezone_iana == TZ_IST
        assert result.working_day_source == "org"

    def test_no_user_facing_eco_outcome_or_notification_is_produced(self):
        import app.work_ledger_producer as pkg
        src_dir = pathlib.Path(pkg.__file__).parent
        for path in src_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            code = "\n".join(l for l in text.splitlines()
                             if not l.strip().startswith("#"))
            body = code.split('"""', 2)[-1]
            for banned in ("send_", "notify", "email", "SHORTFALL",
                           "EarlyClockOut", "threshold"):
                assert banned not in body, f"{path.name}: {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# D18-F1 — the cutover watermark cannot be lowered by ANY route
# ═════════════════════════════════════════════════════════════════════════════
# These are the tests whose absence let the original bypass ship. Each one
# exercises a route that does NOT pass through the ORM listeners.

EARLIER = CUTOVER - timedelta(days=90)


class TestCutoverCannotBeLowered:
    @pytest.fixture()
    def marked(self, db):
        set_producer_cutover(db, organization_id=ORG, cutover_at=CUTOVER)
        db.commit()
        assert get_producer_cutover(db, ORG) == CUTOVER
        return db

    def _table(self):
        return WorkLedgerProducerCutoverHistory.__table__

    def test_core_update_is_refused_by_the_database(self, marked):
        with pytest.raises(Exception):
            marked.execute(
                sa.update(self._table())
                .where(self._table().c.organization_id == ORG)
                .values(producer_cutover_at=EARLIER))
            marked.flush()
        marked.rollback()
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_core_delete_is_refused_by_the_database(self, marked):
        with pytest.raises(Exception):
            marked.execute(
                sa.delete(self._table())
                .where(self._table().c.organization_id == ORG))
            marked.flush()
        marked.rollback()
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_orm_bulk_update_is_refused(self, marked):
        with pytest.raises(Exception):
            marked.query(WorkLedgerProducerCutoverHistory).filter(
                WorkLedgerProducerCutoverHistory.organization_id == ORG
            ).update({"producer_cutover_at": EARLIER},
                     synchronize_session=False)
            marked.flush()
        marked.rollback()
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_orm_delete_is_refused(self, marked):
        with pytest.raises(Exception):
            marked.delete(
                marked.query(WorkLedgerProducerCutoverHistory).one())
            marked.flush()
        marked.rollback()
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_raw_sql_update_is_refused(self, marked):
        with pytest.raises(Exception):
            marked.execute(sa.text(
                f"UPDATE {CUTOVER_HISTORY_TABLE} SET producer_cutover_at = :v"),
                {"v": EARLIER})
        marked.rollback()
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_raw_sql_delete_is_refused(self, marked):
        with pytest.raises(Exception):
            marked.execute(sa.text(f"DELETE FROM {CUTOVER_HISTORY_TABLE}"))
        marked.rollback()
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_delete_then_recreate_is_impossible(self, marked):
        """The route that defeated the mutable row. DELETE is refused, so the
        history cannot be emptied and re-seeded lower."""
        with pytest.raises(Exception):
            marked.execute(sa.text(f"DELETE FROM {CUTOVER_HISTORY_TABLE}"))
        marked.rollback()
        with pytest.raises(ProducerCutoverMovedBackward):
            set_producer_cutover(marked, organization_id=ORG,
                                 cutover_at=EARLIER)
        marked.rollback()
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_an_earlier_row_cannot_lower_the_maximum(self, marked, fk_engine):
        """Monotonic BY CONSTRUCTION: even if an earlier row somehow exists,
        MAX() is unaffected — which is why B was chosen over a mutable row."""
        marked.execute(sa.text(
            f"INSERT INTO {CUTOVER_HISTORY_TABLE} "
            f"(organization_id, producer_cutover_at, set_at, set_by) "
            f"VALUES (:o, :v, :v, 'SMUGGLED')"),
            {"o": ORG, "v": EARLIER})
        marked.commit()
        assert marked.query(WorkLedgerProducerCutoverHistory).count() == 2
        assert get_producer_cutover(marked, ORG) == CUTOVER

    def test_the_service_still_refuses_an_earlier_target(self, marked):
        with pytest.raises(ProducerCutoverMovedBackward):
            set_producer_cutover(marked, organization_id=ORG,
                                 cutover_at=EARLIER)
        marked.rollback()


class TestAppendOnlyEnforcementIsInstalled:
    def test_create_all_installs_both_sqlite_triggers(self, fk_engine):
        with fk_engine.connect() as conn:
            names = {r[0] for r in conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"wl_cutover_history_no_update",
                "wl_cutover_history_no_delete"} <= names

    def test_the_migration_installs_both_sqlite_triggers(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        dbm.Organization.__table__.create(eng, checkfirst=True)
        module = _correction_migration()
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()
                names = {r[0] for r in conn.execute(sa.text(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"wl_cutover_history_no_update",
                "wl_cutover_history_no_delete"} <= names

    def test_both_emitters_use_the_one_shared_constant(self):
        """The ORM hook and the migration must not carry separate copies: that
        is how a migrated schema and a create_all schema diverge."""
        from app.work_ledger_producer import append_only_ddl, models as pm
        mig_code = _strip_docstrings(
            _CORRECTION_MIGRATION.read_text(encoding="utf-8"))
        assert "append_only_statements" in mig_code
        # Not typed out again: the prose may DISCUSS triggers, the code may not
        # define them.
        assert "CREATE TRIGGER" not in mig_code
        model_code = _strip_docstrings(
            pathlib.Path(pm.__file__).read_text(encoding="utf-8"))
        assert "append_only_statements" in model_code
        assert "CREATE TRIGGER" not in model_code
        # And the constant module is the single definition.
        ddl_src = pathlib.Path(append_only_ddl.__file__).read_text(
            encoding="utf-8")
        assert ddl_src.count("CREATE TRIGGER") == 4   # 2 sqlite + 2 postgres

    def test_the_ddl_module_imports_nothing(self):
        """It is imported by a migration, so it must not drag in the ORM."""
        from app.work_ledger_producer import append_only_ddl
        tree = ast.parse(pathlib.Path(append_only_ddl.__file__).read_text(
            encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert imported <= {"__future__"}, imported

    def test_only_the_two_supported_dialects_are_covered(self):
        assert SUPPORTED_DIALECTS == {"sqlite", "postgresql"}
        assert append_only_statements("mysql") == ()

    def test_the_postgresql_ddl_is_well_formed(self):
        """COMPILATION/SHAPE ONLY. No PostgreSQL is reachable in this
        environment and connecting is prohibited, so the runtime behaviour of
        these statements is NOT verified here — see the increment report."""
        stmts = append_only_statements("postgresql")
        assert len(stmts) == 3
        assert "LANGUAGE plpgsql" in stmts[0]
        assert "RAISE EXCEPTION" in stmts[0]
        assert "BEFORE UPDATE" in stmts[1]
        assert "BEFORE DELETE" in stmts[2]
        for s in stmts[1:]:
            assert "FOR EACH ROW EXECUTE FUNCTION" in s
            assert CUTOVER_HISTORY_TABLE in s


# ═════════════════════════════════════════════════════════════════════════════
# D18-F2 — the verified departure survives every carve-out shape
# ═════════════════════════════════════════════════════════════════════════════

def _summary(db, ledger_id):
    from app.work_ledger.provenance import summarise_ledger_provenance
    return summarise_ledger_provenance(db, db.get(WorkDayLedger, ledger_id))


class TestVerifiedDepartureSurvives:
    """The three shapes the audit found sealing with no verified departure."""

    def test_a_normal_close_records_the_departure(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        r = _run(db)
        row = db.query(WorkDayLedger).one()
        assert row.departure_provenance == "WORKER"
        assert row.departure_at.replace(tzinfo=UTC) == _at(DAY, 17)
        assert row.departure_source_ref.startswith("clock_session:")
        assert "/boundary_event:" in row.departure_source_ref
        assert _summary(db, r.ledger_id).has_verified_departure_evidence

    def test_a_break_flush_to_the_close_keeps_the_departure(self, db):
        """Audit case B: the final WRK segment is zero-length and absent."""
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 16), _at(DAY, 17))
        r = _run(db)
        ivs = _intervals(db, r.ledger_id)
        assert [i.provenance for i in ivs] == ["PRODUCER_DERIVED"]
        assert _summary(db, r.ledger_id).has_verified_departure_evidence
        assert db.query(WorkDayLedger).one().departure_provenance == "WORKER"

    def test_an_offline_flush_to_the_close_keeps_the_departure(self, db):
        """Audit case C."""
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _offline(db, s, _at(DAY, 16), _at(DAY, 17))
        r = _run(db)
        ivs = _intervals(db, r.ledger_id)
        assert {i.provenance for i in ivs} == {"PRODUCER_DERIVED"}
        assert _summary(db, r.ledger_id).has_verified_departure_evidence

    def test_a_full_session_break_keeps_the_departure(self, db):
        """Audit case A: ZERO intervals, yet a verified clock-out existed."""
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 9), _at(DAY, 17))
        r = _run(db)
        assert r.outcome is ProducerOutcome.SEALED
        assert _intervals(db, r.ledger_id) == []
        assert _summary(db, r.ledger_id).has_verified_departure_evidence
        assert db.query(WorkDayLedger).one().departure_provenance == "WORKER"

    def test_multiple_sessions_record_the_LATEST_departure(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 12))
        _session(db, clock_in=_at(DAY, 14), clock_out=_at(DAY, 17))
        _run(db)
        row = db.query(WorkDayLedger).one()
        assert row.departure_at.replace(tzinfo=UTC) == _at(DAY, 17)

    def test_an_admin_close_records_admin_override(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                 close_prov=Provenance.ADMIN_OVERRIDE, actor="a@example.test")
        _run(db)
        assert db.query(WorkDayLedger).one().departure_provenance \
            == "ADMIN_OVERRIDE"

    def test_a_synthesized_close_records_its_own_provenance(self, db):
        """It records a day now — carrying the synthesized origin, unaltered."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        assert _run(db).outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.departure_provenance == Provenance.STALE_AUTOCLOSE.value

    def test_every_producer_sealed_row_carries_departure_evidence(self, db):
        """The producer invariant, over several shapes at once."""
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 12), _at(DAY, 13))
        _offline(db, s, _at(DAY, 15), _at(DAY, 15, 30))
        _run(db)
        for row in db.query(WorkDayLedger).all():
            if row.created_by == "WORK_LEDGER_PRODUCER" and \
                    row.state == LedgerState.SEALED.value:
                assert row.departure_at is not None
                assert row.departure_provenance in ("WORKER", "ADMIN_OVERRIDE")
                assert row.departure_source_ref is not None

    def test_an_integrity_fault_row_has_no_departure(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 13))
        _session(db, clock_in=_at(DAY, 12), clock_out=_at(DAY, 17))
        r = _run(db)
        assert r.outcome is ProducerOutcome.LEDGER_INTEGRITY_FAULT
        row = db.query(WorkDayLedger).one()
        assert row.departure_at is None
        assert row.departure_provenance is None
        assert row.departure_source_ref is None
        assert not _summary(db, row.id).has_verified_departure_evidence


class TestDepartureConstraints:
    def test_the_columns_are_nullable_with_no_defaults(self):
        for name in ("departure_at", "departure_provenance",
                     "departure_source_ref", "source_fingerprint"):
            col = WorkDayLedger.__table__.c[name]
            assert col.nullable is True, name
            assert col.default is None and col.server_default is None, name

    def test_producer_derived_is_rejected_as_a_departure(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _run(db)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "UPDATE work_day_ledger SET departure_provenance = "
                "'PRODUCER_DERIVED'"))
        db.rollback()

    def test_the_all_or_none_constraint_holds(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _run(db)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "UPDATE work_day_ledger SET departure_provenance = NULL"))
        db.rollback()

    def test_the_admissible_check_is_generated_from_the_vocabulary(self):
        """The generated-constraint mechanism survives the §24.7 widening.

        D18-F2's CHECK admitted VERIFIED provenance only. §24.7 needs an
        unverified exit to be RECORDED so the day can be sealed and deferred,
        so the storage set widened to verified + synthesized. What has not
        changed is that the SQL is generated from the enums, so the database
        and the vocabulary still cannot drift apart.
        """
        from app.work_ledger.models import (
            ADMISSIBLE_DEPARTURE_PROVENANCE,
            _ADMISSIBLE_DEPARTURE_SQL,
        )
        from app.work_ledger.states import (
            SYNTHESIZED_CLOSURE_PROVENANCE,
            VERIFIED_DEPARTURE_PROVENANCE,
        )

        assert ADMISSIBLE_DEPARTURE_PROVENANCE == (
            VERIFIED_DEPARTURE_PROVENANCE | SYNTHESIZED_CLOSURE_PROVENANCE)
        assert _ADMISSIBLE_DEPARTURE_SQL == ", ".join(
            f"'{p.value}'" for p in
            sorted(ADMISSIBLE_DEPARTURE_PROVENANCE, key=lambda x: x.value))
        # Admissible is NOT verified. The sets stay distinct, and no sixth
        # provenance value was invented to carry the new condition.
        assert VERIFIED_DEPARTURE_PROVENANCE != ADMISSIBLE_DEPARTURE_PROVENANCE
        assert len(ADMISSIBLE_DEPARTURE_PROVENANCE) == 5

    def test_a_sealed_departure_cannot_be_rewritten_in_place(self, db):
        from app.work_ledger.states import ProvenanceImmutable
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        r = _run(db)
        db.commit()
        row = db.get(WorkDayLedger, r.ledger_id)
        row.departure_provenance = "ADMIN_OVERRIDE"
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_summarise_provenance_stays_interval_only(self, db):
        """D18-F2 must not change the interval-level contract."""
        from app.work_ledger.provenance import summarise_provenance
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _break(db, s, _at(DAY, 16), _at(DAY, 17))
        r = _run(db)
        ivs = _intervals(db, r.ledger_id)
        interval_only = summarise_provenance(ivs)
        assert interval_only.day_departure_provenance is None
        assert interval_only.has_verified_departure_evidence is False
        # The ledger-level view sees the day-level fact.
        assert _summary(db, r.ledger_id).has_verified_departure_evidence
        # Interval counts are identical either way.
        assert _summary(db, r.ledger_id).interval_count == \
            interval_only.interval_count


# ═════════════════════════════════════════════════════════════════════════════
# D18-F3 — day closure
# ═════════════════════════════════════════════════════════════════════════════

class _FrozenClock:
    """A stand-in for `datetime` whose `now()` is fixed."""

    def __init__(self, instant):
        self._instant = instant

    def now(self, tz=None):
        return self._instant

    def __getattr__(self, name):
        import datetime as _dt
        return getattr(_dt.datetime, name)


class TestDayClosure:
    def _freeze(self, monkeypatch, instant):
        from app.work_ledger_producer import producer as prod
        monkeypatch.setattr(prod, "datetime", _FrozenClock(instant))

    def test_before_the_local_day_end_refuses(self, db, monkeypatch):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        self._freeze(monkeypatch, DAY_END_IST - timedelta(seconds=1))
        r = _run(db)
        assert r.refusal is RefusalReason.DAY_NOT_ENDED
        assert db.query(WorkDayLedger).count() == 0

    def test_exactly_at_the_local_day_end_is_permitted(self, db, monkeypatch):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        self._freeze(monkeypatch, DAY_END_IST)
        assert _run(db).outcome is ProducerOutcome.SEALED

    def test_after_the_local_day_end_is_permitted(self, db, monkeypatch):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        self._freeze(monkeypatch, DAY_END_IST + timedelta(hours=5))
        assert _run(db).outcome is ProducerOutcome.SEALED

    def test_a_future_day_refuses(self, db):
        """No frozen clock: a real future date must refuse on its own."""
        _configure(db, cutover=CUTOVER)
        future = date(2027, 6, 14)
        r = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=future)
        assert r.refusal is RefusalReason.DAY_NOT_ENDED
        assert db.query(WorkDayLedger).count() == 0

    def test_an_open_session_in_the_day_is_missing_clock_out(self, db):
        """One unresolved exit decides the whole day — it is not sealed around.

        The morning session closed properly; the afternoon one never did. The
        day is MISSING_CLOCK_OUT rather than a sealed half-day, for the same
        reason the producer refuses a day on one inadmissible session: a
        partial day is evidence that silently omits work.
        """
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 12))
        _session(db, clock_in=_at(DAY, 14), clock_out=None)
        r = _run(db)
        assert r.outcome is ProducerOutcome.SEALED
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.MISSING_CLOCK_OUT.value
        # The closed morning session was NOT sealed into intervals on its own.
        assert db.query(WorkStateInterval).count() == 0

    def test_an_open_session_from_an_earlier_day_refuses(self, db):
        """It could still be attributed to, or overlap, this day."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _session(db, clock_in=_at(DAY - timedelta(days=1), 22),
                 clock_out=None)
        assert _run(db).refusal is RefusalReason.SESSION_STILL_OPEN

    def test_an_open_session_AFTER_the_day_does_not_block_it(self, db):
        """`clock_in_time < day_end_utc` is the bound: a session opened after
        the day ended cannot belong to it."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _session(db, clock_in=_at(DAY + timedelta(days=1), 9), clock_out=None)
        assert _run(db).outcome is ProducerOutcome.SEALED

    def test_all_sessions_closed_is_permitted(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        assert _run(db).outcome is ProducerOutcome.SEALED

    def test_the_gate_uses_true_local_midnight_on_a_dst_day(self, db,
                                                            monkeypatch):
        """BST: the day ends at 23:00Z, not at UTC midnight."""
        from app import _tz
        bst_day = date(2026, 3, 30)
        _configure(db, tz=TZ_LONDON,
                   cutover=datetime(2025, 1, 1, tzinfo=UTC))
        _session(db, clock_in=_at(bst_day, 9, tz_name=TZ_LONDON),
                 clock_out=_at(bst_day, 17, tz_name=TZ_LONDON))
        _, day_end = _tz.local_day_bounds(db, ORG, bst_day)
        assert day_end == datetime(2026, 3, 30, 23, 0, tzinfo=UTC)

        self._freeze(monkeypatch, day_end - timedelta(seconds=1))
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=bst_day).refusal is RefusalReason.DAY_NOT_ENDED

        self._freeze(monkeypatch, day_end)
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=bst_day).outcome is ProducerOutcome.SEALED

    def test_the_gates_write_nothing_before_refusing(self, db, monkeypatch):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        self._freeze(monkeypatch, DAY_END_IST - timedelta(hours=1))
        _run(db)
        assert db.query(WorkDayLedger).count() == 0
        assert db.query(WorkStateInterval).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# D18-F3-S — bounded supersession (owner ruling Choice C)
# ═════════════════════════════════════════════════════════════════════════════

class TestSupersession:
    def _seal(self, db):
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        first = _run(db)
        db.commit()
        assert first.outcome is ProducerOutcome.SEALED
        return s, first

    def test_identical_source_and_context_is_already_sealed(self, db):
        _configure(db)
        self._seal(db)
        again = _run(db)
        db.commit()
        assert again.outcome is ProducerOutcome.ALREADY_SEALED
        assert again.superseded_from_version is None
        assert db.query(WorkDayLedger).count() == 1

    def test_source_changed_context_identical_supersedes(self, db):
        _configure(db)
        s, first = self._seal(db)
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        r = _run(db)
        db.commit()
        assert r.outcome is ProducerOutcome.SUPERSEDED
        assert r.superseded_from_version == 1
        assert r.ledger_version == 2

    def test_source_identical_context_changed_refuses(self, db):
        """Case 3. A timezone change alone may never supersede."""
        _configure(db)
        self._seal(db)
        row = db.query(dbm.OrgSettings).filter(
            dbm.OrgSettings.organization_id == ORG).one()
        row.default_timezone = "Asia/Katmandu"
        db.commit()
        from app import _tz
        _tz.invalidate_tz_cache()
        r = _run(db)
        assert r.outcome is ProducerOutcome.REFUSED
        assert r.refusal is RefusalReason.FROZEN_CONTEXT_DIVERGENCE
        assert db.query(WorkDayLedger).count() == 1
        assert db.query(WorkDayLedger).one().timezone_iana == TZ_IST

    def test_source_changed_and_context_changed_refuses(self, db):
        """Case 4. A source correction may not smuggle in a new context."""
        _configure(db)
        s, _ = self._seal(db)
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        wd = (db.query(dbm.AppSettings)
              .filter(dbm.AppSettings.key == K.SETTING_WORKING_DAYS).one())
        wd.value = json.dumps(["TUESDAY"])
        db.commit()
        r = _run(db)
        assert r.refusal is RefusalReason.FROZEN_CONTEXT_DIVERGENCE
        assert db.query(WorkDayLedger).count() == 1
        assert db.query(WorkDayLedger).one().is_working_day is True

    def test_a_working_day_source_change_alone_refuses(self, db):
        _configure(db)
        self._seal(db)
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=WORKER,
            key=K.SETTING_WORKING_DAYS, value=json.dumps(MON_FRI)))
        db.commit()
        r = _run(db)
        assert r.refusal is RefusalReason.FROZEN_CONTEXT_DIVERGENCE
        assert db.query(WorkDayLedger).one().working_day_source == "org"

    def test_the_prior_version_is_retained_and_linked(self, db):
        _configure(db)
        s, first = self._seal(db)
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        r = _run(db)
        db.commit()
        old = db.get(WorkDayLedger, first.ledger_id)
        new = db.get(WorkDayLedger, r.ledger_id)
        assert old is not None and old.ledger_version == 1
        assert old.superseded_by == new.id
        assert new.supersedes == old.id
        assert new.superseded_by is None
        assert _intervals(db, old.id)          # prior evidence still readable

    def test_a_stale_expected_version_raises(self, db):
        from app.work_ledger.service import StaleLedgerVersion, supersede_day
        from app.work_ledger.service import DayKey
        _configure(db)
        self._seal(db)
        with pytest.raises(StaleLedgerVersion):
            supersede_day(
                db, DayKey(organization_id=ORG, user_id=WORKER,
                           work_date=DAY),
                expected_version=99, intervals=[], timezone_iana=TZ_IST,
                is_working_day=True, working_day_source="org")
        db.rollback()

    def test_a_synthesized_closure_cannot_supersede(self, db):
        _configure(db)
        s, _ = self._seal(db)
        # A later admin correction turns the close into a synthesized one.
        append_boundary_event(
            db, session_id=s.id, user_id=WORKER,
            boundary=BOUNDARY_CLOCK_OUT, boundary_at=_at(DAY, 17),
            provenance=Provenance.STALE_AUTOCLOSE, actor_label="SYSTEM")
        db.commit()
        r = _run(db)
        assert r.refusal is RefusalReason.SESSION_SYNTHESIZED_CLOSURE
        assert db.query(WorkDayLedger).count() == 1

    def test_an_open_session_cannot_supersede(self, db):
        _configure(db)
        self._seal(db)
        _session(db, clock_in=_at(DAY, 14), clock_out=None)
        r = _run(db)
        assert r.refusal is RefusalReason.SESSION_STILL_OPEN
        assert db.query(WorkDayLedger).count() == 1

    def test_the_result_reports_both_versions(self, db):
        _configure(db)
        s, first = self._seal(db)
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        r = _run(db)
        assert (r.superseded_from_version, r.ledger_version) == (1, 2)
        assert r.sealed is True and r.has_ledger is True


class TestSourceFingerprint:
    def test_it_is_persisted_at_seal(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _run(db)
        fp = db.query(WorkDayLedger).one().source_fingerprint
        assert isinstance(fp, str) and len(fp) == 64

    def test_it_is_deterministic(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        ev = resolve_session_evidence(
            db, session=s, organization_id=ORG, user_id=WORKER)
        assert source_evidence_fingerprint([ev]) == \
            source_evidence_fingerprint([ev])

    def test_a_context_change_does_not_alter_it(self, db):
        """The whole point: the digest is source-only."""
        from app import _tz
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        ev = resolve_session_evidence(
            db, session=s, organization_id=ORG, user_id=WORKER)
        before = source_evidence_fingerprint([ev])

        row = db.query(dbm.OrgSettings).filter(
            dbm.OrgSettings.organization_id == ORG).one()
        row.default_timezone = "Asia/Katmandu"
        wd = (db.query(dbm.AppSettings)
              .filter(dbm.AppSettings.key == K.SETTING_WORKING_DAYS).one())
        wd.value = json.dumps(["TUESDAY"])
        db.commit()
        _tz.invalidate_tz_cache()

        ev2 = resolve_session_evidence(
            db, session=s, organization_id=ORG, user_id=WORKER)
        assert source_evidence_fingerprint([ev2]) == before

    def test_a_source_change_alters_it(self, db):
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        ev = resolve_session_evidence(
            db, session=s, organization_id=ORG, user_id=WORKER)
        before = source_evidence_fingerprint([ev])
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))
        ev2 = resolve_session_evidence(
            db, session=s, organization_id=ORG, user_id=WORKER)
        assert source_evidence_fingerprint([ev2]) != before

    def test_it_excludes_every_context_dimension(self):
        """Source-level assertion: the digest function must not read context."""
        src = _code_only("evidence")
        fn = src.split("def source_evidence_fingerprint")[1].split("\ndef ")[0]
        for banned in ("timezone_iana", "utc_offset", "is_working_day",
                       "working_day_source", "workweek", "day_start",
                       "day_end", "now("):
            assert banned not in fn, banned

    def test_it_is_immutable_once_sealed(self, db):
        from app.work_ledger.states import ProvenanceImmutable
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        r = _run(db)
        db.commit()
        row = db.get(WorkDayLedger, r.ledger_id)
        row.source_fingerprint = "0" * 64
        with pytest.raises(ProvenanceImmutable):
            db.flush()
        db.rollback()

    def test_it_is_distinct_from_the_interval_fingerprint(self):
        """Two different digests, for two different questions."""
        from app.work_ledger import service as svc
        # The interval fingerprint still exists, in the ledger service...
        assert hasattr(svc, "_evidence_fingerprint")
        # ...and the producer's source digest neither imports nor calls it.
        tree = ast.parse(_code_only("evidence"))
        called = {getattr(n.func, "attr", getattr(n.func, "id", None))
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "_evidence_fingerprint" not in called
        assert "_persisted_fingerprint" not in called


# ═════════════════════════════════════════════════════════════════════════════
# Finding 4 — the integrity-fault row is addressable
# ═════════════════════════════════════════════════════════════════════════════

class TestIntegrityFaultReference:
    def _fault(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 13))
        _session(db, clock_in=_at(DAY, 12), clock_out=_at(DAY, 17))
        return _run(db)

    def test_the_fault_row_is_created_and_returned(self, db):
        r = self._fault(db)
        assert r.outcome is ProducerOutcome.LEDGER_INTEGRITY_FAULT
        row = db.query(WorkDayLedger).one()
        assert row.state == LedgerState.LEDGER_INTEGRITY_FAULT.value
        assert r.integrity_fault_ledger_id == row.id

    def test_the_ledger_id_guard_is_not_weakened(self, db):
        r = self._fault(db)
        assert r.ledger_id is None
        assert r.ledger_version is None
        assert r.sealed is False
        assert r.has_ledger is False

    def test_no_intervals_are_written(self, db):
        self._fault(db)
        assert db.query(WorkStateInterval).count() == 0

    def test_only_a_fault_outcome_may_carry_the_reference(self):
        from app.work_ledger_producer.outcomes import ProducerResult
        with pytest.raises(ValueError):
            ProducerResult(outcome=ProducerOutcome.SOURCE_UNRESOLVED,
                           organization_id=ORG, user_id=WORKER, work_date=DAY,
                           integrity_fault_ledger_id=1)

    def test_superseded_must_report_the_prior_version(self):
        from app.work_ledger_producer.outcomes import ProducerResult
        with pytest.raises(ValueError):
            ProducerResult(outcome=ProducerOutcome.SUPERSEDED,
                           organization_id=ORG, user_id=WORKER, work_date=DAY,
                           ledger_id=1, ledger_version=2)
        with pytest.raises(ValueError):
            ProducerResult(outcome=ProducerOutcome.SEALED,
                           organization_id=ORG, user_id=WORKER, work_date=DAY,
                           ledger_id=1, ledger_version=1,
                           superseded_from_version=0)


def _correction_migration():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "d18_correction", _CORRECTION_MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CORRECTION_MIGRATION = (
    BACKEND / "alembic" / "versions"
    / "e7c2b9d4f815_d18_correction_append_only_cutover.py"
)


class TestCorrectionMigration:
    def test_it_is_a_single_parent_child_of_the_d18_head(self):
        m = _correction_migration()
        assert m.revision == "e7c2b9d4f815"
        assert m.down_revision == "d5f1a8c3b724"
        assert m.branch_labels is None and m.depends_on is None

    def test_it_creates_no_rows(self):
        body = _CORRECTION_MIGRATION.read_text(encoding="utf-8")
        for banned in ("INSERT", "bulk_insert", "create_engine",
                       "DATABASE_URL", "postgresql://", "sessionmaker",
                       "load_dotenv"):
            assert banned not in body, banned

    def test_it_refuses_to_convert_old_cutover_rows(self):
        """Converting a possibly-lowered mutable value into an append-only
        historical claim would fabricate provenance."""
        body = _CORRECTION_MIGRATION.read_text(encoding="utf-8")
        assert "RuntimeError" in body
        assert "COUNT(*)" in body
        assert "fabricate provenance" in body

    def test_it_runs_and_reverses_offline(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        dbm.Organization.__table__.create(eng, checkfirst=True)
        m = _correction_migration()
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                m.upgrade()
                m.upgrade()                    # idempotent
                assert CUTOVER_HISTORY_TABLE in \
                    sa.inspect(conn).get_table_names()
                m.downgrade()
                assert CUTOVER_HISTORY_TABLE not in \
                    sa.inspect(conn).get_table_names()

    def test_the_correction_migrations_verified_sql_is_still_its_own_moment(self):
        """e7c2b9d4f815 keeps the set that was correct WHEN IT RAN.

        This used to compare the D-18 correction migration's literal against
        the live model constant. That comparison broke the moment §24.7 widened
        the model's set, and mechanically "fixing" it would have been wrong:
        a migration must keep running against the schema of its own moment, so
        its literal is frozen history, not a mirror of today's vocabulary.

        What is still worth asserting is that it carries exactly the verified
        pair it was written with — the later widening is a LATER revision's
        job, and c3f7a1e58d2b does it.
        """
        from app.work_ledger.states import VERIFIED_DEPARTURE_PROVENANCE

        m = _correction_migration()
        assert m.VERIFIED_DEPARTURE_SQL == ", ".join(
            f"'{p.value}'" for p in
            sorted(VERIFIED_DEPARTURE_PROVENANCE, key=lambda x: x.value))


# ═════════════════════════════════════════════════════════════════════════════
# Audit Finding 1 — a NULL persisted source fingerprint fails CLOSED
# ═════════════════════════════════════════════════════════════════════════════
# Before the correction, `stored is None` fell through to SOURCE_CHANGED, so a
# sealed version the producer could not compare was SUPERSEDED — even when the
# source evidence and frozen context were byte-identical. D18-F3-S permits
# supersession only on a genuine DIFFERENCE, and an impossible comparison is
# not a difference.

class TestNullSourceFingerprintFailsClosed:
    def _seal_then_null(self, db):
        """Seal normally, then clear the fingerprint to simulate a version the
        producer cannot compare (one it did not author, or one predating the
        column). The fingerprint is CLEARED, never fabricated."""
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        first = _run(db)
        db.commit()
        assert first.outcome is ProducerOutcome.SEALED
        db.execute(sa.text(
            "UPDATE work_day_ledger SET source_fingerprint = NULL"))
        db.commit()
        db.expire_all()
        assert db.query(WorkDayLedger).one().source_fingerprint is None
        return s, first

    # ── Test 1 ──────────────────────────────────────────────────────────────
    def test_null_fingerprint_with_identical_source_and_context_refuses(self, db):
        _configure(db)
        _, first = self._seal_then_null(db)

        result = _run(db)
        db.commit()

        assert result.outcome is ProducerOutcome.REFUSED
        assert result.refusal is RefusalReason.SOURCE_FINGERPRINT_UNAVAILABLE
        # Explicitly NOT any of these.
        assert result.outcome is not ProducerOutcome.SUPERSEDED
        assert result.outcome is not ProducerOutcome.ALREADY_SEALED
        assert result.superseded_from_version is None
        assert result.ledger_id is None

        # Exactly one version, untouched, with no supersession lineage.
        rows = db.query(WorkDayLedger).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == first.ledger_id
        assert row.ledger_version == 1
        assert row.superseded_by is None
        assert row.supersedes is None
        assert row.source_fingerprint is None       # not silently upgraded
        assert row.state == LedgerState.SEALED.value

    # ── Test 2 ──────────────────────────────────────────────────────────────
    def test_null_fingerprint_with_changed_source_still_refuses(self, db):
        _configure(db)
        s, first = self._seal_then_null(db)
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))   # real source change

        result = _run(db)
        db.commit()

        assert result.outcome is ProducerOutcome.REFUSED
        assert result.refusal is RefusalReason.SOURCE_FINGERPRINT_UNAVAILABLE
        assert db.query(WorkDayLedger).count() == 1
        row = db.query(WorkDayLedger).one()
        assert row.ledger_version == 1 and row.superseded_by is None

    # ── Test 3 ──────────────────────────────────────────────────────────────
    def test_null_fingerprint_with_changed_context_reports_context_first(self, db):
        """Context-first ordering is unchanged: a divergent context refuses
        with its own reason, never the fingerprint one."""
        from app import _tz
        _configure(db)
        self._seal_then_null(db)
        row = db.query(dbm.OrgSettings).filter(
            dbm.OrgSettings.organization_id == ORG).one()
        row.default_timezone = "Asia/Katmandu"
        db.commit()
        _tz.invalidate_tz_cache()

        result = _run(db)
        db.commit()

        assert result.outcome is ProducerOutcome.REFUSED
        assert result.refusal is RefusalReason.FROZEN_CONTEXT_DIVERGENCE
        assert db.query(WorkDayLedger).count() == 1
        assert db.query(WorkDayLedger).one().timezone_iana == TZ_IST

    # ── Test 4 ──────────────────────────────────────────────────────────────
    def test_a_present_fingerprint_still_yields_already_sealed(self, db):
        """The correction must not disable the normal idempotent path."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        first = _run(db)
        db.commit()
        assert db.query(WorkDayLedger).one().source_fingerprint is not None

        again = _run(db)
        db.commit()
        assert again.outcome is ProducerOutcome.ALREADY_SEALED
        assert again.ledger_id == first.ledger_id
        assert db.query(WorkDayLedger).count() == 1

    # ── Test 5 ──────────────────────────────────────────────────────────────
    def test_a_present_fingerprint_that_differs_still_supersedes(self, db):
        """Legitimate supersession is untouched by the correction."""
        _configure(db)
        s = _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        first = _run(db)
        db.commit()
        _offline(db, s, _at(DAY, 11), _at(DAY, 11, 30))

        result = _run(db)
        db.commit()
        assert result.outcome is ProducerOutcome.SUPERSEDED
        assert result.superseded_from_version == 1
        assert result.ledger_version == 2
        old = db.get(WorkDayLedger, first.ledger_id)
        assert old.superseded_by == result.ledger_id

    # ── the decision function itself ────────────────────────────────────────
    def test_the_decision_table_covers_the_uncomparable_case(self, db):
        from app.work_ledger_producer.producer import (
            _Decision, _supersession_decision,
        )
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        _run(db)
        db.commit()
        row = db.query(WorkDayLedger).one()
        stored = row.source_fingerprint
        ctx = resolve_day_context(
            db, organization_id=ORG, user=db.get(dbm.User, WORKER),
            work_date=DAY)

        assert _supersession_decision(
            row, context=ctx, fingerprint=stored) is _Decision.IDENTICAL
        assert _supersession_decision(
            row, context=ctx, fingerprint="0" * 64) is _Decision.SOURCE_CHANGED

        row.source_fingerprint = None            # in-memory only, not flushed
        assert _supersession_decision(
            row, context=ctx,
            fingerprint=stored) is _Decision.FINGERPRINT_UNAVAILABLE
        assert _supersession_decision(
            row, context=ctx,
            fingerprint="0" * 64) is _Decision.FINGERPRINT_UNAVAILABLE
        db.rollback()

    def test_null_is_never_treated_as_an_empty_or_fabricated_hash(self):
        """No sentinel, no empty-string hash, no recompute-and-backdate."""
        src = _code_only("producer")
        fn = src.split("def _supersession_decision")[1].split("\ndef ")[0]
        assert "stored is None" in fn
        for banned in ('stored or ""', "stored = fingerprint", '""', "'.'",
                       "hashlib", "recompute"):
            assert banned not in fn, banned

    def test_the_reason_is_not_reused_from_source_changed(self):
        """A distinct, narrowly named reason — never SOURCE_CHANGED."""
        assert RefusalReason.SOURCE_FINGERPRINT_UNAVAILABLE.value == \
            "SOURCE_FINGERPRINT_UNAVAILABLE"
        assert not hasattr(RefusalReason, "SOURCE_CHANGED")
        # It is a ledger-state refusal, not a tenant-configuration one.
        from app.work_ledger_producer import CONFIGURATION_REFUSALS
        assert RefusalReason.SOURCE_FINGERPRINT_UNAVAILABLE \
            not in CONFIGURATION_REFUSALS
