"""
WORKER CONTEXT HISTORY — foundation tests.
==========================================

The property under test throughout: a past day resolves to the classification
that was true on that day, and to UNKNOWN when that cannot be established —
never to today's value, never to a default.

Expected values are written independently. GV-27's requirement minutes,
provenance strings, vocabulary names and dates are literals a reader can check
by hand, not imports of the constants the implementation used.

ON FOREIGN-KEY ENFORCEMENT: the shared engine leaves ``PRAGMA foreign_keys``
off, so cross-tenant behaviour is asserted through the resolver's own filters
rather than through an FK that would not fire.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.worker_context import (
    ContextOutcome,
    ContextPeriod,
    InvalidWorkerContext,
    StaleWorkerContextVersion,
    WorkerContextImmutable,
    WorkerContextOrganizationRequired,
    WorkerContextTimeline,
    current_timeline_version,
    read_timeline,
    record_timeline,
    set_worker_context,
    worker_context_as_of,
)

UTC = timezone.utc
ORG = 41
OTHER_ORG = 42

#: Written literally, never imported.
PRODUCT_VOCAB = "product.employment_type"
CAPTURED, CORRECTION, CREATION_DEFAULT = "CAPTURED", "CORRECTION", "CREATION_DEFAULT"
PART_TIME, FULL_TIME = "part_time", "full_time"

#: GV-27's week. Monday 2026-08-31 .. Friday 2026-09-04.
MON, TUE, WED, THU, FRI = (
    date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2),
    date(2026, 9, 3), date(2026, 9, 4),
)
#: Requirement minutes, stated independently of any implementation constant.
PART_TIME_MINUTES, FULL_TIME_MINUTES = 240, 525

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "worker_context"
MIGRATION = (
    BACKEND / "alembic" / "versions" / "b8c3f6a1d472_worker_context_timeline.py"
)
EXPECTED_REVISION = "b8c3f6a1d472"
EXPECTED_PARENT_REVISION = "a7e2d5f8b031"


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import Organization, User

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    WorkerContextTimeline.__table__.create(engine, checkfirst=True)


_next_id = [7000]


def _worker(db, *, org=ORG, employment_type=FULL_TIME):
    from database.models import User

    _next_id[0] += 1
    uid = _next_id[0]
    u = User(id=uid, name=f"W{uid}", email=f"w{uid}@example.test",
             organization_id=org, employment_type=employment_type)
    db.add(u)
    db.flush()
    return u


def _period(eff, value, provenance=CAPTURED):
    return ContextPeriod(effective_from=eff, worker_context=value,
                         context_vocabulary=PRODUCT_VOCAB, provenance=provenance)


def _required_minutes(resolution):
    """Independent mapping from context to requirement.

    Written here rather than imported: this is the mapping GV-27 asserts, and a
    test that imported the implementation's table could not detect it changing.
    """
    if not resolution.is_resolved:
        return None
    return {PART_TIME: PART_TIME_MINUTES, FULL_TIME: FULL_TIME_MINUTES}[
        resolution.worker_context
    ]


# ═════════════════════════════════════════════════════════════════════════════
# GV-27 — the worked example
# ═════════════════════════════════════════════════════════════════════════════
class TestGV27:
    def test_the_five_day_history_resolves_exactly(self, db):
        """Part-Time Mon-Tue, Full-Time Wed-Fri -> [240,240,525,525,525]."""
        w = _worker(db, employment_type=PART_TIME)
        record_timeline(db, user=w, periods=[
            _period(MON, PART_TIME),
            _period(WED, FULL_TIME),
        ], expected_version=None, now=NOW)

        got = [
            _required_minutes(worker_context_as_of(
                db, organization_id=ORG, user_id=w.id, as_of=day))
            for day in (MON, TUE, WED, THU, FRI)
        ]
        assert got == [240, 240, 525, 525, 525]

    def test_monday_still_resolves_part_time_after_the_change(self, db):
        """The reproducibility property GV-27 exists to pin.

        Re-reading Monday AFTER the Wednesday change must not return Full-Time,
        which is precisely what the mutable column did.
        """
        w = _worker(db, employment_type=PART_TIME)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        before = worker_context_as_of(db, organization_id=ORG, user_id=w.id,
                                      as_of=MON)

        set_worker_context(db, user=w, worker_context=FULL_TIME,
                           effective_from=WED, expected_version=1, now=NOW)
        after = worker_context_as_of(db, organization_id=ORG, user_id=w.id,
                                     as_of=MON)

        assert before.worker_context == after.worker_context == PART_TIME
        assert _required_minutes(after) == 240

    def test_the_period_boundary_is_inclusive(self, db):
        """A period effective Wednesday governs Wednesday, not Thursday."""
        w = _worker(db)
        record_timeline(db, user=w, periods=[
            _period(MON, PART_TIME), _period(WED, FULL_TIME),
        ], expected_version=None, now=NOW)
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=w.id, as_of=TUE
        ).worker_context == PART_TIME
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=w.id, as_of=WED
        ).worker_context == FULL_TIME


# ═════════════════════════════════════════════════════════════════════════════
# Historical floor — H-1
# ═════════════════════════════════════════════════════════════════════════════
class TestHistoricalFloor:
    def test_a_worker_with_no_timeline_is_unknown(self, db):
        w = _worker(db)
        r = worker_context_as_of(db, organization_id=ORG, user_id=w.id, as_of=MON)
        assert r.outcome is ContextOutcome.UNKNOWN_NO_HISTORY
        assert r.worker_context is None

    def test_a_date_below_the_floor_is_unknown(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(WED, FULL_TIME)],
                        expected_version=None, now=NOW)
        r = worker_context_as_of(db, organization_id=ORG, user_id=w.id, as_of=MON)
        assert r.outcome is ContextOutcome.UNKNOWN_BELOW_FLOOR
        assert r.worker_context is None

    def test_the_two_unknowns_are_distinguishable(self, db):
        """Different remediation: never captured vs captured-but-later."""
        never = _worker(db)
        captured = _worker(db)
        record_timeline(db, user=captured, periods=[_period(WED, FULL_TIME)],
                        expected_version=None, now=NOW)

        a = worker_context_as_of(db, organization_id=ORG, user_id=never.id, as_of=MON)
        b = worker_context_as_of(db, organization_id=ORG, user_id=captured.id, as_of=MON)
        assert a.outcome is not b.outcome

    def test_below_the_floor_never_falls_back_to_the_current_value(self, db):
        """The whole point of H-1. The projection says full_time; March does not."""
        w = _worker(db, employment_type=FULL_TIME)
        record_timeline(db, user=w, periods=[_period(WED, FULL_TIME)],
                        expected_version=None, now=NOW)
        r = worker_context_as_of(db, organization_id=ORG, user_id=w.id,
                                 as_of=date(2026, 3, 14))
        assert r.worker_context is None
        assert w.employment_type == FULL_TIME   # the projection is unchanged

    def test_an_unresolved_outcome_cannot_carry_a_value(self):
        """Structural: the guarantee is enforced by the type, not by callers."""
        from app.worker_context import WorkerContextResolution

        with pytest.raises(InvalidWorkerContext):
            WorkerContextResolution(
                outcome=ContextOutcome.UNKNOWN_NO_HISTORY,
                organization_id=ORG, user_id=1, as_of=MON,
                worker_context=FULL_TIME,
            )

    @pytest.mark.parametrize("org,user", [(None, 1), (ORG, None), (None, None)])
    def test_missing_identity_is_unknown_not_an_exception(self, db, org, user):
        r = worker_context_as_of(db, organization_id=org, user_id=user, as_of=MON)
        assert r.is_unknown

    def test_a_database_error_is_unknown_not_an_exception(self):
        """A resolver that throws while consulted fails a day for the wrong reason."""
        class _Exploding:
            def execute(self, *a, **kw):
                raise RuntimeError("database is on fire")

        r = worker_context_as_of(_Exploding(), organization_id=ORG, user_id=1,
                                 as_of=MON)
        assert r.is_unknown

    def test_no_input_produces_a_default_classification(self, db):
        """D-10's unknown -> full_time must be unreachable from this path."""
        w = _worker(db)
        for probe in (MON, date(2020, 1, 1), date(2099, 12, 31)):
            r = worker_context_as_of(db, organization_id=ORG, user_id=w.id,
                                     as_of=probe)
            assert r.worker_context is None


