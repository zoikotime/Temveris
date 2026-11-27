"""
EVAL-EVENT — the ECO threshold crossing lifecycle.
==================================================

The property under test: **a week crosses once, and the record of it cannot be
rewritten.**

Everything here goes through the real pipeline. Days are evaluated by EVAL-DAY
and persisted with ``record_evaluation``; the week is folded by EVAL-WEEK from
the persisted ledger; the threshold is read by ``app.eco_threshold`` from real
``ConfigVersion``/``ConfigValue`` rows. No stub supplies W, and no stub supplies
T — a lifecycle that only works against hand-made numbers has not been tested.

Literals are transcribed rather than imported wherever a wrong production
constant could otherwise agree with itself: the event names, the outcome names,
the sequence numbers and the uniqueness columns are all written out here.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.recipient_authority import EcoRecipientDesignation
from app.config_versioning import (
    ConfigValue,
    ConfigVersion,
    InMemoryConfigVersionProvider,
)
from app.eval_day import (
    EcoDayRecord,
    evaluate_day,
    record_evaluation,
)
from app.eval_event import (
    EcoThresholdEvent,
    EventMode,
    EventOutcome,
    EventType,
    RefusalReason,
    ProvenanceIncomplete,
    ThresholdEventImmutable,
    ThresholdEventResult,
    evaluate_threshold_event,
    read_events,
)
from app.eval_event.append_only_ddl import (
    APPEND_ONLY_DDL_BY_DIALECT,
    POSTGRES_APPEND_ONLY_DDL,
    SQLITE_APPEND_ONLY_DDL,
    SUPPORTED_DIALECTS,
)
from app.requirement_resolution import RequirementCandidate
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

ORG = 91
OTHER_ORG = 92
USER = 8300
OTHER_USER = 8400

#: 2026-08-31 is a Monday. The canonical key is the local workweek START DATE,
#: ISO-formatted (W4-3) — transcribed, never derived from current configuration.
WEEK = "2026-08-31"
OTHER_WEEK = "2026-09-07"
DAYS_IN_WEEK = 7

REQUIRED_MINUTES = 525

# ── The week shapes, and the arithmetic that produces them ───────────────────
# EVAL-DAY rounds QUALIFYING seconds once (GV-12, owner ruling Option A), then
# gross = max(required - qualifying, 0). Each figure below is worked out here so
# a change in the rounding locus fails loudly instead of quietly re-baselining.
#
#   short 600s -> worked 30900 -> qualifying (30930//60) 515 -> gross 10 -> W 50
#   short 360s -> worked 31140 -> qualifying (31170//60) 519 -> gross  6 -> W 30
#   short 120s -> worked 31380 -> qualifying (31410//60) 523 -> gross  2 -> W 10
SHORT_ABOVE, W_ABOVE = 600, 50
SHORT_EQUAL, W_EQUAL = 360, 30
SHORT_BELOW, W_BELOW = 120, 10

THRESHOLD = 30

#: Transcribed. §32: these names are a product surface and cannot be corrected
#: later without a breaking change.
REACHED = "ECO_THRESHOLD_REACHED"
WITHDRAWN = "ECO_THRESHOLD_WITHDRAWN"
PRIMARY = "PRIMARY"
OPERATIONAL, SHADOW = "OPERATIONAL", "SHADOW"

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eval_event"
MIGRATION = (BACKEND / "alembic" / "versions"
             / "f3d8a1c65b29_eco_threshold_event.py")
EXPECTED_REVISION = "f3d8a1c65b29"
EXPECTED_PARENT_REVISION = "e7c2b9d4f815"


# ═════════════════════════════════════════════════════════════════════════════
# Schema and seeding
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    EcoDayRecord.__table__.create(engine, checkfirst=True)
    from app.eval_day import LeaveInputContribution, LeaveInputSnapshot

    LeaveInputSnapshot.__table__.create(engine, checkfirst=True)
    LeaveInputContribution.__table__.create(engine, checkfirst=True)
    ConfigVersion.__table__.create(engine, checkfirst=True)
    ConfigValue.__table__.create(engine, checkfirst=True)
    EcoThresholdEvent.__table__.create(engine, checkfirst=True)
    EcoRecipientDesignation.__table__.create(engine, checkfirst=True)


def _provider():
    """The DAILY requirement provider. Separate axis from the threshold."""
    p = InMemoryConfigVersionProvider()
    for org in (ORG, OTHER_ORG):
        p.capture_version(
            organization_id=org, config_version_id="v1",
            effective_from=date(2026, 1, 1),
            candidates=[RequirementCandidate(
                tier="SHIFT", rank=2, minutes=REQUIRED_MINUTES)])
    return p


def _store_threshold(db, minutes, *, organization_id=ORG, version_key="t1",
                     effective_from=date(2026, 1, 1)):
    """Write the ECO threshold the way it is actually stored.

    Straight into ``ConfigValue``: there is deliberately no threshold capture
    path — who may set a threshold is unruled — so a test cannot and must not
    go through one.
    """
    from app.eco_threshold import (
        ECO_THRESHOLD_KEY, ECO_THRESHOLD_RANK, ECO_THRESHOLD_TIER,
    )

    row = ConfigVersion(
        organization_id=organization_id, version_key=version_key,
        effective_from=effective_from,
        captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="0" * 64,
    )
    db.add(row)
    db.flush()
    db.add(ConfigValue(
        config_version_id=row.id, organization_id=organization_id,
        scope_user_id=None, tier=ECO_THRESHOLD_TIER, rank=ECO_THRESHOLD_RANK,
        key=ECO_THRESHOLD_KEY, value_minutes=minutes,
    ))
    db.flush()
    return row


def _ledger(db, *, on, state=LedgerState.SEALED.value, week=WEEK,
            organization_id=ORG, user_id=USER, version=1, supersedes=None):
    # ``supersedes`` is a CONSTRUCTOR argument because that is what production
    # does: `_write_version` passes `supersedes=current.id` when it builds the
    # row, and the column is frozen once persisted. Back-filling it after the
    # flush was a fixture shortcut the ledger guard now correctly refuses.
    led = WorkDayLedger(
        organization_id=organization_id, user_id=user_id,
        work_date=datetime(on.year, on.month, on.day),
        workweek_key=week, ledger_version=version, state=state,
        timezone_iana="Asia/Kolkata",
        is_working_day=(state != LedgerState.NOT_A_WORKING_DAY.value),
        working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST", supersedes=supersedes,
    )
    db.add(led)
    db.flush()
    return led


def _seal(db, led, worked_seconds):
    begin = datetime(led.work_date.year, led.work_date.month,
                     led.work_date.day, 9, 0, tzinfo=UTC)
    row = WorkStateInterval(
        ledger_id=led.id, organization_id=led.organization_id, seq=1,
        state="WRK", start_at_utc=begin,
        end_at_utc=begin + timedelta(seconds=worked_seconds),
        duration_seconds=worked_seconds, provenance="WORKER", created_at=NOW,
    )
    db.add(row)
    db.flush()
    return [row]


def _record(db, evaluation):
    from app.eval_day import get_or_create_leave_snapshot

    snapshot = get_or_create_leave_snapshot(
        db, organization_id=evaluation.organization_id,
        user_id=evaluation.user_id, work_date=evaluation.work_date,
        overlap_seconds=0, contributions=())
    return record_evaluation(
        db, evaluation, leave_input_snapshot_key=snapshot.snapshot_key)


def _day(db, *, on, worked_seconds=0, state=LedgerState.SEALED.value,
         week=WEEK, organization_id=ORG, user_id=USER, version=1):
    led = _ledger(db, on=on, state=state, week=week,
                  organization_id=organization_id, user_id=user_id,
                  version=version)
    intervals = (_seal(db, led, worked_seconds)
                 if worked_seconds and state == LedgerState.SEALED.value
                 else [])
    evaluation = evaluate_day(
        ledger=led, intervals=intervals, provider=_provider())
    _record(db, evaluation)
    db.flush()
    return led


def _week_dates(key=WEEK):
    start = date.fromisoformat(key)
    return [start + timedelta(days=i) for i in range(DAYS_IN_WEEK)]


def _full_week(db, *, short_seconds, week=WEEK, user_id=USER,
               organization_id=ORG, version=1, working_days=5):
    """Sealed working days plus non-working days — a real seven-day week."""
    dates = _week_dates(week)
    for on in dates[:working_days]:
        _day(db, on=on, worked_seconds=REQUIRED_MINUTES * 60 - short_seconds,
             week=week, user_id=user_id, organization_id=organization_id,
             version=version)
    for on in dates[working_days:]:
        _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value, week=week,
             user_id=user_id, organization_id=organization_id, version=version)


def _correct_days(db, *, count, short_seconds, week=WEEK, user_id=USER,
                  organization_id=ORG, version=2):
    """Re-seal the first ``count`` working days at a new ledger version.

    The real correction path, including the part a naive fixture forgets: the
    predecessor's ``superseded_by`` is pointed at the new row. Without that the
    day has TWO current ledger rows, and EVAL-WEEK correctly refuses the whole
    week as ``LEDGER_MEMBERSHIP_INCONSISTENT`` — so a fixture that skips it
    tests nothing about crossings at all.

    A new ledger version is a new snapshot identity, so ``record_evaluation``
    writes a NEW ``EcoDayRecord`` that supersedes its predecessor rather than
    rewriting it. That lineage is exactly what D4 reads for ``is_retroactive``.
    """
    for on in _week_dates(week)[:count]:
        previous = db.execute(
            sa.select(WorkDayLedger).where(
                WorkDayLedger.organization_id == organization_id,
                WorkDayLedger.user_id == user_id,
                WorkDayLedger.work_date == datetime(on.year, on.month, on.day),
                WorkDayLedger.superseded_by.is_(None),
            )).scalars().one()

        led = _ledger(db, on=on, week=week, organization_id=organization_id,
                      user_id=user_id, version=version,
                      supersedes=previous.id)
        previous.superseded_by = led.id
        db.flush()

        intervals = _seal(db, led, REQUIRED_MINUTES * 60 - short_seconds)
        _record(db, evaluate_day(
            ledger=led, intervals=intervals, provider=_provider()))
        db.flush()


def _run(db, *, week=WEEK, user_id=USER, organization_id=ORG,
         mode=EventMode.OPERATIONAL):
    return evaluate_threshold_event(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=week, mode=mode)


def _rows(db, *, week=WEEK, user_id=USER, organization_id=ORG):
    return list(db.execute(
        sa.select(EcoThresholdEvent)
        .where(EcoThresholdEvent.organization_id == organization_id)
        .where(EcoThresholdEvent.user_id == user_id)
        .where(EcoThresholdEvent.workweek_key == week)
        .order_by(EcoThresholdEvent.id)
    ).scalars().all())


@pytest.fixture
def crossed(db):
    """A week that has crossed: W = 50, T = 30, one recorded crossing."""
    _store_threshold(db, THRESHOLD)
    _full_week(db, short_seconds=SHORT_ABOVE)
    result = _run(db)
    assert result.outcome is EventOutcome.CROSSED
    return result


def _source_tokens(path: pathlib.Path) -> set:
    """Identifiers and runtime strings — docstrings excluded.

    A text scan cannot tell "calls X" from "deliberately does not call X", and
    these docstrings do a great deal of the latter.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                out.add(node.value)
    return out


def _package_files():
    return sorted(p for p in PACKAGE.glob("*.py"))


# ═════════════════════════════════════════════════════════════════════════════
# A — the comparison (§21.1): W >= T, and equality crosses
# ═════════════════════════════════════════════════════════════════════════════

class TestComparison:

    def test_week_arithmetic_is_what_the_tests_assume(self, db):
        """The three week shapes really produce 50, 30 and 10 minutes.

        Asserted directly so a rounding-locus change fails here, loudly, rather
        than silently turning every lifecycle assertion below into a test of
        the wrong week.
        """
        from app.eval_week import evaluate_workweek

        for short, expected, week in (
            (SHORT_ABOVE, W_ABOVE, WEEK),
            (SHORT_EQUAL, W_EQUAL, OTHER_WEEK),
            (SHORT_BELOW, W_BELOW, "2026-09-14"),
        ):
            _full_week(db, short_seconds=short, week=week)
            evaluation = evaluate_workweek(
                db, organization_id=ORG, user_id=USER, workweek_key=week)
            assert evaluation.weekly_attributable_shortfall_minutes == expected

    def test_w_above_t_crosses(self, db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        result = _run(db)
        assert result.outcome is EventOutcome.CROSSED
        assert result.weekly_total_minutes == W_ABOVE
        assert result.threshold_minutes == THRESHOLD

    def test_w_exactly_equals_t_crosses(self, db):
        """§21.1: exactly T triggers. Strictly-greater-than is a defect.

        This is the single most important assertion in the file: an
        implementation using ``>`` passes every other crossing test here and
        fails only this one.
        """
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_EQUAL)
        result = _run(db)
        assert result.weekly_total_minutes == W_EQUAL == THRESHOLD
        assert result.outcome is EventOutcome.CROSSED
        assert len(_rows(db)) == 1

    def test_one_minute_below_t_does_not_cross(self, db):
        """The same week, against a threshold one minute higher.

        Pins the other side of the boundary: the comparison is not vacuously
        true, so ``>=`` is being tested rather than "always crosses".
        """
        _store_threshold(db, W_EQUAL + 1)
        _full_week(db, short_seconds=SHORT_EQUAL)
        result = _run(db)
        assert result.outcome is EventOutcome.NOT_CROSSED
        assert _rows(db) == []

    def test_w_below_t_writes_nothing(self, db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_BELOW)
        result = _run(db)
        assert result.outcome is EventOutcome.NOT_CROSSED
        assert result.event_id is None
        assert _rows(db) == []

    def test_source_contains_no_strict_greater_than_on_the_total(self):
        """No ``weekly_total > threshold`` anywhere in the package."""
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if "weekly_total" in names and "threshold_minutes" in names:
                assert all(isinstance(op, ast.GtE) for op in node.ops), (
                    "the threshold comparison must be >=, never >")


# ═════════════════════════════════════════════════════════════════════════════
# B — the crossing is a PERSISTED natural key (§21.2)
# ═════════════════════════════════════════════════════════════════════════════

class TestIdempotency:

    def test_crossing_writes_exactly_one_primary_row(self, db, crossed):
        rows = _rows(db)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == REACHED
        assert row.tier == PRIMARY
        assert row.sequence == 1
        assert row.mode == OPERATIONAL
        assert row.supersedes_event_id is None

    def test_replay_writes_nothing(self, db, crossed):
        again = _run(db)
        assert again.outcome is EventOutcome.ALREADY_CROSSED
        assert again.event_id == crossed.event_id
        assert len(_rows(db)) == 1

    def test_replay_ten_times_writes_nothing(self, db, crossed):
        for _ in range(10):
            assert _run(db).outcome is EventOutcome.ALREADY_CROSSED
        assert len(_rows(db)) == 1

    def test_a_further_shortfall_does_not_create_a_second_event(self, db,
                                                                crossed):
        """The week gets worse. It has already crossed; nothing new happens."""
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE * 2)
        result = _run(db)
        assert result.outcome is EventOutcome.ALREADY_CROSSED
        assert len(_rows(db)) == 1

    def test_the_decision_survives_a_new_session(self, db, crossed, engine):
        """No in-memory state: a different Session reaches the same answer.

        The point of §21.2's natural key is that a restart, a second worker or
        a replay months later cannot produce a duplicate primary event.
        """
        from sqlalchemy.orm import Session

        with Session(engine) as fresh:
            result = evaluate_threshold_event(
                fresh, organization_id=ORG, user_id=USER, workweek_key=WEEK)
            assert result.outcome is EventOutcome.ALREADY_CROSSED
            assert result.event_id == crossed.event_id

    def test_no_has_notified_boolean_exists(self):
        columns = {c.name for c in EcoThresholdEvent.__table__.columns}
        for banned in ("has_notified", "notified", "notification_sent",
                       "is_notified", "delivered", "sent"):
            assert banned not in columns

    def test_other_worker_and_other_week_are_separate_episodes(self, db,
                                                              crossed):
        _full_week(db, short_seconds=SHORT_ABOVE, user_id=OTHER_USER)
        other = _run(db, user_id=OTHER_USER)
        assert other.outcome is EventOutcome.CROSSED
        assert other.sequence == 1

        _full_week(db, short_seconds=SHORT_ABOVE, week=OTHER_WEEK)
        later = _run(db, week=OTHER_WEEK)
        assert later.outcome is EventOutcome.CROSSED
        assert later.sequence == 1


# ═════════════════════════════════════════════════════════════════════════════
# C — withdrawal (§21.3, R-21.3.1)
# ═════════════════════════════════════════════════════════════════════════════

def _withdraw(db):
    """Correct the week downwards until it is below T, then re-run."""
    _correct_days(db, count=5, short_seconds=SHORT_BELOW)
    return _run(db)