# ═════════════════════════════════════════════════════════════════════════════
# Current projection parity
# ═════════════════════════════════════════════════════════════════════════════
class TestProjectionParity:
    def test_as_of_today_equals_the_projection(self, db):
        w = _worker(db, employment_type=PART_TIME)
        record_timeline(db, user=w, periods=[_period(MON, FULL_TIME)],
                        expected_version=None, now=NOW)
        today = NOW.date()
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=w.id, as_of=today
        ).worker_context == w.employment_type

    def test_a_change_updates_the_projection(self, db):
        w = _worker(db, employment_type=PART_TIME)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        assert w.employment_type == PART_TIME
        set_worker_context(db, user=w, worker_context=FULL_TIME,
                           effective_from=WED, expected_version=1, now=NOW)
        assert w.employment_type == FULL_TIME

    def test_a_future_dated_change_does_not_move_the_projection(self, db):
        """The subtle case: the newest period is not today's answer."""
        w = _worker(db, employment_type=PART_TIME)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        future = NOW.date() + timedelta(days=30)
        set_worker_context(db, user=w, worker_context=FULL_TIME,
                           effective_from=future, expected_version=1, now=NOW)

        assert w.employment_type == PART_TIME, "a future change moved today"
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=w.id, as_of=NOW.date()
        ).worker_context == PART_TIME
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=w.id, as_of=future
        ).worker_context == FULL_TIME

    def test_a_failed_write_leaves_the_projection_untouched(self, db):
        """Atomicity: the projection must never advance without its history."""
        w = _worker(db, employment_type=PART_TIME)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)

        with pytest.raises(StaleWorkerContextVersion):
            set_worker_context(db, user=w, worker_context=FULL_TIME,
                               effective_from=WED, expected_version=99, now=NOW)
        assert w.employment_type == PART_TIME
        assert current_timeline_version(
            db, organization_id=ORG, user_id=w.id) == 1
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Corrections, supersession, concurrency, idempotency
# ═════════════════════════════════════════════════════════════════════════════
class TestCorrections:
    def test_a_correction_appends_a_version_and_preserves_the_prior(self, db):
        """"We said the 3rd; it was the 5th" — re-dating without mutation."""
        w = _worker(db)
        record_timeline(db, user=w, periods=[
            _period(MON, PART_TIME), _period(WED, FULL_TIME),
        ], expected_version=None, now=NOW)

        record_timeline(db, user=w, periods=[
            _period(MON, PART_TIME, CORRECTION),
            _period(FRI, FULL_TIME, CORRECTION),
        ], expected_version=1, now=NOW)

        v1 = read_timeline(db, organization_id=ORG, user_id=w.id, timeline_version=1)
        v2 = read_timeline(db, organization_id=ORG, user_id=w.id, timeline_version=2)
        assert [p.effective_from for p in v1] == [MON, WED]
        assert [p.effective_from for p in v2] == [MON, FRI]
        # Wednesday now resolves part_time under the corrected timeline.
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=w.id, as_of=WED
        ).worker_context == PART_TIME

    def test_supersedes_version_chains_backwards(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        record_timeline(db, user=w, periods=[_period(MON, FULL_TIME, CORRECTION)],
                        expected_version=1, now=NOW)
        v1 = read_timeline(db, organization_id=ORG, user_id=w.id, timeline_version=1)
        v2 = read_timeline(db, organization_id=ORG, user_id=w.id, timeline_version=2)
        assert v1[0].supersedes_version is None
        assert v2[0].supersedes_version == 1

    def test_an_earlier_period_survives_a_later_change(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        set_worker_context(db, user=w, worker_context=FULL_TIME,
                           effective_from=WED, expected_version=1, now=NOW)
        assert [p.effective_from for p in read_timeline(
            db, organization_id=ORG, user_id=w.id)] == [MON, WED]

    def test_a_stale_write_is_rejected_not_merged(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        record_timeline(db, user=w, periods=[_period(MON, FULL_TIME, CORRECTION)],
                        expected_version=1, now=NOW)
        with pytest.raises(StaleWorkerContextVersion):
            record_timeline(db, user=w, periods=[_period(MON, PART_TIME, CORRECTION)],
                            expected_version=1, now=NOW)
        db.rollback()

    def test_a_first_write_against_an_existing_timeline_is_rejected(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        with pytest.raises(StaleWorkerContextVersion):
            record_timeline(db, user=w, periods=[_period(MON, FULL_TIME)],
                            expected_version=None, now=NOW)
        db.rollback()

    def test_replaying_an_identical_timeline_is_a_no_op(self, db):
        w = _worker(db)
        first = record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                                expected_version=None, now=NOW)
        replay = record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                                 expected_version=1, now=NOW)
        assert replay.created is False
        assert replay.timeline_version == first.timeline_version == 1

    def test_a_replay_is_recognised_regardless_of_order(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[
            _period(MON, PART_TIME), _period(WED, FULL_TIME),
        ], expected_version=None, now=NOW)
        replay = record_timeline(db, user=w, periods=[
            _period(WED, FULL_TIME), _period(MON, PART_TIME),
        ], expected_version=1, now=NOW)
        assert replay.created is False

    def test_a_recorded_period_cannot_be_modified(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        row = read_timeline(db, organization_id=ORG, user_id=w.id)[0]
        row.worker_context = FULL_TIME
        with pytest.raises(WorkerContextImmutable):
            db.flush()
        db.rollback()

    def test_a_recorded_period_cannot_be_deleted(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        row = read_timeline(db, organization_id=ORG, user_id=w.id)[0]
        db.delete(row)
        with pytest.raises(WorkerContextImmutable):
            db.flush()
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# Validation, provenance, vocabulary
# ═════════════════════════════════════════════════════════════════════════════
class TestValidation:
    def test_an_untenanted_worker_is_refused(self, db):
        w = _worker(db, org=None)
        with pytest.raises(WorkerContextOrganizationRequired):
            record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                            expected_version=None, now=NOW)
        db.rollback()

    def test_an_empty_timeline_is_refused(self, db):
        w = _worker(db)
        with pytest.raises(InvalidWorkerContext):
            record_timeline(db, user=w, periods=[], expected_version=None, now=NOW)
        db.rollback()

    def test_an_empty_value_is_refused(self, db):
        w = _worker(db)
        with pytest.raises(InvalidWorkerContext):
            record_timeline(db, user=w, periods=[_period(MON, "  ")],
                            expected_version=None, now=NOW)
        db.rollback()

    def test_duplicate_effective_dates_are_refused(self, db):
        w = _worker(db)
        with pytest.raises(InvalidWorkerContext):
            record_timeline(db, user=w, periods=[
                _period(MON, PART_TIME), _period(MON, FULL_TIME),
            ], expected_version=None, now=NOW)
        db.rollback()

    def test_an_undeclared_vocabulary_is_refused(self, db):
        w = _worker(db)
        with pytest.raises(InvalidWorkerContext):
            record_timeline(db, user=w, periods=[ContextPeriod(
                effective_from=MON, worker_context=FULL_TIME,
                context_vocabulary="legal.worker_category", provenance=CAPTURED,
            )], expected_version=None, now=NOW)
        db.rollback()

    @pytest.mark.parametrize("bad", ["ATTESTED", "captured", "GUESSED", ""])
    def test_an_unapproved_provenance_is_refused(self, db, bad):
        """ATTESTED in particular: H-2 was declined and must stay unreachable."""
        w = _worker(db)
        with pytest.raises(InvalidWorkerContext):
            record_timeline(db, user=w, periods=[_period(MON, FULL_TIME, bad)],
                            expected_version=None, now=NOW)
        db.rollback()

    def test_the_approved_provenance_vocabulary_is_exactly_three(self):
        from app.worker_context import WorkerContextProvenance

        assert sorted(p.value for p in WorkerContextProvenance) == [
            "CAPTURED", "CORRECTION", "CREATION_DEFAULT",
        ]

    def test_creation_default_is_not_a_deliberate_decision(self, db):
        """A database default is not evidence that anybody chose full_time."""
        w = _worker(db)
        record_timeline(db, user=w,
                        periods=[_period(MON, FULL_TIME, CREATION_DEFAULT)],
                        expected_version=None, now=NOW)
        r = worker_context_as_of(db, organization_id=ORG, user_id=w.id, as_of=MON)
        assert r.is_resolved
        assert r.provenance == CREATION_DEFAULT
        assert r.is_deliberate is False

    def test_captured_is_a_deliberate_decision(self, db):
        w = _worker(db)
        record_timeline(db, user=w, periods=[_period(MON, FULL_TIME, CAPTURED)],
                        expected_version=None, now=NOW)
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=w.id, as_of=MON).is_deliberate is True


class TestStorageConstraints:
    def _row(self, db, w, **kw):
        base = dict(
            organization_id=ORG, user_id=w.id, timeline_version=1,
            effective_from=MON, worker_context=FULL_TIME,
            context_vocabulary=PRODUCT_VOCAB, provenance=CAPTURED,
            recorded_at_utc=NOW,
        )
        base.update(kw)
        row = WorkerContextTimeline(**base)
        db.add(row)
        db.flush()
        return row

    def test_version_zero_is_refused(self, db):
        w = _worker(db)
        with pytest.raises(IntegrityError):
            self._row(db, w, timeline_version=0)
        db.rollback()

    @pytest.mark.parametrize("field", ["worker_context", "context_vocabulary",
                                       "provenance"])
    def test_empty_required_text_is_refused(self, db, field):
        w = _worker(db)
        with pytest.raises(IntegrityError):
            self._row(db, w, **{field: ""})
        db.rollback()

    def test_two_periods_cannot_share_a_date_in_one_version(self, db):
        w = _worker(db)
        self._row(db, w)
        with pytest.raises(IntegrityError):
            self._row(db, w)
        db.rollback()

    def test_both_identity_columns_are_required(self):
        for col in ("organization_id", "user_id"):
            assert WorkerContextTimeline.__table__.c[col].nullable is False


# ═════════════════════════════════════════════════════════════════════════════
# Organization isolation
# ═════════════════════════════════════════════════════════════════════════════
class TestOrganizationIsolation:
    def test_another_organization_never_sees_the_value(self, db):
        w = _worker(db, org=ORG)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        r = worker_context_as_of(db, organization_id=OTHER_ORG, user_id=w.id,
                                 as_of=MON)
        assert r.is_unknown
        assert r.worker_context is None

    def test_two_organizations_hold_independent_timelines(self, db):
        a = _worker(db, org=ORG)
        b = _worker(db, org=OTHER_ORG)
        record_timeline(db, user=a, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        record_timeline(db, user=b, periods=[_period(MON, FULL_TIME)],
                        expected_version=None, now=NOW)
        assert worker_context_as_of(
            db, organization_id=ORG, user_id=a.id, as_of=MON
        ).worker_context == PART_TIME
        assert worker_context_as_of(
            db, organization_id=OTHER_ORG, user_id=b.id, as_of=MON
        ).worker_context == FULL_TIME

    def test_reads_are_filtered_on_organization_as_well_as_worker(self, db):
        w = _worker(db, org=ORG)
        record_timeline(db, user=w, periods=[_period(MON, PART_TIME)],
                        expected_version=None, now=NOW)
        assert read_timeline(db, organization_id=OTHER_ORG, user_id=w.id) == ()


# ═════════════════════════════════════════════════════════════════════════════
# The AST guard — one writer for User.employment_type
# ═════════════════════════════════════════════════════════════════════════════
def employment_type_write_sites(tree: ast.Module, label: str) -> set:
    """Every place that could set ``employment_type``.

    Three shapes, because the real repository uses all three:

    * ``x.employment_type = ...``            — direct attribute assignment
    * ``User(..., employment_type=...)``     — keyword at USER construction only
    * ``"employment_type"`` in a container literal **in a module that also calls
      ``setattr``** — the allow-list idiom driving a dynamic write, which no
      attribute-based scan can see. ``employees/router.py:729`` mutates through
      exactly that shape.

    Both of the last two branches are deliberately narrowed, because the broad
    forms cry wolf on the module that legitimately RESOLVES employment type:
    ``schedule_policy/resolver.py`` builds an ``EffectiveSchedule`` dataclass
    with a field of that name (a read-model output), and lists the name in a
    provenance ``sources`` map. Neither writes to a Worker record. A guard that
    flags reads is a guard people learn to ignore.
    """
    sites = set()
    module_uses_setattr = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "setattr"
        for n in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "employment_type":
                    sites.add(f"{label}:{node.lineno}")
        elif isinstance(node, ast.AnnAssign):
            t = node.target
            if isinstance(t, ast.Attribute) and t.attr == "employment_type":
                sites.add(f"{label}:{node.lineno}")
        elif isinstance(node, ast.Call):
            func = node.func
            constructs_user = (
                (isinstance(func, ast.Name) and func.id == "User")
                or (isinstance(func, ast.Attribute) and func.attr == "User")
            )
            if constructs_user:
                for kw in node.keywords:
                    if kw.arg == "employment_type":
                        sites.add(f"{label}:{node.lineno}")
        elif module_uses_setattr and isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and elt.value == "employment_type":
                    sites.add(f"{label}:{node.lineno}")
    return sites


def _scan_app() -> set:
    sites = set()
    for path in (BACKEND / "app").rglob("*.py"):
        rel = str(path.relative_to(BACKEND)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        sites |= employment_type_write_sites(tree, rel)
    return sites


class TestAstGuardItself:
    """The guard must be tested before it is trusted (§19.13-14).

    Known-positive and known-negative fixtures, parsed from source strings, so
    the guard cannot become tautological by matching nothing.
    """

    @pytest.mark.parametrize("src", [
        "emp.employment_type = 'full_time'",
        "user.employment_type: str = 'part_time'",
        "u = models.User(name='a', employment_type='full_time')",
        # The real employees/router.py shape: a name allow-list plus setattr.
        "ALLOWED = {'name', 'employment_type'}\n"
        "for k, v in d.items():\n"
        "    setattr(emp, k, v)",
        "FIELDS = ['employment_type']\nsetattr(o, f, v)",
        "PAIR = ('employment_type', 'role')\nsetattr(o, f, v)",
    ])
    def test_known_positives_are_detected(self, src):
        assert employment_type_write_sites(ast.parse(src), "x") != set()

    @pytest.mark.parametrize("src", [
        "x = user.employment_type",
        "print(employment_type)",
        "d = {'employment_type': value}",
        "'employment_type in a docstring'",
        "normalize(user.employment_type)",
        "if user.employment_type == 'part_time': pass",
        # A read-model dataclass with a field of the same name is not a write
        # to the Worker record — schedule_policy/resolver.py does exactly this.
        "s = EffectiveSchedule(employment_type=et, work_mode='onsite')",
        "employment_type: str = 'full_time'",
        # A name list with no setattr anywhere is data, not a write path —
        # schedule_policy/resolver.py's provenance `sources` map.
        "sources = {k: 'system' for k in ('work_mode', 'employment_type')}",
    ])
    def test_known_negatives_are_not_flagged(self, src):
        assert employment_type_write_sites(ast.parse(src), "x") == set()

    def test_the_scan_reads_a_non_trivial_number_of_modules(self):
        modules = list((BACKEND / "app").rglob("*.py"))
        assert len(modules) > 100, f"only {len(modules)} modules scanned"


class TestOneWriterInvariant:
    #: The EXACT write sites as they stand today, pinned per site rather than
    #: per module so a fourth site inside employees/router.py cannot slip
    #: through. `employees/router.py` is NOT authorised for change in this step
    #: — B6 is a separate platform workstream — so its three sites are the
    #: recorded unmigrated baseline. Any new site anywhere fails this test.
    KNOWN_UNMIGRATED_MODULES = {
        "app/employees/router.py",       # create, create-with-password, allow-list
        "app/worker_context/service.py",  # the approved single writer
    }
    KNOWN_UNMIGRATED_SITE_COUNTS = {
        "app/employees/router.py": 3,
        "app/worker_context/service.py": 1,
    }

    def test_no_unexpected_module_writes_employment_type(self):
        modules = {site.rsplit(":", 1)[0] for site in _scan_app()}
        unexpected = modules - self.KNOWN_UNMIGRATED_MODULES
        assert unexpected == set(), (
            f"new employment_type write site(s): {sorted(unexpected)}. All "
            f"writes must go through app/worker_context/service.py."
        )

    def test_no_additional_site_appears_inside_a_known_module(self):
        """A module-level allow-list would hide a fourth site in a known file."""
        counts = {}
        for site in _scan_app():
            counts[site.rsplit(":", 1)[0]] = counts.get(site.rsplit(":", 1)[0], 0) + 1
        assert counts == self.KNOWN_UNMIGRATED_SITE_COUNTS, (
            f"write-site count changed: {counts}"
        )

    def test_the_approved_writer_is_the_only_one_in_this_package(self):
        sites = {
            s for s in _scan_app() if s.startswith("app/worker_context/")
        }
        modules = {s.rsplit(":", 1)[0] for s in sites}
        assert modules == {"app/worker_context/service.py"}

    def test_the_known_sites_are_actually_found(self):
        """Guard the guard: if the scan found nothing, the test above passes
        vacuously."""
        modules = {site.rsplit(":", 1)[0] for site in _scan_app()}
        assert "app/employees/router.py" in modules
        assert "app/worker_context/service.py" in modules


# ═════════════════════════════════════════════════════════════════════════════
# Isolation guards
# ═════════════════════════════════════════════════════════════════════════════
def _imported_modules(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def _package_files() -> list:
    files = sorted(PACKAGE.glob("*.py"))
    assert files, f"no modules under {PACKAGE}"
    return files


class TestIsolationGuards:
    def test_the_guard_reads_the_whole_package(self):
        assert {p.name for p in _package_files()} == {
            "__init__.py", "errors.py", "models.py", "service.py", "vocabulary.py",
        }

    @pytest.mark.parametrize("forbidden", [
        "app.wec",                  # observational, not employment history
        "app.payroll",              # does not record employment type at all
        "app.schedule_policy",      # reads the projection; not a history source
        "app.attendance_policy",    # requirement resolution, not context
        "app._worked",              # worked time — a different fact entirely
    ])
    def test_no_secondary_evidence_source_is_consulted(self, forbidden):
        """H-1: nothing here reconstructs history from indirect evidence."""
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if m == forbidden or m.startswith(forbidden + ".")
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_forbidden_modules_actually_exist(self):
        """Guard the guard: a typo'd name can never be imported."""
        import importlib.util

        for name in ("app.wec", "app.payroll", "app.schedule_policy",
                     "app.attendance_policy", "app._worked"):
            assert importlib.util.find_spec(name) is not None, name

    def test_no_eco_or_classifier_dependency(self):
        for path in _package_files():
            offenders = {
                m for m in _imported_modules(path)
                if any(t in m for t in ("early_clock_out", "work_ledger",
                                        "requirement_resolution", "rollout"))
            }
            assert not offenders, f"{path.name} imports {offenders}"

    def test_no_legal_vocabulary_is_declared(self):
        """D11 is open. Only the product vocabulary exists."""
        from app.worker_context import KNOWN_VOCABULARIES

        assert KNOWN_VOCABULARIES == frozenset({"product.employment_type"})

    #: The ONE package authorized to consult this one, and the only wiring that
    #: exists: GV-27's requirement path, under owner ruling R8. The adapter
    #: resolves the historical classification and selects the matching
    #: configuration value; it changes nothing here and writes nothing.
    _AUTHORIZED_CONSUMER = "app/config_versioning/adapter.py"

    def test_only_the_authorized_consumer_wires_this_package(self):
        """This guard inverted at GV-27, deliberately.

        It previously asserted NOTHING consulted this package, which was the
        right claim while the package was built but unwired — the risk then was
        a consumer appearing without a ruling. R8 authorized exactly one, so the
        claim narrows rather than disappearing: every OTHER module in ``app/``
        is still held to the old rule, and a second consumer still fails here.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "worker_context" in path.parts:
                continue
            if path.relative_to(BACKEND).as_posix() == self._AUTHORIZED_CONSUMER:
                continue
            if any(m.startswith("app.worker_context")
                   for m in _imported_modules(path)):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    def test_the_authorized_consumer_really_does_consult_it(self):
        """The other half: the exemption above must not be dead.

        If GV-27's wiring were ever removed, the exemption would silently become
        a licence for a future consumer rather than a record of this one.
        """
        adapter = (BACKEND / self._AUTHORIZED_CONSUMER).read_text(encoding="utf-8")
        assert "worker_context_as_of" in adapter


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════
def _migration_tree() -> ast.Module:
    return ast.parse(MIGRATION.read_text(encoding="utf-8"))


def _module_constant(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {MIGRATION.name}")


def _create_table_calls(tree: ast.Module) -> dict:
    tables = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"):
            tables[node.args[0].value] = [
                a.args[0].value for a in node.args[1:]
                if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                and a.func.attr == "Column"
            ]
    return tables


def _called_attributes(tree: ast.Module) -> set:
    return {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


def _load_migration_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_wc_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migrated_schema():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from database.models import Organization, User

    module = _load_migration_module()
    eng = sa.create_engine("sqlite:///:memory:", future=True)
    Organization.__table__.create(eng, checkfirst=True)
    User.__table__.create(eng, checkfirst=True)
    with eng.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    return eng


class TestMigrationSafety:
    def test_revision_and_parent_are_exactly_as_authorised(self):
        tree = _migration_tree()
        assert _module_constant(tree, "revision") == EXPECTED_REVISION
        assert _module_constant(tree, "down_revision") == EXPECTED_PARENT_REVISION

    def test_it_is_a_single_parent_revision(self):
        assert isinstance(_module_constant(_migration_tree(), "down_revision"), str)

    def test_the_graph_has_exactly_one_head_and_one_root(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert len(sd.get_heads()) == 1, f"the graph forked: {sd.get_heads()}"
        assert len(sd.get_bases()) == 1

    def test_exactly_one_revision_claims_that_parent(self):
        children = []
        for path in (BACKEND / "alembic" / "versions").glob("*.py"):
            try:
                parent = _module_constant(
                    ast.parse(path.read_text(encoding="utf-8")), "down_revision")
            except AssertionError:
                continue
            if parent == EXPECTED_PARENT_REVISION:
                children.append(path.name)
        assert children == [MIGRATION.name]

    def test_the_migration_is_additive_only(self):
        forbidden = {"alter_column", "drop_column", "add_column",
                     "rename_table", "drop_constraint", "drop_index"}
        assert not (_called_attributes(_migration_tree()) & forbidden)

    def test_the_users_table_is_not_touched(self):
        body = MIGRATION.read_text(encoding="utf-8")
        assert "users" not in _create_table_calls(_migration_tree())
        for op_name in ("add_column", "alter_column", "drop_column"):
            assert f"op.{op_name}" not in body

    def test_no_backfill_exists(self):
        """Fabricating history is exactly what H-1 forbids."""
        assert "bulk_insert" not in _called_attributes(_migration_tree())
        for node in _migration_tree().body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    upper = " ".join(inner.value.upper().split())
                    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
                        assert verb not in upper, inner.value

    def test_the_migration_opens_no_connection_of_its_own(self):
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("create_engine", "psycopg", "DATABASE_URL",
                       "postgresql://", "sessionmaker", "load_dotenv"):
            assert banned not in body

    def test_only_the_one_authorised_table_is_created(self):
        assert set(_create_table_calls(_migration_tree())) == {
            "worker_context_timeline"}


class TestMigrationActuallyRuns:
    def test_the_table_is_created(self, migrated_schema):
        assert "worker_context_timeline" in sa.inspect(
            migrated_schema).get_table_names()

    def test_executed_columns_match_the_orm_model(self, migrated_schema):
        actual = sa.inspect(migrated_schema).get_columns("worker_context_timeline")
        assert sorted(c["name"] for c in actual) == sorted(
            c.name for c in WorkerContextTimeline.__table__.columns)

    def test_executed_nullability_matches_the_orm_model(self, migrated_schema):
        actual = {
            c["name"]: c["nullable"]
            for c in sa.inspect(migrated_schema).get_columns(
                "worker_context_timeline")
        }
        assert actual == {
            c.name: c.nullable for c in WorkerContextTimeline.__table__.columns}

    def test_the_migrated_table_is_empty(self, migrated_schema):
        with migrated_schema.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM worker_context_timeline")).scalar() == 0

    def test_the_users_table_was_not_altered(self, migrated_schema):
        from database.models import User

        actual = sorted(c["name"] for c in
                        sa.inspect(migrated_schema).get_columns("users"))
        assert actual == sorted(c.name for c in User.__table__.columns)

    def test_the_uniqueness_rule_is_live(self, migrated_schema):
        with Session(migrated_schema) as s:
            for _ in range(2):
                s.add(WorkerContextTimeline(
                    organization_id=ORG, user_id=1, timeline_version=1,
                    effective_from=MON, worker_context=FULL_TIME,
                    context_vocabulary=PRODUCT_VOCAB, provenance=CAPTURED,
                    recorded_at_utc=NOW))
            with pytest.raises(IntegrityError):
                s.flush()
            s.rollback()

    def test_downgrade_removes_exactly_that_table(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)
        before = set(sa.inspect(eng).get_table_names())
        with eng.begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                module.upgrade()
        after = set(sa.inspect(eng).get_table_names())
        with eng.begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                module.downgrade()
        assert after - before == {"worker_context_timeline"}
        assert set(sa.inspect(eng).get_table_names()) == before

    def test_reapplying_is_a_no_op(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        module = _load_migration_module()
        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)
        for _ in range(2):
            with eng.begin() as c:
                with Operations.context(MigrationContext.configure(c)):
                    module.upgrade()
        assert "worker_context_timeline" in sa.inspect(eng).get_table_names()


class TestPostgresDialectCompilation:
    """SQLite passing does not prove PostgreSQL correctness."""

    @staticmethod
    def _ddl():
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        return str(CreateTable(WorkerContextTimeline.__table__).compile(
            dialect=postgresql.dialect()))

    def test_the_effective_date_compiles_to_a_bare_date(self):
        assert "effective_from DATE" in self._ddl()

    def test_the_capture_instant_is_timezone_aware(self):
        assert "recorded_at_utc TIMESTAMP WITH TIME ZONE" in self._ddl()

    def test_the_value_columns_are_plain_text_not_enums(self):
        ddl = self._ddl()
        assert "worker_context VARCHAR(64)" in ddl
        assert "context_vocabulary VARCHAR(64)" in ddl
        assert "provenance VARCHAR(32)" in ddl
        assert "CREATE TYPE" not in ddl

    def test_the_natural_key_compiles_as_unique(self):
        assert "UNIQUE (user_id, timeline_version, effective_from)" in self._ddl()

    def test_the_organization_reference_restricts(self):
        assert "REFERENCES organizations (id) ON DELETE RESTRICT" in self._ddl()

    def test_check_constraints_survive_compilation(self):
        assert "timeline_version >= 1" in self._ddl()