class TestWithdrawal:

    def test_falling_below_t_records_a_withdrawal(self, db, crossed):
        result = _withdraw(db)
        assert result.outcome is EventOutcome.WITHDRAWN
        rows = _rows(db)
        assert len(rows) == 2
        assert [r.event_type for r in rows] == [REACHED, WITHDRAWN]

    def test_the_withdrawal_references_the_crossing_it_closes(self, db,
                                                             crossed):
        _withdraw(db)
        original, withdrawal = _rows(db)
        assert withdrawal.supersedes_event_id == original.id
        assert withdrawal.sequence == original.sequence == 1

    def test_the_original_is_not_deleted_hidden_or_back_dated(self, db,
                                                              crossed):
        """R-21.3.1, asserted field by field rather than by row count."""
        before = _rows(db)[0]
        frozen = {c.name: getattr(before, c.name)
                  for c in EcoThresholdEvent.__table__.columns}

        _withdraw(db)
        db.expire_all()

        after = db.get(EcoThresholdEvent, frozen["id"])
        assert after is not None, "the original was deleted"
        for name, value in frozen.items():
            assert getattr(after, name) == value, f"{name} was rewritten"

    def test_the_withdrawal_reuses_the_original_recipient_snapshot(self, db,
                                                                   crossed):
        """D5: recipients are frozen at creation, never re-resolved."""
        original = _rows(db)[0]
        expected = dict(original.recipient_snapshot)
        _withdraw(db)
        withdrawal = _rows(db)[1]
        assert withdrawal.recipient_snapshot == expected

    def test_a_second_run_below_t_does_not_withdraw_twice(self, db, crossed):
        _withdraw(db)
        again = _run(db)
        assert again.outcome is EventOutcome.NOT_CROSSED
        assert len(_rows(db)) == 2

    def test_the_withdrawal_records_the_new_total(self, db, crossed):
        _withdraw(db)
        withdrawal = _rows(db)[1]
        assert withdrawal.weekly_total_minutes == W_BELOW
        assert withdrawal.weekly_total_minutes < withdrawal.threshold_minutes


# ═════════════════════════════════════════════════════════════════════════════
# D — append-only enforcement (D1). The database is the authority.
# ═════════════════════════════════════════════════════════════════════════════

class TestAppendOnly:

    def test_core_update_is_refused_below_the_orm(self, db, crossed):
        """A Core UPDATE fires no ORM listener. The trigger still refuses it."""
        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            db.execute(sa.text(
                "UPDATE eco_threshold_event SET weekly_total_minutes = 0 "
                "WHERE id = :i"), {"i": crossed.event_id})
        assert "append-only" in str(excinfo.value)
        db.rollback()

    def test_core_delete_is_refused_below_the_orm(self, db, crossed):
        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            db.execute(sa.text(
                "DELETE FROM eco_threshold_event WHERE id = :i"),
                {"i": crossed.event_id})
        assert "append-only" in str(excinfo.value)
        db.rollback()

    def test_bulk_update_of_the_whole_table_is_refused(self, db, crossed):
        with pytest.raises(sa.exc.IntegrityError):
            db.execute(sa.text(
                "UPDATE eco_threshold_event SET detail = 'rewritten'"))
        db.rollback()

    def test_orm_update_is_refused(self, db, crossed):
        row = db.get(EcoThresholdEvent, crossed.event_id)
        row.detail = "rewritten"
        with pytest.raises(ThresholdEventImmutable):
            db.flush()
        db.rollback()

    def test_orm_delete_is_refused(self, db, crossed):
        row = db.get(EcoThresholdEvent, crossed.event_id)
        db.delete(row)
        with pytest.raises(ThresholdEventImmutable):
            db.flush()
        db.rollback()

    def test_the_triggers_exist_by_name(self, db):
        names = set(db.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )).scalars().all())
        assert {"eco_threshold_event_no_update",
                "eco_threshold_event_no_delete"} <= names

    def test_there_is_no_updated_at_column(self):
        columns = {c.name for c in EcoThresholdEvent.__table__.columns}
        assert "updated_at" not in columns
        assert "updated_at_utc" not in columns

    def test_only_the_two_supported_dialects_are_protected(self):
        assert SUPPORTED_DIALECTS == {"sqlite", "postgresql"}
        assert set(APPEND_ONLY_DDL_BY_DIALECT) == SUPPORTED_DIALECTS

    def test_postgres_ddl_guards_both_operations_at_row_level(self):
        """Shape only — no PostgreSQL is reachable in this suite, and a claim
        about runtime behaviour that nothing executed would be a claim about
        code that was never run."""
        joined = " ".join(POSTGRES_APPEND_ONLY_DDL)
        assert "BEFORE UPDATE ON eco_threshold_event" in joined
        assert "BEFORE DELETE ON eco_threshold_event" in joined
        assert joined.count("FOR EACH ROW") == 2
        assert "RAISE EXCEPTION" in joined
        assert "plpgsql" in joined

    def test_sqlite_ddl_aborts_rather_than_ignoring(self):
        joined = " ".join(SQLITE_APPEND_ONLY_DDL)
        assert joined.count("RAISE(ABORT") == 2
        assert "IGNORE" not in joined


# ═════════════════════════════════════════════════════════════════════════════
# E — re-crossing (§21.4)
# ═════════════════════════════════════════════════════════════════════════════

class TestReCrossing:

    def test_re_crossing_takes_sequence_two(self, db, crossed):
        _withdraw(db)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, version=3)
        result = _run(db)
        assert result.outcome is EventOutcome.RE_CROSSED
        assert result.sequence == 2

    def test_the_re_crossing_references_the_withdrawal(self, db, crossed):
        _withdraw(db)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, version=3)
        _run(db)
        original, withdrawal, recrossing = _rows(db)
        assert recrossing.event_type == REACHED
        assert recrossing.supersedes_event_id == withdrawal.id
        assert withdrawal.supersedes_event_id == original.id

    def test_the_episode_reads_as_one_ordered_history(self, db, crossed):
        _withdraw(db)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, version=3)
        _run(db)
        history = read_events(db, organization_id=ORG, user_id=USER,
                              workweek_key=WEEK)
        assert [(e.event_type, e.sequence) for e in history] == [
            (REACHED, 1), (WITHDRAWN, 1), (REACHED, 2)]

    def test_replay_after_a_re_crossing_writes_nothing(self, db, crossed):
        _withdraw(db)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, version=3)
        _run(db)
        again = _run(db)
        assert again.outcome is EventOutcome.ALREADY_CROSSED
        assert again.sequence == 2
        assert len(_rows(db)) == 3

    def test_a_crossing_cannot_be_withdrawn_twice(self, db, crossed):
        """Enforced by the database, not by the read that precedes it."""
        original = _rows(db)[0]
        _withdraw(db)
        duplicate = EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, sequence=1, mode=OPERATIONAL, event_type=WITHDRAWN,
            supersedes_event_id=original.id, weekly_total_minutes=0,
            threshold_minutes=THRESHOLD, is_retroactive=False, caused_by={},
            week_provenance={}, recipient_snapshot={}, worker_recipient=USER,
            created_by="TEST")
        db.add(duplicate)
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# F — frozen provenance (D2)
# ═════════════════════════════════════════════════════════════════════════════

class TestProvenance:

    def test_the_event_carries_the_whole_week_evaluation(self, db, crossed):
        from app.eval_week import evaluate_workweek

        evaluation = evaluate_workweek(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK)
        provenance = _rows(db)[0].week_provenance

        assert provenance["canonical_dates"] == [
            d.isoformat() for d in evaluation.canonical_dates]
        assert provenance["day_record_ids"] == list(evaluation.day_record_ids)
        assert provenance["ledger_versions"] == list(evaluation.ledger_versions)
        assert provenance["policy_versions"] == list(evaluation.policy_versions)
        assert provenance["config_versions"] == list(evaluation.config_versions)
        assert provenance["observed_states"] == list(evaluation.observed_states)
        assert provenance["blocking_dates"] == []

    def test_w_alone_is_insufficient_so_day_hashes_are_frozen_too(self, db,
                                                                  crossed):
        provenance = _rows(db)[0].week_provenance
        hashes = provenance["day_content_hashes"]
        assert len(hashes) == DAYS_IN_WEEK
        assert all(len(h) == 64 for h in hashes)

        stored = {r.id: r.content_hash for r in db.execute(
            sa.select(EcoDayRecord).where(
                EcoDayRecord.id.in_(provenance["day_record_ids"]))
        ).scalars().all()}
        assert hashes == [stored[i] for i in provenance["day_record_ids"]]

    def test_provenance_is_never_recomputed(self, db, crossed):
        """A later correction must not change what the event says it saw."""
        frozen = dict(_rows(db)[0].week_provenance)
        _correct_days(db, count=5, short_seconds=SHORT_BELOW)
        _run(db)
        db.expire_all()
        assert _rows(db)[0].week_provenance == frozen

    def test_the_threshold_version_is_recorded(self, db, crossed):
        assert _rows(db)[0].threshold_config_version is not None


# ═════════════════════════════════════════════════════════════════════════════
# G — retroactivity (D4): causal lineage, never a clock read
# ═════════════════════════════════════════════════════════════════════════════

class TestRetroactivity:

    def test_an_uncorrected_week_is_not_retroactive(self, db, crossed):
        row = _rows(db)[0]
        assert row.is_retroactive is False
        assert row.caused_by["corrected_day_record_ids"] == []
        assert row.caused_by["re_evaluated_generations"] == []

    def test_a_correction_driven_crossing_is_retroactive(self, db):
        """The week only crosses because its evidence was corrected."""
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_BELOW)
        assert _run(db).outcome is EventOutcome.NOT_CROSSED

        _correct_days(db, count=5, short_seconds=SHORT_ABOVE)
        result = _run(db)
        assert result.outcome is EventOutcome.CROSSED

        row = _rows(db)[0]
        assert row.is_retroactive is True
        assert row.caused_by["corrected_day_record_ids"]
        assert all(g > 1 for g in row.caused_by["re_evaluated_generations"])

    def test_the_lineage_names_the_records_that_justify_the_flag(self, db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_BELOW)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE)
        _run(db)

        row = _rows(db)[0]
        named = row.caused_by["corrected_day_record_ids"]
        records = db.execute(
            sa.select(EcoDayRecord).where(EcoDayRecord.id.in_(named))
        ).scalars().all()
        assert records, "the flag must name real records"
        assert all(r.supersedes is not None for r in records)

    def test_the_flag_is_an_indicator_not_causal_proof(self, db):
        """Ruling 3, made executable rather than left as prose.

        The week is corrected to the SAME worked seconds it already had: the
        lineage moves (generation 2, ``supersedes`` set) while the weekly total
        does not move at all, so the correction cannot have caused anything.
        The flag still rises — which is precisely the precision limit, and the
        reason this value must never be read as proof of causation.
        """
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE)   # a no-op fix

        result = _run(db)
        assert result.outcome is EventOutcome.CROSSED
        assert result.weekly_total_minutes == W_ABOVE, (
            "the correction must not have changed the total")

        row = _rows(db)[0]
        assert row.is_retroactive is True
        assert row.caused_by["corrected_day_record_ids"]

    def test_the_limitation_is_documented_where_the_value_is_defined(self):
        """A caller reading ``is_retroactive`` must meet the caveat there."""
        text = (PACKAGE / "outcomes.py").read_text(encoding="utf-8")
        assert "NOT CAUSAL PROOF" in text.upper()
        assert "NOT_CROSSED" in text, (
            "the missing-counterfactual half of the limit must be stated too")

    def test_the_determination_reads_no_clock(self):
        """No ``now``/``utcnow``/``today`` in any decision path.

        The only timestamp the package takes is the audit ``created_at_utc``
        default, which no branch reads. Pinned by AST so a later "just check
        whether it is still this week" cannot slip in.
        """
        tokens = _source_tokens(PACKAGE / "service.py")
        for banned in ("now", "utcnow", "today", "date_today", "time"):
            assert banned not in tokens, f"service.py reads the clock: {banned}"

    def test_the_same_inputs_produce_the_same_determination_twice(self, db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_BELOW)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE)
        first = _run(db)
        first_row = dict(_rows(db)[0].caused_by)

        # The same stored threshold governs both weeks — one ConfigVersion is
        # effective from a date before either of them.
        _full_week(db, short_seconds=SHORT_BELOW, week=OTHER_WEEK)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, week=OTHER_WEEK)
        second = _run(db, week=OTHER_WEEK)

        assert first.outcome is second.outcome
        assert first_row["is_retroactive"] is True
        assert _rows(db, week=OTHER_WEEK)[0].caused_by[
            "is_retroactive"] is True


# ═════════════════════════════════════════════════════════════════════════════
# H — recipients (D5). Worker only, for this increment.
# ═════════════════════════════════════════════════════════════════════════════

class TestRecipients:

    def test_the_snapshot_names_the_worker(self, db, crossed):
        row = _rows(db)[0]
        assert row.worker_recipient == USER
        assert row.recipient_snapshot["worker_user_id"] == USER
        assert row.recipient_snapshot["roles"] == ["WORKER"]

    def test_only_the_four_spec_audiences_appear(self, db, crossed):
        """Restated for Stage 5, which authorised exactly this vocabulary.

        Before RECIPIENT-AUTHORITY existed this asserted that the word
        "manager" appeared nowhere, because any party beyond the Worker would
        have had to be invented. Owner ruling D-3 replaced the earlier A/B/C
        options with §33.1's four audiences, so DIRECT MANAGER is now the
        SPECIFICATION's word rather than an invention — and the claim becomes
        the stronger one: the snapshot names those four and NOTHING else, and
        the invented vocabulary the original guard was built to catch is still
        absent from the package.
        """
        from app.recipient_authority import AUDIENCE_ORDER

        snapshot = _rows(db)[0].recipient_snapshot
        named = [entry["audience"] for entry in snapshot["audiences"]]
        assert named == [a.value for a in AUDIENCE_ORDER]

        # An invented party is still an invented party. "manager" is gone from
        # this list because §33.1 names it; the rest never appear in the spec.
        payload = str(snapshot).lower()
        for banned in ("reviewer", "approver", "supervisor", "team_lead",
                       "payroll", "finance"):
            assert banned not in payload

        for path in _package_files():
            tokens = {t.lower() for t in _source_tokens(path)}
            for banned in ("reviewer", "designated_reviewer", "approver",
                           "reviewer_role", "reviewer_authority"):
                assert banned not in tokens, f"{path.name} invents a reviewer"

    def test_the_snapshot_is_identity_based_and_never_an_address(self, db,
                                                                 crossed):
        """D-3: authority is identity, never an email lookup."""
        payload = str(_rows(db)[0].recipient_snapshot)
        assert "@" not in payload
        for banned in ("email", "address", "smtp", "display_name"):
            assert banned not in payload.lower()


# ═════════════════════════════════════════════════════════════════════════════
# I — fail-closed refusals
# ═════════════════════════════════════════════════════════════════════════════

class TestRefusals:

    def test_an_unfinished_week_refuses_and_writes_nothing(self, db):
        """A partial week has no W. Comparing one to T is the exact
        confident-wrong-number failure EVAL-WEEK exists to prevent."""
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE, working_days=5)
        # Remove one day's authoritative record: the week is no longer final.
        db.execute(sa.text(
            "DELETE FROM eco_day_record WHERE organization_id = :o "
            "AND user_id = :u AND work_date = :d"),
            {"o": ORG, "u": USER, "d": _week_dates()[0].isoformat()})
        db.flush()

        result = _run(db)
        assert result.outcome is EventOutcome.REFUSED
        assert result.refusal is RefusalReason.WEEK_NOT_FINAL
        assert result.event_id is None
        assert _rows(db) == []

    def test_no_threshold_refuses_and_writes_nothing(self, db):
        """There is no default and no borrowed figure. Never 30 by accident."""
        _full_week(db, short_seconds=SHORT_ABOVE)
        result = _run(db)
        assert result.outcome is EventOutcome.REFUSED
        assert result.refusal is RefusalReason.THRESHOLD_NOT_RESOLVED
        assert _rows(db) == []

    @pytest.mark.parametrize("key", [
        "", "2026-W36", "2026-8-31", "31-08-2026", "2026-08-31T00:00:00",
        "not-a-date", "2026-02-30",
    ])
    def test_a_non_canonical_workweek_key_refuses(self, db, key):
        result = evaluate_threshold_event(
            db, organization_id=ORG, user_id=USER, workweek_key=key)
        assert result.outcome is EventOutcome.REFUSED
        assert result.refusal is RefusalReason.MALFORMED_WORKWEEK_KEY

    def test_refusals_are_distinct_and_never_collapsed(self):
        assert len(set(RefusalReason)) == 3

    def test_a_refusal_cannot_carry_an_event_id(self):
        with pytest.raises(ValueError):
            ThresholdEventResult(
                outcome=EventOutcome.REFUSED, organization_id=ORG,
                user_id=USER, workweek_key=WEEK, mode=EventMode.OPERATIONAL,
                refusal=RefusalReason.WEEK_NOT_FINAL, event_id=1)

    def test_a_refusal_must_say_why(self):
        with pytest.raises(ValueError):
            ThresholdEventResult(
                outcome=EventOutcome.REFUSED, organization_id=ORG,
                user_id=USER, workweek_key=WEEK, mode=EventMode.OPERATIONAL)


# ═════════════════════════════════════════════════════════════════════════════
# J — SHADOW isolation (D8)
# ═════════════════════════════════════════════════════════════════════════════

class TestShadowIsolation:

    def test_a_shadow_crossing_does_not_satisfy_an_operational_run(self, db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)

        shadow = _run(db, mode=EventMode.SHADOW)
        assert shadow.outcome is EventOutcome.CROSSED

        operational = _run(db, mode=EventMode.OPERATIONAL)
        assert operational.outcome is EventOutcome.CROSSED, (
            "a shadow row must not be mistaken for an operational crossing")
        assert operational.event_id != shadow.event_id

    def test_both_modes_hold_sequence_one_at_once(self, db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        _run(db, mode=EventMode.SHADOW)
        _run(db, mode=EventMode.OPERATIONAL)

        rows = _rows(db)
        assert len(rows) == 2
        assert {r.mode for r in rows} == {SHADOW, OPERATIONAL}
        assert {r.sequence for r in rows} == {1}

    def test_reads_are_scoped_to_one_mode(self, db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        _run(db, mode=EventMode.SHADOW)
        _run(db, mode=EventMode.OPERATIONAL)

        for mode, expected in ((EventMode.SHADOW, SHADOW),
                               (EventMode.OPERATIONAL, OPERATIONAL)):
            history = read_events(db, organization_id=ORG, user_id=USER,
                                  workweek_key=WEEK, mode=mode)
            assert [e.mode for e in history] == [expected]

    def test_a_shadow_withdrawal_does_not_touch_the_operational_episode(self,
                                                                        db):
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        _run(db, mode=EventMode.SHADOW)
        operational = _run(db, mode=EventMode.OPERATIONAL)

        _correct_days(db, count=5, short_seconds=SHORT_BELOW)
        _run(db, mode=EventMode.SHADOW)

        still_open = _run(db, mode=EventMode.OPERATIONAL)
        assert still_open.outcome is EventOutcome.WITHDRAWN
        assert still_open.event_id != operational.event_id
        shadow_rows = [r for r in _rows(db) if r.mode == SHADOW]
        assert [r.event_type for r in shadow_rows] == [REACHED, WITHDRAWN]


# ═════════════════════════════════════════════════════════════════════════════
# K — the database is the concurrency authority (D6)
# ═════════════════════════════════════════════════════════════════════════════

class TestConcurrency:

    def test_a_duplicate_primary_is_refused_by_the_database(self, db, crossed):
        """Not by the read that precedes it: two runs that both saw no open
        crossing compute the SAME sequence, and one insert must lose."""
        duplicate = EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, sequence=1, mode=OPERATIONAL, event_type=REACHED,
            weekly_total_minutes=W_ABOVE, threshold_minutes=THRESHOLD,
            is_retroactive=False, caused_by={}, week_provenance={},
            recipient_snapshot={}, worker_recipient=USER, created_by="TEST")
        db.add(duplicate)
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_the_uniqueness_boundary_is_the_documented_one(self):
        constraint = next(
            c for c in EcoThresholdEvent.__table__.constraints
            if getattr(c, "name", None) == "uq_eco_threshold_event_identity")
        assert [c.name for c in constraint.columns] == [
            "organization_id", "user_id", "workweek_key", "tier", "sequence",
            "mode", "event_type"]

    def test_at_most_one_row_may_supersede_another(self):
        constraint = next(
            c for c in EcoThresholdEvent.__table__.constraints
            if getattr(c, "name", None)
            == "uq_eco_threshold_event_withdrawal_target")
        assert [c.name for c in constraint.columns] == ["supersedes_event_id"]

    def test_no_in_memory_lock_or_module_level_state(self):
        for path in _package_files():
            tokens = _source_tokens(path)
            for banned in ("Lock", "RLock", "Semaphore", "threading",
                           "lru_cache", "cache"):
                assert banned not in tokens, f"{path.name} holds shared state"


# ═════════════════════════════════════════════════════════════════════════════
# K2 — the owner rulings that closed the two implementation deviations
# ═════════════════════════════════════════════════════════════════════════════

class TestRuledLifecycleBoundary:
    """Ruling 1 (amended D6) and Ruling 2 (no reverse pointer), asserted as
    BEHAVIOUR rather than as constraint metadata.

    The column list is checked elsewhere; what matters here is that the shapes
    the ruling permits are accepted and the shapes it forbids are refused.
    """

    def test_event_type_participates_so_a_withdrawal_may_share_the_sequence(
            self, db, crossed):
        """crossing 1 and withdrawal 1 coexist — the point of the amendment.

        Under the original six-column boundary this insert is refused, and the
        ruled lifecycle cannot be represented at all.
        """
        original = _rows(db)[0]
        db.add(EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, sequence=original.sequence, mode=OPERATIONAL,
            event_type=WITHDRAWN, supersedes_event_id=original.id,
            weekly_total_minutes=0, threshold_minutes=THRESHOLD,
            is_retroactive=False, caused_by={}, week_provenance={},
            recipient_snapshot={}, worker_recipient=USER, created_by="TEST"))
        db.flush()

        rows = _rows(db)
        assert [(r.event_type, r.sequence) for r in rows] == [
            (REACHED, 1), (WITHDRAWN, 1)]

    def test_two_rows_of_the_same_type_at_one_identity_are_still_refused(
            self, db, crossed):
        """The amendment widened the boundary by exactly one column and no
        more: same type, same sequence, same mode is still a duplicate."""
        db.add(EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, sequence=1, mode=OPERATIONAL, event_type=WITHDRAWN,
            supersedes_event_id=None, weekly_total_minutes=0,
            threshold_minutes=THRESHOLD, is_retroactive=False, caused_by={},
            week_provenance={}, recipient_snapshot={}, worker_recipient=USER,
            created_by="TEST"))
        db.flush()
        db.add(EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, sequence=1, mode=OPERATIONAL, event_type=WITHDRAWN,
            supersedes_event_id=None, weekly_total_minutes=0,
            threshold_minutes=THRESHOLD, is_retroactive=False, caused_by={},
            week_provenance={}, recipient_snapshot={}, worker_recipient=USER,
            created_by="TEST"))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_a_withdrawn_crossing_is_not_the_open_primary(self, db, crossed):
        """The open-primary invariant is NOT weakened by the amendment.

        Asserted against the authority directly — the internal read the
        lifecycle branches on — rather than inferred from an outcome.
        """
        from app.eval_event.service import _open_crossing

        assert _open_crossing(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, mode=OPERATIONAL) is not None

        _withdraw(db)

        assert _open_crossing(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, mode=OPERATIONAL) is None, (
            "a withdrawn crossing must not count as the open primary")

    def test_exactly_one_open_primary_survives_a_full_episode(self, db,
                                                              crossed):
        """crossing 1 -> withdrawal 1 -> re-crossing 2, and at every point at
        most one open primary exists for (org, user, week, tier, mode)."""
        from app.eval_event.service import _open_crossing

        def open_ids():
            found = _open_crossing(
                db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
                tier=PRIMARY, mode=OPERATIONAL)
            return [] if found is None else [found.id]

        assert len(open_ids()) == 1
        _withdraw(db)
        assert open_ids() == []
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, version=3)
        _run(db)
        assert len(open_ids()) == 1

        rows = _rows(db)
        assert [(r.event_type, r.sequence) for r in rows] == [
            (REACHED, 1), (WITHDRAWN, 1), (REACHED, 2)]

    def test_a_lost_insert_race_yields_the_winner_not_a_second_primary(
            self, db, monkeypatch):
        """The competitor committed between this run's read and its insert.

        Simulated by making the "is anything open?" read miss ONCE — which is
        exactly the window a real concurrent run opens. The database refuses
        the duplicate, and the service must re-read and report the winner
        rather than raise or write a second open primary.
        """
        from app.eval_event import service as svc

        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        winner = _run(db)
        assert winner.outcome is EventOutcome.CROSSED

        real = svc._open_crossing
        calls = {"n": 0}

        def blind_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None          # the competitor is not visible yet
            return real(*args, **kwargs)

        monkeypatch.setattr(svc, "_open_crossing", blind_once)

        loser = _run(db)
        assert loser.outcome is EventOutcome.ALREADY_CROSSED
        assert loser.event_id == winner.event_id
        assert calls["n"] >= 2, "the recovery path must re-read the winner"

        open_rows = [r for r in _rows(db) if r.event_type == REACHED]
        assert len(open_rows) == 1, "a second open primary was created"

    def test_a_lost_race_after_a_withdrawal_also_yields_the_winner(self, db,
                                                                   crossed,
                                                                   monkeypatch):
        """The same race, one lifecycle later. Sequence 2, not sequence 3."""
        from app.eval_event import service as svc

        _withdraw(db)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, version=3)
        winner = _run(db)
        assert (winner.outcome, winner.sequence) == (
            EventOutcome.RE_CROSSED, 2)

        real = svc._open_crossing
        calls = {"n": 0}

        def blind_once(*args, **kwargs):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(*args, **kwargs)

        monkeypatch.setattr(svc, "_open_crossing", blind_once)

        loser = _run(db)
        assert loser.outcome is EventOutcome.ALREADY_CROSSED
        assert loser.event_id == winner.event_id
        assert [r.sequence for r in _rows(db) if r.event_type == REACHED] == [
            1, 2], "a third crossing was created"

    def test_the_sequence_is_derived_from_withdrawals_not_from_max(self):
        """Pins the derivation itself, not merely its current output.

        ``max(sequence) + 1`` reads correct and is not: it lets a run that saw
        no open crossing, but counted sequences after a competitor committed,
        create a second open primary. A refactor back to it must fail here
        rather than in production.
        """
        import inspect

        from app.eval_event.service import _next_sequence

        source = inspect.getsource(_next_sequence)
        body = source.split('"""')[-1]
        assert "ECO_THRESHOLD_WITHDRAWN" in body, (
            "the sequence must be counted from withdrawals")
        assert "desc()" not in body and "max" not in body.lower(), (
            "the sequence must not be derived from max(sequence)")

    def test_the_original_crossing_is_never_updated_by_the_lifecycle(
            self, db, crossed):
        """Ruling 2: no reverse pointer is written, ever.

        The whole episode runs; the original row's every column is compared
        before and after. An UPDATE would also be refused by the triggers, so
        this pins the weaker but more informative claim: nothing even tries.
        """
        original_id = crossed.event_id
        before = {c.name: getattr(db.get(EcoThresholdEvent, original_id),
                                  c.name)
                  for c in EcoThresholdEvent.__table__.columns}

        _withdraw(db)
        _correct_days(db, count=5, short_seconds=SHORT_ABOVE, version=3)
        _run(db)
        db.expire_all()

        after = db.get(EcoThresholdEvent, original_id)
        for name, value in before.items():
            assert getattr(after, name) == value, f"{name} changed"
        assert after.supersedes_event_id is None

    def test_no_column_named_for_a_reverse_pointer_exists(self):
        columns = {c.name for c in EcoThresholdEvent.__table__.columns}
        for banned in ("withdrawn_by_event_id", "withdrawn_by", "closed_by",
                       "superseded_by", "withdrawn", "is_open", "is_withdrawn"):
            assert banned not in columns, (
                f"{banned} is a mutable reverse pointer; Ruling 2 forbids it")


# ═════════════════════════════════════════════════════════════════════════════
# K3 — audit Finding 2: the recovery lookup carries its full context
# ═════════════════════════════════════════════════════════════════════════════

class TestRecoveryLookupIsContextScoped:
    """The withdrawal-recovery read must not trust a globally unique id alone.

    ``supersedes_event_id`` is unique across the whole table, so a lookup on it
    by itself already finds the right row — until something puts a row there
    that should not exist. These tests introduce exactly that: a withdrawal
    that references a crossing in ANOTHER context. The recovery read must not
    return it.

    The malformed rows are inserted directly, not through the service, because
    the service cannot produce them — which is the point. A defence-in-depth
    filter is only testable by supplying the input it defends against.
    """

    CONTEXTS = [
        ("mode", {"mode": SHADOW}),
        ("organization_id", {"organization_id": OTHER_ORG}),
        ("user_id", {"user_id": OTHER_USER}),
        ("workweek_key", {"workweek_key": OTHER_WEEK}),
        ("tier", {"tier": "SECONDARY"}),
    ]

    @pytest.mark.parametrize("dimension,override",
                             CONTEXTS, ids=[c[0] for c in CONTEXTS])
    def test_a_cross_context_withdrawal_is_not_returned(self, db, crossed,
                                                        dimension, override):
        from app.eval_event.service import _withdrawal_of

        original = _rows(db)[0]
        fields = dict(
            organization_id=ORG, user_id=USER, workweek_key=WEEK, tier=PRIMARY,
            sequence=1, mode=OPERATIONAL,
            event_type=WITHDRAWN, supersedes_event_id=original.id,
            weekly_total_minutes=0, threshold_minutes=THRESHOLD,
            is_retroactive=False, caused_by={}, week_provenance={},
            recipient_snapshot={}, worker_recipient=USER, created_by="TEST")
        fields.update(override)
        db.add(EcoThresholdEvent(**fields))
        db.flush()

        # Sanity: the row really is there, and an id-only read WOULD find it.
        id_only = db.execute(
            sa.select(EcoThresholdEvent).where(
                EcoThresholdEvent.supersedes_event_id == original.id)
        ).scalars().first()
        assert id_only is not None, "the malformed row was not inserted"
        assert getattr(id_only, dimension) == override[dimension]

        # The scoped read must refuse it.
        found = _withdrawal_of(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, mode=OPERATIONAL, crossing_id=original.id)
        assert found is None, (
            f"the recovery lookup returned a withdrawal from another "
            f"{dimension}")

    def test_the_correctly_scoped_withdrawal_is_still_found(self, db, crossed):
        """The filter narrows what is read; it must not break the real case."""
        from app.eval_event.service import _withdrawal_of

        original = _rows(db)[0]
        _withdraw(db)
        found = _withdrawal_of(
            db, organization_id=ORG, user_id=USER, workweek_key=WEEK,
            tier=PRIMARY, mode=OPERATIONAL, crossing_id=original.id)
        assert found is not None
        assert found.event_type == WITHDRAWN
        assert found.supersedes_event_id == original.id

    def test_the_global_uniqueness_constraint_is_not_weakened(self, db,
                                                             crossed):
        """Narrowing the READ must not narrow what may EXIST."""
        original = _rows(db)[0]
        db.add(EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=WEEK, tier=PRIMARY,
            sequence=1, mode=SHADOW, event_type=WITHDRAWN,
            supersedes_event_id=original.id, weekly_total_minutes=0,
            threshold_minutes=THRESHOLD, is_retroactive=False, caused_by={},
            week_provenance={}, recipient_snapshot={}, worker_recipient=USER,
            created_by="TEST"))
        db.flush()
        db.add(EcoThresholdEvent(
            organization_id=ORG, user_id=USER, workweek_key=WEEK, tier=PRIMARY,
            sequence=2, mode=SHADOW, event_type=WITHDRAWN,
            supersedes_event_id=original.id, weekly_total_minutes=0,
            threshold_minutes=THRESHOLD, is_retroactive=False, caused_by={},
            week_provenance={}, recipient_snapshot={}, worker_recipient=USER,
            created_by="TEST"))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_every_event_read_in_the_service_carries_tenant_and_mode(self):
        """Pins the convention itself, so a future read cannot quietly skip it.

        Counted over the functions that read the event table; ``read_events``
        is scoped by mode but deliberately not by tier, which is recorded as an
        informational future-proofing item rather than changed here.
        """
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)]:
            body = ast.get_source_segment(source, fn) or ""
            if "select(EcoThresholdEvent" not in body:
                continue
            n_selects = body.count("select(EcoThresholdEvent")
            for column in ("organization_id", "user_id", "workweek_key",
                           "mode"):
                assert body.count(f"EcoThresholdEvent.{column} ==") >= \
                    n_selects, (
                    f"{fn.name} has {n_selects} event select(s) but does not "
                    f"filter {column} on each")


# ═════════════════════════════════════════════════════════════════════════════
# K4 — audit Finding 4: frozen provenance fails closed
# ═════════════════════════════════════════════════════════════════════════════

class TestProvenanceParity:
    """A hash list shorter than the id list it describes is worse than no
    event: it looks well-formed while silently covering fewer days."""

    def test_hashes_correspond_positionally_to_ids(self, db, crossed):
        provenance = _rows(db)[0].week_provenance
        ids = provenance["day_record_ids"]
        hashes = provenance["day_content_hashes"]
        assert len(ids) == len(hashes) == DAYS_IN_WEEK

        stored = {r.id: r.content_hash for r in db.execute(
            sa.select(EcoDayRecord).where(EcoDayRecord.id.in_(ids))
        ).scalars().all()}
        for position, record_id in enumerate(ids):
            assert hashes[position] == stored[record_id], (
                f"position {position} describes record {record_id} but "
                f"carries another record's hash")

    def test_an_unresolvable_day_record_refuses_instead_of_truncating(self,
                                                                      db):
        """The guard, supplied with the input it exists for.

        ``evaluate_workweek`` would itself refuse a week with a missing record,
        so the only way to reach this is to hand the freezer an evaluation that
        names an id which is not there — exactly the state the contract must
        fail closed on if it ever occurs.
        """
        import dataclasses

        from app.eval_event.service import _freeze_provenance
        from app.eval_week import evaluate_workweek

        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        week = evaluate_workweek(db, organization_id=ORG, user_id=USER,
                                 workweek_key=WEEK)
        assert week.day_record_ids

        ghost = max(week.day_record_ids) + 10_000
        broken = dataclasses.replace(
            week, day_record_ids=tuple(week.day_record_ids) + (ghost,))

        with pytest.raises(ProvenanceIncomplete) as excinfo:
            _freeze_provenance(db, broken)
        assert str(ghost) in str(excinfo.value)

    def test_nothing_is_written_when_provenance_cannot_be_frozen(self, db):
        """The refusal must leave no partial row behind."""
        import dataclasses

        from app.eval_event.service import _freeze_provenance
        from app.eval_week import evaluate_workweek

        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        week = evaluate_workweek(db, organization_id=ORG, user_id=USER,
                                 workweek_key=WEEK)
        broken = dataclasses.replace(
            week, day_record_ids=tuple(week.day_record_ids) + (999_999,))
        with pytest.raises(ProvenanceIncomplete):
            _freeze_provenance(db, broken)
        assert _rows(db) == []

    def test_a_missing_record_is_already_refused_one_layer_earlier(self, db):
        """The outer defence still holds: EVAL-WEEK refuses first.

        Recorded so the new guard is understood as a second line, not as the
        only thing standing between a missing record and a written event.
        """
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        db.execute(sa.text(
            "DELETE FROM eco_day_record WHERE organization_id = :o "
            "AND user_id = :u AND work_date = :d"),
            {"o": ORG, "u": USER, "d": _week_dates()[0].isoformat()})
        db.flush()

        result = _run(db)
        assert result.outcome is EventOutcome.REFUSED
        assert result.refusal is RefusalReason.WEEK_NOT_FINAL
        assert _rows(db) == []

    def test_a_blank_content_hash_cannot_reach_the_freezer(self, db):
        """The blank-hash branch is unreachable, and this records WHY.

        ``eco_day_record`` already refuses an empty hash with a CHECK
        constraint, so the guard in ``_freeze_provenance`` is a second line
        that no test can feed through the database. Asserting the upstream
        constraint here means that if it is ever dropped, this test fails and
        whoever drops it learns that the EVAL-EVENT guard has just become
        load-bearing rather than redundant.
        """
        _store_threshold(db, THRESHOLD)
        _full_week(db, short_seconds=SHORT_ABOVE)
        target = db.execute(sa.select(EcoDayRecord.id)).scalars().first()

        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            db.execute(sa.text(
                "UPDATE eco_day_record SET content_hash = '' WHERE id = :i"),
                {"i": target})
        assert "hash_nonempty" in str(excinfo.value)
        db.rollback()

    def test_the_parity_invariant_is_stated_in_the_code(self):
        """The guard must survive a refactor that rebuilds the hash list.

        Whitespace-insensitive on both sides, so reformatting the condition
        does not silently disarm this.
        """
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        squashed = "".join(source.split())
        assert "len(provenance.day_content_hashes)!=len(" in squashed, (
            "the explicit parity check on the assembled provenance is gone")


# ═════════════════════════════════════════════════════════════════════════════
# L — the package boundary (D7, D8, §17)
# ═════════════════════════════════════════════════════════════════════════════

class TestBoundaries:

    def test_nothing_is_imported_from_notifications_or_the_legacy_bus(self):
        for path in _package_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("app.notifications")
                    assert not node.module.startswith("app.events")
                    assert not node.module.startswith("app.rollout")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("app.notifications")
                        assert not alias.name.startswith("app.events")
                        assert not alias.name.startswith("app.rollout")

    def test_no_delivery_scheduling_or_ui_vocabulary(self):
        """D7: EVAL-EVENT generates events. It never delivers one."""
        # ``get`` and ``post`` are deliberately NOT here: ``dict.get`` is an
        # ordinary read and banning it would make this guard noise rather than
        # evidence. Routing is caught by ``APIRouter``/``router``/``endpoint``,
        # which no dictionary access can produce by accident.
        banned = (
            "send", "send_email", "send_notification", "deliver", "dispatch",
            "enqueue", "notify", "render", "template", "subject", "body",
            "APIRouter", "router", "endpoint", "BackgroundTasks",
            "scheduler", "cron", "celery",
        )
        for path in _package_files():
            tokens = _source_tokens(path)
            for name in banned:
                assert name not in tokens, f"{path.name} names {name!r}"

    def test_no_rollout_state_is_read_or_written(self):
        for path in _package_files():
            tokens = _source_tokens(path)
            for banned in ("RolloutControl", "rollout_state", "set_rollout",
                           "ENABLED", "OFF"):
                assert banned not in tokens, f"{path.name} touches rollout"

    def test_no_horizon_decision_is_made_here(self):
        """D3: the retroactive NOTIFICATION horizon is MODEL-HORIZON's.

        ``is_retroactive`` is RECORDED; whether anyone may be told is not
        decided, which is why a retroactive crossing is still persisted.
        """
        for path in _package_files():
            tokens = _source_tokens(path)
            for banned in ("horizon", "within_horizon", "horizon_days",
                           "eligible", "notification_eligible"):
                assert banned not in tokens, f"{path.name} decides the horizon"

    def test_the_service_never_commits(self):
        """The caller owns the transaction boundary."""
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "commit"

    def test_the_ddl_module_imports_nothing(self):
        """It is imported by an Alembic revision, which must not drag models,
        metadata or a session into its own runtime."""
        tree = ast.parse(
            (PACKAGE / "append_only_ddl.py").read_text(encoding="utf-8"))
        imports = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert [n.module for n in imports
                if isinstance(n, ast.ImportFrom)] == ["__future__"]


# ═════════════════════════════════════════════════════════════════════════════
# M — the migration
# ═════════════════════════════════════════════════════════════════════════════

class TestMigration:

    def test_the_revision_and_its_parent_are_pinned(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        assigned = {}
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                              ast.Name):
                target = node.target.id
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
            if target in ("revision", "down_revision") \
                    and isinstance(node.value, ast.Constant):
                assigned[target] = node.value.value
        assert assigned["revision"] == EXPECTED_REVISION
        assert assigned["down_revision"] == EXPECTED_PARENT_REVISION

    def test_the_migration_emits_the_shared_append_only_ddl(self):
        """Not a transcription: ``op.create_table`` builds its own ``Table``,
        so the model's ``after_create`` listener does not fire for it. One
        definition, two emitters."""
        source = MIGRATION.read_text(encoding="utf-8")
        assert "from app.eval_event.append_only_ddl import" in source
        assert "append_only_statements(dialect)" in source
        assert "drop_statements(dialect)" in source

    def test_the_migration_is_purely_additive(self):
        source = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(source)
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef)
                       and n.name == "upgrade")
        called = {n.func.attr for n in ast.walk(upgrade)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        for banned in ("drop_table", "drop_column", "alter_column",
                       "rename_table"):
            assert banned not in called

    def test_migration_and_model_agree_on_every_column(self, engine):
        """A create_all database and a migrated database must be the same
        shape, or every below-ORM test passes vacuously."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        probe = sa.create_engine("sqlite://")
        from database.models import Organization, User

        Organization.__table__.create(probe, checkfirst=True)
        User.__table__.create(probe, checkfirst=True)

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_eco_threshold_event_rev", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with probe.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.upgrade()
            conn.commit()

            migrated = {c["name"] for c in sa.inspect(conn).get_columns(
                "eco_threshold_event")}
            triggers = set(conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )).scalars().all())

        expected = {c.name for c in EcoThresholdEvent.__table__.columns}
        assert migrated == expected
        assert {"eco_threshold_event_no_update",
                "eco_threshold_event_no_delete"} <= triggers

    def test_the_migration_round_trips(self):
        """upgrade -> downgrade -> upgrade, on a private in-memory database."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        probe = sa.create_engine("sqlite://")
        from database.models import Organization, User

        Organization.__table__.create(probe, checkfirst=True)
        User.__table__.create(probe, checkfirst=True)

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_eco_threshold_event_roundtrip", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with probe.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.upgrade()
                assert "eco_threshold_event" in sa.inspect(
                    conn).get_table_names()
                module.downgrade()
                assert "eco_threshold_event" not in sa.inspect(
                    conn).get_table_names()
                module.upgrade()
                assert "eco_threshold_event" in sa.inspect(
                    conn).get_table_names()
            conn.rollback()

    def test_the_table_compiles_for_postgresql(self):
        from sqlalchemy.dialects import postgresql

        ddl = sa.schema.CreateTable(EcoThresholdEvent.__table__).compile(
            dialect=postgresql.dialect()).string
        assert "eco_threshold_event" in ddl
        assert "ON DELETE RESTRICT" in ddl


# ═════════════════════════════════════════════════════════════════════════════
# N — the vocabulary (§32)
# ═════════════════════════════════════════════════════════════════════════════

class TestVocabulary:

    def test_the_event_names_are_exactly_these(self):
        assert EventType.ECO_THRESHOLD_REACHED.value == REACHED
        assert EventType.ECO_THRESHOLD_WITHDRAWN.value == WITHDRAWN
        assert len(set(EventType)) == 2

    def test_the_modes_are_exactly_these(self):
        assert {m.value for m in EventMode} == {OPERATIONAL, SHADOW}

    def test_the_outcomes_are_exactly_these(self):
        assert {o.value for o in EventOutcome} == {
            "CROSSED", "RE_CROSSED", "ALREADY_CROSSED", "NOT_CROSSED",
            "WITHDRAWN", "REFUSED"}

    def test_the_event_tier_is_not_the_configuration_tier(self):
        from app.eco_threshold import ECO_THRESHOLD_TIER
        from app.eval_event import EventTier

        assert EventTier.PRIMARY.value == PRIMARY
        assert ECO_THRESHOLD_TIER != EventTier.PRIMARY.value

    def test_v1_vocabulary_never_appears(self):
        """ECO v1 stays permanently disabled; none of its names may leak in."""
        for path in _package_files():
            tokens = {t.lower() for t in _source_tokens(path)}
            for banned in ("early_clock_out_alert", "eco_v1", "ecov1"):
                assert banned not in tokens


# ═════════════════════════════════════════════════════════════════════════════
# GOLDEN VECTORS — GV-04, GV-05, GV-08, executed
# ═════════════════════════════════════════════════════════════════════════════
# Promoted once EVAL-EVENT was implemented and independently audited. Executed
# HERE rather than in test_eco_golden_vectors.py for the same reason GV-12 lives
# in the week-fold suite and GV-28 in the classifier suite: the fixtures that
# build a real week, a real threshold and a real event history already exist in
# this file, and a vector should be executed where its behaviour lives.
#
# EVERY expected value is READ FROM THE ARTIFACT at test time and never
# restated. Each test also asserts exhaustiveness, so a field added to a vector
# fails here instead of being silently ignored into a green build.
#
# NOTHING IS RECOMPUTED. W comes from EVAL-WEEK, T from the threshold reader,
# and the lifecycle from EVAL-EVENT. There is no shortfall arithmetic here.

GOLDEN_PATH = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"


def _vector(gid):
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return next(v for v in data["vectors"] if v["id"] == gid)


def _exhaustive(gid, expected, handled):
    unhandled = set(expected) - set(handled)
    assert not unhandled, (
        f"{gid} carries expected_fields this harness never compared: "
        f"{sorted(unhandled)}")


def _week_of_grosses(db, grosses, week=WEEK):
    """A seven-day week whose working days carry the given gross shortfalls.

    The vectors describe a week as an accumulation ("prior week 20; today gross
    10"), so the days are unequal. Gross minutes are turned back into worked
    seconds and handed to the real evaluator — this helper never computes a
    shortfall itself, and the resulting weekly total is asserted against the
    artifact rather than assumed.
    """
    dates = _week_dates(week)
    for index, on in enumerate(dates):
        if index < len(grosses):
            _day(db, on=on,
                 worked_seconds=(REQUIRED_MINUTES - grosses[index]) * 60,
                 week=week)
        else:
            _day(db, on=on, state=LedgerState.NOT_A_WORKING_DAY.value,
                 week=week)


class TestGV04:
    """GV-04, executed: "Threshold at exactly 30".

    §21.1: the comparison is W(w,k) >= T(t,k). Exactly 30 triggers, and
    implementing it as strictly-greater-than is a defect — which is why the
    threshold is stored at exactly the weekly total the vector describes.
    """

    def test_gv04_threshold_at_exactly_thirty(self, db):
        expected = _vector("GV-04")["expected_fields"]

        _store_threshold(db, expected["weekly_total_minutes"])
        _week_of_grosses(db, [20, 10, 0, 0, 0])

        from app.eval_week import evaluate_workweek

        week = evaluate_workweek(db, organization_id=ORG, user_id=USER,
                                 workweek_key=WEEK)
        result = _run(db)
        events = read_events(db, organization_id=ORG, user_id=USER,
                             workweek_key=WEEK)
        handled = []

        assert week.weekly_attributable_shortfall_minutes == \
            expected["weekly_total_minutes"]
        assert result.weekly_total_minutes == expected["weekly_total_minutes"]
        handled.append("weekly_total_minutes")

        assert result.crossed is expected["crossed"]
        handled.append("crossed")

        assert [e.event_type for e in events] == expected["events_emitted"]
        handled.append("events_emitted")

        assert len(events) == expected["event_count"]
        handled.append("event_count")

        _exhaustive("GV-04", expected, handled)

        # Everything else the vector's prose requires: ONE primary event, and
        # the equality case specifically.
        assert result.threshold_minutes == expected["weekly_total_minutes"], \
            "this vector is the equality case; W and T must be equal"
        assert events[0].sequence == 1
        assert events[0].tier == PRIMARY
        assert events[0].mode == OPERATIONAL
        assert events[0].supersedes_event_id is None
        assert events[0].worker_recipient == USER

    def test_gv04_is_idempotent(self, db):
        """One ECO_THRESHOLD_REACHED, and it survives a replay."""
        expected = _vector("GV-04")["expected_fields"]
        _store_threshold(db, expected["weekly_total_minutes"])
        _week_of_grosses(db, [20, 10, 0, 0, 0])
        _run(db)
        assert _run(db).outcome is EventOutcome.ALREADY_CROSSED
        assert len(_rows(db)) == expected["event_count"]


class TestGV05:
    """GV-05, executed: "Threshold at 29".

    One minute below the threshold. The GV-04/GV-05 pair is what makes the
    comparison testable at all: an implementation using ``>`` passes GV-05 and
    fails GV-04, and one that always crosses does the reverse.
    """

    def test_gv05_threshold_at_twenty_nine(self, db):
        expected = _vector("GV-05")["expected_fields"]
        gv04 = _vector("GV-04")["expected_fields"]

        # T is GV-04's crossing figure; this week lands one minute below it.
        _store_threshold(db, gv04["weekly_total_minutes"])
        _week_of_grosses(db, [20, 9, 0, 0, 0])

        from app.eval_week import evaluate_workweek

        week = evaluate_workweek(db, organization_id=ORG, user_id=USER,
                                 workweek_key=WEEK)
        result = _run(db)
        events = read_events(db, organization_id=ORG, user_id=USER,
                             workweek_key=WEEK)
        handled = []

        assert week.weekly_attributable_shortfall_minutes == \
            expected["weekly_total_minutes"]
        assert result.weekly_total_minutes == expected["weekly_total_minutes"]
        handled.append("weekly_total_minutes")

        assert result.crossed is expected["crossed"]
        handled.append("crossed")

        assert [e.event_type for e in events] == expected["events_emitted"]
        handled.append("events_emitted")

        assert len(events) == expected["event_count"]
        handled.append("event_count")

        _exhaustive("GV-05", expected, handled)

        # "no event" means NO ROW EXISTS — not a row that went unnotified.
        # Counted below the ORM so no reader can be blamed for hiding one.
        persisted = db.execute(sa.text(
            "SELECT count(*) FROM eco_threshold_event")).scalar()
        assert persisted == 0
        assert result.event_id is None
        assert result.weekly_total_minutes < result.threshold_minutes


class TestGV08:
    """GV-08, executed: "Re-crossing (§21.4)".

    The full lifecycle: a crossing, a de-crossing that withdraws it, and a new
    primary at sequence 2 referencing the withdrawal. The withdrawal shares the
    crossing's sequence, so one number is one complete lifecycle.

    GV-08's input chain begins at GV-07, which remains ALERT-blocked. Only
    GV-07's STATE is needed here — a withdrawal — and that state is EVAL-EVENT's
    to produce. None of GV-08's own expected fields asserts notification
    behaviour, so it does not inherit that block.
    """

    def _lifecycle(self, db):
        expected = _vector("GV-08")["expected_fields"]
        gv04 = _vector("GV-04")["expected_fields"]

        _store_threshold(db, gv04["weekly_total_minutes"])
        _week_of_grosses(db, [20, 10, 0, 0, 0])          # GV-04: weekly 30
        crossing = _run(db)
        assert crossing.outcome is EventOutcome.CROSSED

        _correct_days(db, count=1, short_seconds=10 * 60, version=2)
        withdrawal = _run(db)                             # GV-07: -10 -> 20
        assert withdrawal.outcome is EventOutcome.WITHDRAWN

        _correct_days(db, count=1, short_seconds=22 * 60, version=3)
        return expected, crossing, withdrawal, _run(db)   # +12 -> 32

    def test_gv08_re_crossing(self, db):
        expected, crossing, withdrawn, result = self._lifecycle(db)

        from app.eval_week import evaluate_workweek

        week = evaluate_workweek(db, organization_id=ORG, user_id=USER,
                                 workweek_key=WEEK)
        events = read_events(db, organization_id=ORG, user_id=USER,
                             workweek_key=WEEK)
        withdrawal_row = next(e for e in events if e.event_type == WITHDRAWN)
        recrossing = events[-1]
        handled = []

        assert week.weekly_attributable_shortfall_minutes == \
            expected["weekly_total_minutes"]
        assert result.weekly_total_minutes == expected["weekly_total_minutes"]
        handled.append("weekly_total_minutes")

        assert result.sequence == expected["sequence"]
        handled.append("sequence")

        assert (recrossing.supersedes_event_id == withdrawal_row.id) is \
            expected["references_prior_withdrawal"]
        handled.append("references_prior_withdrawal")

        _exhaustive("GV-08", expected, handled)

        # "new primary event": a NEW row, and the lifecycle in order.
        assert result.outcome is EventOutcome.RE_CROSSED
        assert recrossing.event_type == REACHED
        assert [(e.event_type, e.sequence) for e in events] == [
            (REACHED, 1), (WITHDRAWN, 1), (REACHED, expected["sequence"])]

    def test_gv08_leaves_the_original_crossing_intact(self, db):
        """R-21.3.1 still holds after the re-crossing."""
        expected, crossing, _withdrawn, _result = self._lifecycle(db)
        events = read_events(db, organization_id=ORG, user_id=USER,
                             workweek_key=WEEK)
        original = events[0]
        assert original.id == crossing.event_id
        assert original.sequence == 1
        assert original.supersedes_event_id is None
        assert original.event_type == REACHED

    def test_gv08_creates_no_third_sequence(self, db):
        """A re-crossing follows a withdrawal — a replay follows nothing."""
        expected, _c, _w, _r = self._lifecycle(db)
        again = _run(db)
        assert again.outcome is EventOutcome.ALREADY_CROSSED
        crossings = [e.sequence for e in _rows(db) if e.event_type == REACHED]
        assert crossings == [1, expected["sequence"]]
