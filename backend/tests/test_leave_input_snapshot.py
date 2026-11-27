"""
The immutable leave input snapshot, and day-record authority.
=============================================================

Owner Ruling 2. Three properties under test:

1. **The snapshot is content-addressed and deterministic.** Identical resolved
   leave produces the same key and reuses the same row; any difference in any
   component produces a different key. Contribution order cannot matter.

2. **A historical evaluation is reproducible without reading current leave.**
   The frozen input carries the status each request held at evaluation time and
   the seconds it contributed, so ``approved -> rejected`` afterwards cannot
   change what an older record is said to have used.

3. **Exactly one authoritative record per Worker-day.** Authority is serialized
   by the unique generation, not by the pointer; the predecessor is superseded
   rather than rewritten; and the one permitted mutation is the only one
   allowed.

Literals are transcribed rather than imported where a wrong production constant
could otherwise agree with itself.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession

from app.config_versioning import ConfigValue, ConfigVersion, compute_content_hash
from app.eval_day import (
    DayRecordImmutable,
    EcoDayRecord,
    LeaveContribution,
    LeaveInputContribution,
    LeaveInputSnapshot,
    LeaveSnapshotImmutable,
    compute_leave_snapshot_hash,
    evaluate_and_record_day,
    get_or_create_leave_snapshot,
    leave_interval_fingerprint,
    read_contributions,
    read_leave_snapshot,
)
from app.eval_week import WeeklyState, evaluate_workweek
from app.leave_intervals import LeaveInterval, LeaveIntervalSet
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = timezone.utc
ORG = 101
OTHER_ORG = 102
USER = 10100
OTHER_USER = 10200

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
DAY = date(2026, 8, 31)                 # a Monday; the canonical key IS this
MONDAY_WEEK = "2026-08-31"
REQUIRED_MINUTES = 480

BACKEND = pathlib.Path(__file__).resolve().parents[1]
EVAL_DAY = BACKEND / "app" / "eval_day"
EVAL_WEEK = BACKEND / "app" / "eval_week"

# ── Independently transcribed ────────────────────────────────────────────────
APPROVED = "approved"
LOCKED = "locked_for_payroll"
REJECTED = "rejected"
EXPECTED_REVISION = "d3a1c8b57f92"
EXPECTED_PARENT = "e5f8c2a91d64"
EXPECTED_REVISION_COUNT = 94   # + b5f2a8c47d93 (ECO day record INV-4 break evidence)
#: 73 since the D-18 correction added e7c2b9d4f815, after d5f1a8c3b724, after §45.D-15's c4e9b7d2a583 (clock_session_boundary_event),
#: an ordinary single-parent child of b7c4e0a92d51.


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    from database.models import LeaveRequest, Organization, User

    from app.policy_version import PolicyVersion
    from app.break_treatment import BreakTreatmentProfile
    from app.break_classification import (
        BreakClassificationAssertion, ClassificationSnapshot,
        EcoClassificationAuthority,
    )

    for table in (Organization, User, LeaveRequest):
        table.__table__.create(engine, checkfirst=True)
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


def _c(request_id=7001, set_version=1, status=APPROVED, fingerprint="f" * 8,
       interval_count=1, seconds=900):
    return LeaveContribution(
        leave_request_id=request_id, set_version=set_version, status=status,
        interval_fingerprint=fingerprint, interval_count=interval_count,
        contributed_seconds=seconds,
    )


def _hash(*, overlap_seconds=900, contributions=(), organization_id=ORG,
          user_id=USER, work_date=DAY):
    return compute_leave_snapshot_hash(
        organization_id=organization_id, user_id=user_id, work_date=work_date,
        overlap_seconds=overlap_seconds, contributions=contributions,
    )


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
# STEP 2 — deterministic canonicalisation and hashing (ruling N)
# ═════════════════════════════════════════════════════════════════════════════
class TestCanonicalHash:
    def test_same_content_same_hash(self):
        a = _hash(contributions=[_c()])
        b = _hash(contributions=[_c()])
        assert a == b
        assert len(a) == 64

    def test_contribution_order_does_not_matter(self):
        one, two = _c(request_id=7001), _c(request_id=7002, seconds=60)
        assert _hash(overlap_seconds=960, contributions=[one, two]) == \
            _hash(overlap_seconds=960, contributions=[two, one])

    def test_a_tuple_and_a_list_hash_identically(self):
        assert _hash(contributions=(_c(),)) == _hash(contributions=[_c()])

    @pytest.mark.parametrize("changed", [
        {"status": LOCKED},
        {"status": REJECTED},
        {"set_version": 2},
        {"fingerprint": "g" * 8},
        {"interval_count": 2},
        {"seconds": 901},
        {"request_id": 7002},
    ])
    def test_any_component_change_changes_the_hash(self, changed):
        assert _hash(contributions=[_c()]) != _hash(contributions=[_c(**changed)])

    def test_overlap_seconds_changes_the_hash(self):
        assert _hash(overlap_seconds=900, contributions=[_c()]) != \
            _hash(overlap_seconds=901, contributions=[_c()])

    def test_identity_components_change_the_hash(self):
        base = _hash(contributions=[_c()])
        assert base != _hash(contributions=[_c()], organization_id=OTHER_ORG)
        assert base != _hash(contributions=[_c()], user_id=OTHER_USER)
        assert base != _hash(contributions=[_c()],
                             work_date=DAY + timedelta(days=1))

    def test_the_empty_snapshot_is_deterministic(self):
        assert _hash(overlap_seconds=0, contributions=()) == \
            _hash(overlap_seconds=0, contributions=[])
        # And distinct from a snapshot that had leave.
        assert _hash(overlap_seconds=0, contributions=()) != \
            _hash(contributions=[_c()])

    def test_the_empty_snapshot_differs_per_worker_and_day(self):
        empty = _hash(overlap_seconds=0, contributions=())
        assert empty != _hash(overlap_seconds=0, contributions=(),
                              user_id=OTHER_USER)
        assert empty != _hash(overlap_seconds=0, contributions=(),
                              work_date=DAY + timedelta(days=1))

    def test_approved_and_locked_hash_differently(self):
        """Ruling D: equivalent for ECO classification, distinct as facts."""
        assert _hash(contributions=[_c(status=APPROVED)]) != \
            _hash(contributions=[_c(status=LOCKED)])

    def test_the_interval_fingerprint_is_order_independent(self):
        class _I:
            def __init__(self, s, e, d):
                self.start_at_utc, self.end_at_utc, self.duration_seconds = s, e, d

        a = _I(datetime(2026, 8, 31, 9, tzinfo=UTC),
               datetime(2026, 8, 31, 10, tzinfo=UTC), 3600)
        b = _I(datetime(2026, 8, 31, 11, tzinfo=UTC),
               datetime(2026, 8, 31, 12, tzinfo=UTC), 3600)
        assert leave_interval_fingerprint([a, b]) == \
            leave_interval_fingerprint([b, a])
        assert leave_interval_fingerprint([a]) != leave_interval_fingerprint([b])

    def test_a_naive_instant_is_treated_as_utc(self):
        """SQLite discards tzinfo, so an instant read back is naive. Treating
        that as a different value would make the same leave hash two ways."""
        class _I:
            def __init__(self, s, e, d):
                self.start_at_utc, self.end_at_utc, self.duration_seconds = s, e, d

        aware = _I(datetime(2026, 8, 31, 9, tzinfo=UTC),
                   datetime(2026, 8, 31, 10, tzinfo=UTC), 3600)
        naive = _I(datetime(2026, 8, 31, 9), datetime(2026, 8, 31, 10), 3600)
        assert leave_interval_fingerprint([aware]) == \
            leave_interval_fingerprint([naive])

    def test_the_duration_is_covered(self):
        """A boundary pair cannot be silently re-timed."""
        class _I:
            def __init__(self, s, e, d):
                self.start_at_utc, self.end_at_utc, self.duration_seconds = s, e, d

        begin, end = (datetime(2026, 8, 31, 9, tzinfo=UTC),
                      datetime(2026, 8, 31, 10, tzinfo=UTC))
        assert leave_interval_fingerprint([_I(begin, end, 3600)]) != \
            leave_interval_fingerprint([_I(begin, end, 3599)])

    def test_no_float_or_decimal_hour_appears_in_the_canonical_form(self):
        tokens = _code_tokens(
            ast.parse((EVAL_DAY / "leave_snapshot.py").read_text(
                encoding="utf-8")))
        for banned in ("float", "Decimal", "round", "hours"):
            assert banned not in tokens, f"leave_snapshot.py uses {banned}"

    def test_provenance_is_excluded_from_the_hash(self):
        """``captured_at_utc`` and surrogate ids must not enter the digest, or
        identical leave would hash differently each time it was recorded."""
        import inspect as _inspect

        params = set(_inspect.signature(compute_leave_snapshot_hash).parameters)
        assert params == {"organization_id", "user_id", "work_date",
                          "overlap_seconds", "contributions"}
        assert "captured_at_utc" not in params

    def test_a_contribution_validates_its_own_invariants(self):
        with pytest.raises(ValueError):
            _c(set_version=0)
        with pytest.raises(ValueError):
            _c(seconds=-1)
        with pytest.raises(ValueError):
            _c(interval_count=-1)
        with pytest.raises(ValueError):
            _c(status="   ")


# ═════════════════════════════════════════════════════════════════════════════
# STEP 4 — content-addressed get-or-create
# ═════════════════════════════════════════════════════════════════════════════
class TestSnapshotWriter:
    def _create(self, db, **kw):
        return get_or_create_leave_snapshot(
            db, organization_id=kw.pop("organization_id", ORG),
            user_id=kw.pop("user_id", USER),
            work_date=kw.pop("work_date", DAY),
            overlap_seconds=kw.pop("overlap_seconds", 900),
            contributions=kw.pop("contributions", (_c(),)),
        )

    def test_the_key_is_the_content_hash(self, db):
        snapshot = self._create(db)
        assert snapshot.snapshot_key == snapshot.content_hash
        assert snapshot.snapshot_key == _hash(contributions=[_c()])

    def test_identical_content_reuses_the_row(self, db):
        first = self._create(db)
        second = self._create(db)
        assert first.id == second.id
        assert db.execute(
            sa.select(sa.func.count()).select_from(LeaveInputSnapshot)).scalar() == 1

    def test_different_content_creates_a_new_row(self, db):
        first = self._create(db)
        second = self._create(db, contributions=(_c(status=REJECTED, seconds=0),),
                              overlap_seconds=0)
        assert first.id != second.id
        assert first.snapshot_key != second.snapshot_key

    def test_the_empty_snapshot_is_created_once_and_reused(self, db):
        a = self._create(db, overlap_seconds=0, contributions=())
        b = self._create(db, overlap_seconds=0, contributions=())
        assert a.id == b.id
        assert a.contribution_count == 0
        assert a.overlap_seconds == 0
        assert read_contributions(db, snapshot=a) == ()

    def test_contributions_are_persisted_one_per_request(self, db):
        snapshot = self._create(db, overlap_seconds=960, contributions=(
            _c(request_id=7001, seconds=900),
            _c(request_id=7002, status=REJECTED, seconds=0),
        ))
        rows = read_contributions(db, snapshot=snapshot)
        assert [r.leave_request_id for r in rows] == [7001, 7002]
        assert [r.status_at_evaluation for r in rows] == [APPROVED, REJECTED]
        assert [r.contributed_seconds for r in rows] == [900, 0]
        assert snapshot.contribution_count == 2

    def test_a_non_contributing_request_is_recorded(self, db):
        """Ruling C: "leave existed but did not qualify" must stay
        distinguishable from "no leave existed"."""
        rejected_only = self._create(
            db, overlap_seconds=0,
            contributions=(_c(status=REJECTED, seconds=0),))
        empty = self._create(db, overlap_seconds=0, contributions=())

        assert rejected_only.snapshot_key != empty.snapshot_key
        assert rejected_only.overlap_seconds == 0 == empty.overlap_seconds
        assert rejected_only.contribution_count == 1
        assert empty.contribution_count == 0

    def test_it_is_readable_by_key(self, db):
        snapshot = self._create(db)
        found = read_leave_snapshot(
            db, organization_id=ORG, snapshot_key=snapshot.snapshot_key)
        assert found is not None and found.id == snapshot.id
        assert read_leave_snapshot(
            db, organization_id=OTHER_ORG,
            snapshot_key=snapshot.snapshot_key) is None

    def test_organization_isolation(self, db):
        mine = self._create(db, organization_id=ORG)
        theirs = self._create(db, organization_id=OTHER_ORG)
        assert mine.id != theirs.id
        assert mine.snapshot_key != theirs.snapshot_key


# ═════════════════════════════════════════════════════════════════════════════
# STEP 3 — immutability (ruling O)
# ═════════════════════════════════════════════════════════════════════════════
class TestSnapshotImmutability:
    def _snapshot(self, db):
        return get_or_create_leave_snapshot(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            overlap_seconds=900, contributions=(_c(),))

    @pytest.mark.parametrize("field,value", [
        ("overlap_seconds", 0),
        ("content_hash", "f" * 64),
        ("snapshot_key", "f" * 64),
        ("contribution_count", 9),
        ("work_date", date(2026, 1, 1)),
    ])
    def test_a_snapshot_cannot_be_modified(self, db, field, value):
        snapshot = self._snapshot(db)
        setattr(snapshot, field, value)
        with pytest.raises(LeaveSnapshotImmutable):
            db.flush()
        db.rollback()

    def test_a_snapshot_cannot_be_deleted(self, db):
        snapshot = self._snapshot(db)
        db.delete(snapshot)
        with pytest.raises(LeaveSnapshotImmutable):
            db.flush()
        db.rollback()

    def test_a_contribution_cannot_be_modified(self, db):
        snapshot = self._snapshot(db)
        row = read_contributions(db, snapshot=snapshot)[0]
        row.contributed_seconds = 0
        with pytest.raises(LeaveSnapshotImmutable):
            db.flush()
        db.rollback()

    def test_a_contribution_cannot_be_deleted(self, db):
        snapshot = self._snapshot(db)
        row = read_contributions(db, snapshot=snapshot)[0]
        db.delete(row)
        with pytest.raises(LeaveSnapshotImmutable):
            db.flush()
        db.rollback()

    def test_negative_values_are_refused_by_the_schema(self, db):
        db.add(LeaveInputSnapshot(
            snapshot_key="k", organization_id=ORG, user_id=USER, work_date=DAY,
            content_hash="k", overlap_seconds=-1, contribution_count=0,
            captured_at_utc=NOW,
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_no_foreign_key_to_leave_requests(self):
        """Ruling P: the request id is historical provenance, so ECO evidence
        survives a deletion the existing leave rules permit."""
        targets = {
            fk.column.table.name
            for fk in LeaveInputContribution.__table__.foreign_keys
        }
        assert "leave_requests" not in targets
        assert targets == {"leave_input_snapshot"}


# ═════════════════════════════════════════════════════════════════════════════
# The recorded day: identity, authority, reproducibility
# ═════════════════════════════════════════════════════════════════════════════
def _config(db, *, effective_from, minutes=REQUIRED_MINUTES):
    values = [{
        "scope_user_id": None, "tier": "TENANT_FALLBACK", "rank": 6,
        "key": "required_minutes", "value_minutes": minutes, "source_ref": None,
    }]
    key = effective_from.isoformat()
    version = ConfigVersion(
        organization_id=ORG, version_key=key, effective_from=effective_from,
        captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
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


def _worked_day(db, *, on=DAY, gap_minutes=30, version=1):
    led = WorkDayLedger(
        organization_id=ORG, user_id=USER,
        work_date=datetime(on.year, on.month, on.day),
        workweek_key=MONDAY_WEEK, ledger_version=version,
        state=LedgerState.SEALED.value, timezone_iana="Asia/Kolkata", is_working_day=True, working_day_source="org",
        utc_offset_seconds=0, created_at=NOW, created_by="TEST",
    )
    db.add(led)
    db.flush()
    seconds = (REQUIRED_MINUTES - gap_minutes) * 60
    begin = datetime(on.year, on.month, on.day, 9, 0, tzinfo=UTC)
    db.add(WorkStateInterval(
        ledger_id=led.id, organization_id=ORG, seq=1, state="WRK",
        start_at_utc=begin, end_at_utc=begin + timedelta(seconds=seconds),
        duration_seconds=seconds, provenance="WORKER", created_at=NOW,
    ))
    db.flush()
    return led


_seq = [9000]


def _leave(db, *, on=DAY, minutes=15, status=APPROVED, set_version=1):
    from database.models import LeaveRequest

    _seq[0] += 1
    request = LeaveRequest(
        id=_seq[0], user_id=USER, organization_id=ORG,
        leave_type="Casual Leave", from_date=datetime(on.year, on.month, on.day),
        to_date=datetime(on.year, on.month, on.day), description="x",
        status=status,
    )
    db.add(request)
    db.flush()
    interval_set = LeaveIntervalSet(
        organization_id=ORG, user_id=USER, leave_request_id=request.id,
        set_version=set_version, supersedes=None,
        timezone_iana="Asia/Kolkata", recorded_by="TEST", recorded_at_utc=NOW,
    )
    db.add(interval_set)
    db.flush()
    if minutes:
        begin = datetime(on.year, on.month, on.day, 18, 0, tzinfo=UTC)
        db.add(LeaveInterval(
            set_id=interval_set.id, organization_id=ORG, seq=1, work_date=on,
            start_at_utc=begin, end_at_utc=begin + timedelta(minutes=minutes),
            duration_seconds=minutes * 60,
        ))
        db.flush()
    return request, interval_set


def _record(db, on=DAY):
    return evaluate_and_record_day(
        db, organization_id=ORG, user_id=USER, work_date=on)


def _rows(db, on=DAY):
    return db.execute(
        sa.select(EcoDayRecord)
        .where(EcoDayRecord.organization_id == ORG,
               EcoDayRecord.work_date == on)
        .order_by(EcoDayRecord.evaluation_generation)
    ).scalars().all()


class TestDayRecordIdentityAndAuthority:
    def test_a_first_evaluation_references_an_explicit_snapshot(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        result = _record(db)

        assert result.is_recorded
        key = result.record.leave_input_snapshot_key
        assert key                                        # never NULL
        snapshot = read_leave_snapshot(db, organization_id=ORG, snapshot_key=key)
        assert snapshot is not None
        assert snapshot.overlap_seconds == 0
        assert snapshot.contribution_count == 0           # explicit empty
        assert result.record.evaluation_generation == 1
        assert result.record.superseded_by is None
        assert result.record.supersedes is None

    def test_exact_replay_writes_nothing(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        _leave(db, minutes=15)

        first = _record(db)
        snapshots_before = db.execute(
            sa.select(sa.func.count()).select_from(LeaveInputSnapshot)).scalar()
        second = _record(db)

        assert second.record.id == first.record.id
        assert second.record.evaluation_generation == 1
        assert len(_rows(db)) == 1
        assert db.execute(
            sa.select(sa.func.count()).select_from(LeaveInputSnapshot)
        ).scalar() == snapshots_before

    def test_changed_leave_creates_a_new_authoritative_record(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        first = _record(db)

        _leave(db, minutes=15)
        second = _record(db)

        assert second.record.id != first.record.id
        assert second.record.evaluation_generation == 2
        assert second.record.supersedes == first.record.id
        rows = _rows(db)
        assert rows[0].superseded_by == rows[1].id
        assert rows[1].superseded_by is None

    def test_changed_config_creates_a_new_authoritative_record(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        first = _record(db)

        _config(db, effective_from=DAY, minutes=600)
        second = _record(db)

        assert second.record.id != first.record.id
        assert second.record.config_version != first.record.config_version
        assert second.record.evaluation_generation == 2
        assert _rows(db)[0].superseded_by == second.record.id

    def test_changed_ledger_creates_a_new_authoritative_record(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        old = _worked_day(db)
        first = _record(db)

        new = _worked_day(db, gap_minutes=0, version=2)
        old.superseded_by = new.id
        db.flush()
        second = _record(db)

        assert second.record.ledger_version == 2
        assert second.record.evaluation_generation == 2
        assert _rows(db)[0].superseded_by == second.record.id

    def test_the_content_hash_covers_the_leave_key(self, db):
        """Ruling G: two records differing only by leave input must not share
        a digest."""
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        first = _record(db)
        _leave(db, minutes=15)
        second = _record(db)

        assert second.record.content_hash != first.record.content_hash

    def test_a_record_cannot_be_persisted_without_a_snapshot_key(self, db):
        from app.eval_day import LeaveInputSnapshotRequired, evaluate_day
        from app.config_versioning import DatabaseConfigVersionProvider
        from app.eval_day import record_evaluation

        _config(db, effective_from=DAY - timedelta(days=30))
        led = _worked_day(db)
        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=DatabaseConfigVersionProvider(db),
        )
        for bad in ("", "   ", None):
            with pytest.raises(LeaveInputSnapshotRequired):
                record_evaluation(db, evaluation,
                                  leave_input_snapshot_key=bad)
        assert _rows(db) == []


class TestReproducibilityWithoutCurrentLeave:
    def test_a_status_change_after_evaluation_cannot_rewrite_history(self, db):
        """Ruling D, the case that fails under every design omitting status.

        approved -> ECO evaluation -> rejected. The old record's own snapshot
        still says the leave was approved and how much it contributed.
        """
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        request, _ = _leave(db, minutes=15, status=APPROVED)
        recorded = _record(db)
        assert recorded.record.exclusion_minutes == 15
        key = recorded.record.leave_input_snapshot_key

        request.status = REJECTED
        db.flush()

        snapshot = read_leave_snapshot(db, organization_id=ORG, snapshot_key=key)
        contributions = read_contributions(db, snapshot=snapshot)
        assert snapshot.overlap_seconds == 15 * 60
        assert [c.status_at_evaluation for c in contributions] == [APPROVED]
        assert [c.contributed_seconds for c in contributions] == [15 * 60]
        # And the persisted record still reads 15.
        assert _rows(db)[0].exclusion_minutes == 15

    def test_re_evaluating_after_rejection_supersedes_rather_than_rewrites(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        request, _ = _leave(db, minutes=15, status=APPROVED)
        first = _record(db)

        request.status = REJECTED
        db.flush()
        second = _record(db)

        assert second.record.id != first.record.id
        assert second.record.exclusion_minutes == 0
        assert second.record.attributable_minutes == 30
        rows = _rows(db)
        assert [r.exclusion_minutes for r in rows] == [15, 0]
        assert rows[0].superseded_by == rows[1].id

    def test_locked_for_payroll_is_recorded_distinctly(self, db):
        """Ruling D: equivalent in effect, distinct as a historical fact."""
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        _leave(db, minutes=15, status=LOCKED)
        result = _record(db)

        assert result.record.exclusion_minutes == 15
        snapshot = read_leave_snapshot(
            db, organization_id=ORG,
            snapshot_key=result.record.leave_input_snapshot_key)
        assert [c.status_at_evaluation
                for c in read_contributions(db, snapshot=snapshot)] == [LOCKED]

    def test_a_withdrawn_set_is_represented_by_the_selected_version(self, db):
        """Ruling C (A2-narrow): zero seconds under the selected set version.

        Not absence. The withdrawn request is recorded with the CURRENT empty
        version, so "leave existed and was withdrawn" stays distinguishable
        from "no leave existed" at day level.
        """
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        request, interval_set = _leave(db, minutes=15)
        _record(db)

        # Withdrawal: a NEW set version holding no intervals.
        db.add(LeaveIntervalSet(
            organization_id=ORG, user_id=USER,
            leave_request_id=request.id, set_version=2,
            supersedes=interval_set.id, timezone_iana="Asia/Kolkata",
            recorded_by="TEST", recorded_at_utc=NOW,
        ))
        db.flush()
        after = _record(db)

        snapshot = read_leave_snapshot(
            db, organization_id=ORG,
            snapshot_key=after.record.leave_input_snapshot_key)
        # The arithmetic is unchanged: nothing is excluded.
        assert after.record.exclusion_minutes == 0
        assert snapshot.overlap_seconds == 0
        # But the request that was considered is recorded.
        assert snapshot.contribution_count == 1
        rows = read_contributions(db, snapshot=snapshot)
        assert len(rows) == 1
        assert rows[0].leave_request_id == request.id
        assert rows[0].set_version == 2          # the SELECTED empty version
        assert rows[0].status_at_evaluation == APPROVED   # raw, unchanged
        assert rows[0].interval_count == 0
        assert rows[0].contributed_seconds == 0

    def test_the_withdrawn_day_differs_from_a_day_with_no_leave(self, db):
        """The whole point of ruling C, asserted on the keys themselves."""
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        no_leave = _record(db)
        empty_key = no_leave.record.leave_input_snapshot_key

        request, interval_set = _leave(db, minutes=15)
        _record(db)
        db.add(LeaveIntervalSet(
            organization_id=ORG, user_id=USER,
            leave_request_id=request.id, set_version=2,
            supersedes=interval_set.id, timezone_iana="Asia/Kolkata",
            recorded_by="TEST", recorded_at_utc=NOW,
        ))
        db.flush()
        withdrawn = _record(db)

        assert withdrawn.record.leave_input_snapshot_key != empty_key

    def test_multiple_requests_are_each_recorded(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db, gap_minutes=60)
        _leave(db, minutes=10, status=APPROVED)
        _leave(db, minutes=10, status=REJECTED)
        _leave(db, minutes=10, status=LOCKED)
        result = _record(db)

        snapshot = read_leave_snapshot(
            db, organization_id=ORG,
            snapshot_key=result.record.leave_input_snapshot_key)
        rows = read_contributions(db, snapshot=snapshot)
        assert len(rows) == 3
        assert sorted(c.status_at_evaluation for c in rows) == sorted(
            [APPROVED, REJECTED, LOCKED])
        # Only the two granted ones contributed.
        assert sum(c.contributed_seconds for c in rows) == 20 * 60
        assert result.record.exclusion_minutes == 20
        assert snapshot.overlap_seconds == 20 * 60

    def test_the_set_version_is_recorded(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        _leave(db, minutes=15, set_version=3)
        result = _record(db)

        snapshot = read_leave_snapshot(
            db, organization_id=ORG,
            snapshot_key=result.record.leave_input_snapshot_key)
        assert [c.set_version
                for c in read_contributions(db, snapshot=snapshot)] == [3]


class TestSupersessionMutationException:
    """Ruling I — narrow, named, and test-enforced."""

    def _two_records(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        _record(db)
        _leave(db, minutes=15)
        _record(db)
        return _rows(db)

    def test_the_only_permitted_update_is_null_to_a_value(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        first = _record(db).record
        assert first.superseded_by is None

        first.superseded_by = 999_999            # NULL -> value: authorized
        db.flush()
        assert _rows(db)[0].superseded_by == 999_999
        db.rollback()

    def test_a_superseded_record_cannot_become_current_again(self, db):
        rows = self._two_records(db)
        rows[0].superseded_by = None
        with pytest.raises(DayRecordImmutable):
            db.flush()
        db.rollback()

    def test_a_superseded_record_cannot_be_re_pointed(self, db):
        rows = self._two_records(db)
        rows[0].superseded_by = 999_999
        with pytest.raises(DayRecordImmutable):
            db.flush()
        db.rollback()

    @pytest.mark.parametrize("field,value", [
        ("exclusion_minutes", 99),
        ("attributable_minutes", 99),
        ("content_hash", "f" * 64),
        ("leave_input_snapshot_key", "f" * 64),
        ("evaluation_generation", 9),
        ("supersedes", 1),
        ("config_version", "other"),
        ("ledger_version", 9),
        ("work_date", date(2026, 1, 1)),
        ("state", "NO_SHORTFALL"),
    ])
    def test_every_other_field_stays_immutable(self, db, field, value):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        record = _record(db).record

        setattr(record, field, value)
        with pytest.raises(DayRecordImmutable):
            db.flush()
        db.rollback()

    def test_a_record_still_cannot_be_deleted(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        record = _record(db).record
        db.delete(record)
        with pytest.raises(DayRecordImmutable):
            db.flush()
        db.rollback()

    def test_the_exception_is_named_in_the_model(self):
        """Not achieved by removing the listener."""
        from app.eval_day import models as m

        assert m.SUPERSESSION_FIELD == "superseded_by"
        body = (EVAL_DAY / "models.py").read_text(encoding="utf-8")
        assert "before_update" in body        # the listener still exists


class TestConcurrency:
    def test_the_same_input_race_recovers_to_one_row(self, db, monkeypatch):
        """CASE 1: identical seven-part identity -> identity conflict ->
        existing-row recovery. Blinding the pre-check is exactly a losing
        writer's state."""
        from app.eval_day import evaluator as ev

        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        winner = _record(db)

        calls = {"n": 0}
        real = ev._existing

        def _blind_first(db_, ev_, key_):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(db_, ev_, key_)

        monkeypatch.setattr(ev, "_existing", _blind_first)
        again = _record(db)

        assert calls["n"] >= 2
        assert again.record.id == winner.record.id
        assert len(_rows(db)) == 1

    def test_the_different_input_race_retries_and_supersedes(self, db, monkeypatch):
        """CASE 2: different identity, contended generation -> the loser
        re-reads and claims the next one, superseding the new current row.

        Simulated by making the FIRST generation derivation stale, which is
        exactly what a competitor's concurrent insert produces.
        """
        from app.eval_day import evaluator as ev

        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        first = _record(db)
        _leave(db, minutes=15)

        calls = {"n": 0}
        real = ev._generation_after

        def _stale_once(predecessor):
            calls["n"] += 1
            # First attempt claims a generation already taken.
            return 1 if calls["n"] == 1 else real(predecessor)

        monkeypatch.setattr(ev, "_generation_after", _stale_once)
        second = _record(db)

        assert calls["n"] >= 2, "the retry must have re-read the generation"
        assert second.is_recorded
        assert second.record.evaluation_generation == 2
        rows = _rows(db)
        assert len(rows) == 2
        assert sum(1 for r in rows if r.superseded_by is None) == 1
        assert rows[0].superseded_by == rows[1].id

    def test_exactly_one_authoritative_row_after_many_re_evaluations(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        _record(db)
        for minutes in (5, 10, 15):
            _leave(db, minutes=minutes)
            _record(db)

        rows = _rows(db)
        assert len(rows) == 4
        assert [r.evaluation_generation for r in rows] == [1, 2, 3, 4]
        assert sum(1 for r in rows if r.superseded_by is None) == 1
        assert rows[-1].superseded_by is None

    def test_a_non_duplicate_integrity_error_is_still_raised(self, db,
                                                             monkeypatch):
        from app.eval_day import evaluator as ev
        from app.config_versioning import DatabaseConfigVersionProvider
        from app.eval_day import evaluate_day, record_evaluation

        _config(db, effective_from=DAY - timedelta(days=30))
        led = _worked_day(db)
        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=DatabaseConfigVersionProvider(db),
        )
        object.__setattr__(evaluation, "requirement_outcome", None)
        monkeypatch.setattr(ev, "_existing", lambda *a, **k: None)

        with pytest.raises(sa.exc.IntegrityError):
            record_evaluation(db, evaluation, leave_input_snapshot_key="k" * 64)
        db.rollback()

    def test_the_generation_constraint_is_the_serializer(self, db):
        """Two rows cannot share a generation for one Worker-day, whatever
        their identities."""
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        record = _record(db).record

        db.add(EcoDayRecord(
            organization_id=ORG, user_id=USER, work_date=DAY,
            ledger_version=1, policy_version=None, config_version="other",
            leave_input_snapshot_key="z" * 64,
            evaluation_generation=record.evaluation_generation,
            requirement_outcome="RESOLVED", content_hash="z" * 64,
            state="SHORTFALL", shortfall_computed=True,
            required_minutes=1, qualifying_minutes=0,
            gross_shortfall_minutes=1, exclusion_minutes=0,
            attributable_minutes=1, contributes_to_accumulator_minutes=1,
            evaluated_at_utc=NOW,
        ))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()


class TestEvalWeekSelection:
    def test_the_week_uses_the_authoritative_record(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        dates = [DAY + timedelta(days=i) for i in range(7)]
        for on in dates[:5]:
            _worked_day(db, on=on, gap_minutes=10)
            _record(db, on=on)
        for on in dates[5:]:
            db.add(WorkDayLedger(
                organization_id=ORG, user_id=USER,
                work_date=datetime(on.year, on.month, on.day),
                workweek_key=MONDAY_WEEK, ledger_version=1,
                state=LedgerState.NOT_A_WORKING_DAY.value,
                # D4-A: the state asserts no work was expected.
                timezone_iana="Asia/Kolkata", is_working_day=False, working_day_source="org", utc_offset_seconds=0,
                created_at=NOW, created_by="TEST"))
            db.flush()
            _record(db, on=on)

        before = evaluate_workweek(
            db, organization_id=ORG, user_id=USER, workweek_key=MONDAY_WEEK)
        assert before.state is WeeklyState.COMPLETE
        assert before.weekly_attributable_shortfall_minutes == 50

        # Leave on one day, re-evaluated: the week must use the new record.
        _leave(db, on=dates[0], minutes=10)
        _record(db, on=dates[0])

        after = evaluate_workweek(
            db, organization_id=ORG, user_id=USER, workweek_key=MONDAY_WEEK)
        assert after.state is WeeklyState.COMPLETE
        assert after.weekly_attributable_shortfall_minutes == 40

    def test_eval_week_resolves_nothing(self):
        """Ruling L, test-enforced and unchanged."""
        for path in sorted(EVAL_WEEK.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("resolve_requirement_strict", "version_as_of",
                           "DatabaseConfigVersionProvider", "resolve_threshold",
                           "approved_leave_overlap", "intervals_for_day",
                           "LeaveRequest", "read_leave_snapshot",
                           "compute_leave_snapshot_hash",
                           "app.config_versioning", "app.leave_intervals"):
                assert banned not in tokens, f"{path.name} resolves {banned}"

    def test_eval_week_selects_by_predicate_not_by_latest(self):
        body = (EVAL_WEEK / "membership.py").read_text(encoding="utf-8")
        assert "superseded_by" in body
        tokens = _code_tokens(ast.parse(body))
        for banned in ("evaluation_generation", "evaluated_at_utc", "max"):
            assert banned not in tokens, f"membership.py uses {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# Migration and scope
# ═════════════════════════════════════════════════════════════════════════════
class TestMigration:
    PATH = (BACKEND / "alembic" / "versions"
            / "d3a1c8b57f92_leave_input_snapshot_and_day_authority.py")

    def test_the_revision_is_a_single_parent_child_of_the_head(self):
        body = self.PATH.read_text(encoding="utf-8")
        assert f"revision: str = '{EXPECTED_REVISION}'" in body
        assert (f"down_revision: Union[str, Sequence[str], None] = "
                f"'{EXPECTED_PARENT}'") in body

    def test_the_graph_has_one_head_and_one_base(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        assert len(list((BACKEND / "alembic" / "versions").glob("*.py"))) == (
            EXPECTED_REVISION_COUNT)
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        # Head re-baselined by §45.D-15: c4e9b7d2a583 is an ordinary
        # single-parent child of b7c4e0a92d51. Still one base, one head.
        assert list(sd.get_heads()) == ["b5f2a8c47d93"]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_it_creates_no_row_and_opens_no_connection(self):
        body = self.PATH.read_text(encoding="utf-8")
        for banned in ("create_engine", "psycopg", "DATABASE_URL",
                       "postgresql://", "sessionmaker", "load_dotenv",
                       "INSERT INTO", "UPDATE ", "DELETE FROM", "bulk_insert"):
            assert banned not in body, banned

    def test_it_drops_no_existing_table(self):
        tree = ast.parse(self.PATH.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {
            getattr(n.func, "attr", None) for n in ast.walk(upgrade)
            if isinstance(n, ast.Call)
        }
        assert "drop_table" not in called
        assert "alter_column" not in called
        assert "drop_column" not in called

    def test_applying_it_twice_is_a_no_op(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        import importlib.util

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)
        EcoDayRecord.__table__.create(eng, checkfirst=True)

        spec = importlib.util.spec_from_file_location("_ruling2_mig", self.PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for _ in range(2):
            with eng.begin() as conn:
                with Operations.context(MigrationContext.configure(conn)):
                    module.upgrade()

        inspector = sa.inspect(eng)
        assert "leave_input_snapshot" in inspector.get_table_names()
        assert "leave_input_contribution" in inspector.get_table_names()

    def test_it_is_a_no_op_when_the_day_record_table_is_absent(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        import importlib.util

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)

        spec = importlib.util.spec_from_file_location("_ruling2_mig2", self.PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()

        assert "eco_day_record" not in set(sa.inspect(eng).get_table_names())

    def test_the_new_tables_match_their_models(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from database.models import Organization, User

        import importlib.util

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        Organization.__table__.create(eng, checkfirst=True)
        User.__table__.create(eng, checkfirst=True)
        EcoDayRecord.__table__.create(eng, checkfirst=True)

        spec = importlib.util.spec_from_file_location("_ruling2_mig3", self.PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()

        inspector = sa.inspect(eng)
        for model in (LeaveInputSnapshot, LeaveInputContribution):
            cols = {c["name"] for c in inspector.get_columns(model.__tablename__)}
            assert cols == {c.name for c in model.__table__.columns}, \
                model.__tablename__


class TestScopeHeld:
    def test_the_golden_vectors_are_untouched(self):
        import json

        fixture = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
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

    def test_no_classifier_or_legal_determination(self):
        """Nothing here DECIDES a legal break category.

        The claim narrowed when owner rulings P-1/P-2 landed. EVAL-DAY now
        CONSUMES a classification: ``classification.py`` compares an assertion
        against ``ClassificationResult.BRK_R``/``BRK_M`` to decide how the
        governing profile TREATS it. Reading a determination made elsewhere is
        not making one, and the tokens that would constitute making one —
        jurisdiction, legal matrix, statutory, compensable — remain banned.

        The compensating half is below: the vocabulary must be IMPORTED from
        the package that owns it, never restated as a literal here.
        """
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("jurisdiction", "legal_matrix", "LegalMatrix",
                           "statutory", "compensable", "worker_context"):
                assert banned not in tokens, f"{path.name} decides {banned}"

    def test_the_break_vocabulary_is_borrowed_never_restated(self):
        """EVAL-DAY may compare against the vocabulary, not define it.

        A bare ``"BRK_R"`` literal here would be a second spelling of a legal
        category — exactly the drift INV-4 exists to prevent — so the names may
        appear only as attributes of the owning enum.
        """
        import ast as _ast

        for path in sorted(EVAL_DAY.glob("*.py")):
            tree = _ast.parse(path.read_text(encoding="utf-8"))
            literals = {n.value for n in _ast.walk(tree)
                        if isinstance(n, _ast.Constant)
                        and isinstance(n.value, str)}
            for banned in ("BRK_M", "BRK_R"):
                assert banned not in literals, (
                    f"{path.name} restates {banned} as a literal; it must come "
                    f"from app.break_classification.ClassificationResult")

    def test_qualifying_states_is_still_wrk_only(self):
        from app.eval_day import QUALIFYING_STATES

        assert set(QUALIFYING_STATES) == {"WRK"}

    def test_no_event_notification_or_scheduler(self):
        for path in sorted(EVAL_DAY.glob("*.py")):
            tokens = _code_tokens(ast.parse(path.read_text(encoding="utf-8")))
            for banned in ("resolve_threshold", "ECO_THRESHOLD", "crossing",
                           "notification", "notify", "sequence", "add_job",
                           "apscheduler", "APIRouter", "BaseModel"):
                assert banned not in tokens, f"{path.name} uses {banned}"

    def test_eco_v1_remains_disabled(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_the_terminology_guard_is_satisfied(self):
        from app.early_clock_out import find_prohibited_terms, format_hits, scan_text

        path = EVAL_DAY / "leave_snapshot.py"
        hits = scan_text(path.read_text(encoding="utf-8"))
        assert hits == [], format_hits(hits, source=path.name)
        assert find_prohibited_terms("LEAVE_AUTHORITY_UNRESOLVED") == []

    def test_no_production_database_is_reachable(self):
        for path in sorted(EVAL_DAY.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for banned in ("create_engine", "DATABASE_URL", "postgresql://",
                           "sessionmaker", "load_dotenv"):
                assert banned not in body, f"{path.name} mentions {banned}"


# ═════════════════════════════════════════════════════════════════════════════
# BOUNDED CORRECTION INCREMENT — the audit's D1, D2 and triage defects
# ═════════════════════════════════════════════════════════════════════════════
def _plain_record(db, *, generation, key, supersedes=None, work_date=DAY,
                  config_version="c", content_hash=None):
    """An EcoDayRecord built directly, for authority tests that need control.

    Deliberately not through ``record_evaluation``: these tests are ABOUT that
    function's authority handling, so the fixtures must not depend on it.
    """
    row = EcoDayRecord(
        organization_id=ORG, user_id=USER, work_date=work_date,
        ledger_version=1, policy_version=None, config_version=config_version,
        leave_input_snapshot_key=key, evaluation_generation=generation,
        supersedes=supersedes, requirement_outcome="RESOLVED",
        content_hash=(content_hash or key), state="SHORTFALL",
        shortfall_computed=True, required_minutes=1, qualifying_minutes=0,
        gross_shortfall_minutes=1, exclusion_minutes=0, attributable_minutes=1,
        contributes_to_accumulator_minutes=1, evaluated_at_utc=NOW,
    )
    db.add(row)
    db.flush()
    return row


class TestRulingGIsNotVacuous:
    """The leave key must be the ONLY thing that moved."""

    _FIXED = dict(
        organization_id=ORG, user_id=USER, work_date=DAY, ledger_version=1,
        policy_version=None, config_version="cfg-1",
        result={"state": "SHORTFALL", "attributable_minutes": 30},
    )

    def test_only_the_leave_key_changing_changes_the_digest(self):
        """Ruling G, tested without changing anything else.

        The previous guard re-evaluated a day WITH leave added, so the result
        fields moved too and the digests would have differed even if the leave
        key were absent from the payload entirely. Here every other input is
        held identical by construction.
        """
        from app.eval_day.models import compute_result_hash

        first = compute_result_hash(leave_input_snapshot_key="a" * 64,
                                    **self._FIXED)
        second = compute_result_hash(leave_input_snapshot_key="b" * 64,
                                     **self._FIXED)
        assert first != second

    def test_the_same_leave_key_reproduces_the_digest(self):
        from app.eval_day.models import compute_result_hash

        assert (compute_result_hash(leave_input_snapshot_key="a" * 64,
                                    **self._FIXED)
                == compute_result_hash(leave_input_snapshot_key="a" * 64,
                                       **self._FIXED))


# ── Ruling N, transcribed independently ─────────────────────────────────────
#: The canonical form written out from the ruling, NOT obtained by calling the
#: production function. A digest that agrees with itself proves nothing.
def _independent_fingerprint(rows) -> str:
    encoded = json.dumps(sorted(rows), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _independent_leave_hash(*, organization_id, user_id, work_date,
                            overlap_seconds, contributions) -> str:
    payload = {
        "organization_id": int(organization_id),
        "user_id": int(user_id),
        "work_date": work_date.isoformat(),
        "overlap_seconds": int(overlap_seconds),
        "contributions": [
            {
                "leave_request_id": int(c["leave_request_id"]),
                "set_version": int(c["set_version"]),
                "status": c["status"],
                "interval_fingerprint": c["interval_fingerprint"],
                "interval_count": int(c["interval_count"]),
                "contributed_seconds": int(c["contributed_seconds"]),
            }
            for c in sorted(contributions,
                            key=lambda c: int(c["leave_request_id"]))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: Pinned digests for one fixed input, so that editing BOTH the implementation
#: and the transcription above still fails.
PINNED_FINGERPRINT = (
    "33530636b8de2220e778187ce9e8b042e9348e90b44d0cb9f1e3ca4bbdf3d5b6")
PINNED_SNAPSHOT_HASH = (
    "6a806b6dd40bf9c3ed89888f3acb901f455f27a465971fa9eba4f8072b994830")
PINNED_EMPTY_HASH = (
    "80fa941b2cd0e6ea3b6f92d37bbadf6ac8429aede3a8a856f2fcb87a84fd5619")


class _Interval:
    def __init__(self, start, end, seconds):
        self.start_at_utc, self.end_at_utc, self.duration_seconds = (
            start, end, seconds)


class TestCanonicalFormIndependently:
    """Ruling N, verified against a form written out here rather than imported."""

    START = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
    END = datetime(2026, 8, 31, 18, 15, tzinfo=UTC)

    def test_the_interval_fingerprint_matches_the_transcribed_form(self):
        expected = _independent_fingerprint([
            ("2026-08-31T18:00:00+00:00", "2026-08-31T18:15:00+00:00", 900),
        ])
        assert expected == PINNED_FINGERPRINT
        assert leave_interval_fingerprint(
            [_Interval(self.START, self.END, 900)]) == expected

    def test_the_snapshot_hash_matches_the_transcribed_form(self):
        expected = _independent_leave_hash(
            organization_id=ORG, user_id=USER, work_date=DAY,
            overlap_seconds=900,
            contributions=[{
                "leave_request_id": 7001, "set_version": 1,
                "status": APPROVED, "interval_fingerprint": PINNED_FINGERPRINT,
                "interval_count": 1, "contributed_seconds": 900,
            }],
        )
        assert expected == PINNED_SNAPSHOT_HASH
        assert compute_leave_snapshot_hash(
            organization_id=ORG, user_id=USER, work_date=DAY,
            overlap_seconds=900,
            contributions=(_c(request_id=7001, set_version=1, status=APPROVED,
                              fingerprint=PINNED_FINGERPRINT,
                              interval_count=1, seconds=900),),
        ) == expected

    def test_the_empty_snapshot_hash_matches_the_transcribed_form(self):
        expected = _independent_leave_hash(
            organization_id=ORG, user_id=USER, work_date=DAY,
            overlap_seconds=0, contributions=[])
        assert expected == PINNED_EMPTY_HASH
        assert _hash(overlap_seconds=0, contributions=()) == expected


class TestForeignKeysWhereEnforced:
    """Ruling P's tenancy claim, on an engine that actually enforces it.

    The shared test engine leaves ``PRAGMA foreign_keys`` off, so every
    assertion about ``RESTRICT`` or the composite tenant key would otherwise
    pass vacuously. Same dedicated-engine idiom as Steps 6 and 7.
    """

    @pytest.fixture()
    def fk_session(self):
        from database.models import Organization, User

        eng = sa.create_engine("sqlite:///:memory:", future=True)

        @sa_event.listens_for(eng, "connect")
        def _enable_fk(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        for table in (Organization.__table__, User.__table__,
                      LeaveInputSnapshot.__table__,
                      LeaveInputContribution.__table__):
            table.create(eng, checkfirst=True)

        with SaSession(eng) as session:
            session.add(Organization(id=ORG, name="Org A"))
            session.add(Organization(id=OTHER_ORG, name="Org B"))
            session.add(User(id=USER, name="W", email="w@example.test",
                             organization_id=ORG))
            session.commit()
            yield session

    def test_foreign_key_enforcement_is_actually_on(self, fk_session):
        """Guard the guard."""
        assert fk_session.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1

    def _snapshot(self, session, key="k" * 64):
        row = LeaveInputSnapshot(
            snapshot_key=key, organization_id=ORG, user_id=USER,
            work_date=DAY, content_hash=key, overlap_seconds=0,
            contribution_count=0, captured_at_utc=NOW)
        session.add(row)
        session.flush()
        return row

    def _contribution(self, snapshot_id, organization_id, request_id=7001):
        return LeaveInputContribution(
            snapshot_id=snapshot_id, organization_id=organization_id,
            leave_request_id=request_id, set_version=1,
            status_at_evaluation=APPROVED, interval_fingerprint="f" * 8,
            interval_count=1, contributed_seconds=0)

    def test_a_contribution_cannot_be_attached_across_tenants(self, fk_session):
        """Ruling P: no cross-tenant contribution attachment."""
        snapshot = self._snapshot(fk_session)
        fk_session.add(self._contribution(snapshot.id, OTHER_ORG))
        with pytest.raises(sa.exc.IntegrityError):
            fk_session.flush()
        fk_session.rollback()

    def test_a_contribution_cannot_reference_a_nonexistent_snapshot(
        self, fk_session
    ):
        fk_session.add(self._contribution(999999, ORG))
        with pytest.raises(sa.exc.IntegrityError):
            fk_session.flush()
        fk_session.rollback()

    def test_the_same_tenant_attachment_is_accepted(self, fk_session):
        """Guard the guard: the refusals above are not refusing everything."""
        snapshot = self._snapshot(fk_session, key="a" * 64)
        fk_session.add(self._contribution(snapshot.id, ORG))
        fk_session.flush()
        fk_session.commit()

    def test_a_snapshot_carrying_contributions_cannot_be_deleted(
        self, fk_session
    ):
        """RESTRICT, enforced by the database and not only by the listener."""
        snapshot = self._snapshot(fk_session, key="b" * 64)
        fk_session.add(self._contribution(snapshot.id, ORG))
        fk_session.commit()

        with pytest.raises(sa.exc.IntegrityError):
            fk_session.execute(
                sa.text("DELETE FROM leave_input_snapshot WHERE id = :i"),
                {"i": snapshot.id})
            fk_session.flush()
        fk_session.rollback()

    def test_an_organization_holding_snapshots_cannot_be_deleted(
        self, fk_session
    ):
        self._snapshot(fk_session, key="d" * 64)
        fk_session.commit()
        with pytest.raises(sa.exc.IntegrityError):
            fk_session.execute(
                sa.text("DELETE FROM organizations WHERE id = :i"), {"i": ORG})
            fk_session.flush()
        fk_session.rollback()


class TestIntervalCorrectionEndToEnd:
    """A corrected interval must supersede, not rewrite."""

    def test_a_corrected_interval_set_produces_a_new_authoritative_record(
        self, db
    ):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db, gap_minutes=60)
        request, first_set = _leave(db, minutes=15)
        first = _record(db)
        first_key = first.record.leave_input_snapshot_key
        assert first.record.exclusion_minutes == 15

        # The correction: a NEW set version with a different interval.
        corrected = LeaveIntervalSet(
            organization_id=ORG, user_id=USER, leave_request_id=request.id,
            set_version=2, supersedes=first_set.id,
            timezone_iana="Asia/Kolkata", recorded_by="TEST",
            recorded_at_utc=NOW)
        db.add(corrected)
        db.flush()
        begin = datetime(DAY.year, DAY.month, DAY.day, 19, 0, tzinfo=UTC)
        db.add(LeaveInterval(
            set_id=corrected.id, organization_id=ORG, seq=1, work_date=DAY,
            start_at_utc=begin, end_at_utc=begin + timedelta(minutes=25),
            duration_seconds=25 * 60))
        db.flush()

        second = _record(db)

        # A new snapshot, a new identity, a new authoritative record.
        assert second.record.leave_input_snapshot_key != first_key
        assert second.record.exclusion_minutes == 25
        assert second.record.evaluation_generation == 2
        assert second.record.supersedes == first.record.id

        rows = _rows(db)
        assert len(rows) == 2
        assert rows[0].superseded_by == rows[1].id
        assert rows[1].superseded_by is None

        # The superseded record's own snapshot still describes the ORIGINAL
        # interval, including its set version.
        old = read_leave_snapshot(db, organization_id=ORG,
                                  snapshot_key=first_key)
        old_rows = read_contributions(db, snapshot=old)
        assert [c.set_version for c in old_rows] == [1]
        assert [c.contributed_seconds for c in old_rows] == [15 * 60]
        assert old.overlap_seconds == 15 * 60

        # And the new one describes the correction.
        new = read_leave_snapshot(
            db, organization_id=ORG,
            snapshot_key=second.record.leave_input_snapshot_key)
        new_rows = read_contributions(db, snapshot=new)
        assert [c.set_version for c in new_rows] == [2]
        assert [c.contributed_seconds for c in new_rows] == [25 * 60]
        assert (new_rows[0].interval_fingerprint
                != old_rows[0].interval_fingerprint)


class TestStalePredecessorAuthority:
    """D1. The audit's interleaving, not the stale-GENERATION one.

    ``W2`` reads predecessor ``P``; ``W1`` commits successor ``A`` and
    supersedes ``P``; ``W2`` proceeds holding the stale ``P``. Two mechanisms
    must each stop it leaving two authoritative rows, and both are tested
    separately so neither can hide a regression in the other.
    """

    def _two_generations(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        first = _record(db)
        _leave(db, minutes=15)
        second = _record(db)
        assert second.record.evaluation_generation == 2
        return first.record, second.record

    def test_the_generation_is_derived_from_the_predecessor_that_was_read(self):
        """The coupling itself: one read decides both."""
        from app.eval_day.evaluator import _generation_after

        assert _generation_after(None) == 1

        class _Row:
            evaluation_generation = 7

        assert _generation_after(_Row()) == 8

    def test_a_stale_predecessor_cannot_produce_two_authoritative_rows(
        self, db, monkeypatch
    ):
        """The generation derived from a stale predecessor is already taken."""
        from app.eval_day import evaluator as ev

        predecessor, successor = self._two_generations(db)
        _leave(db, minutes=20)          # a third, different input

        calls = {"n": 0}
        real = ev._authoritative

        def _stale_once(db_, ev_):
            calls["n"] += 1
            # The first read answers with the row that WAS current.
            return predecessor if calls["n"] == 1 else real(db_, ev_)

        monkeypatch.setattr(ev, "_authoritative", _stale_once)
        third = _record(db)

        assert calls["n"] >= 2, "the attempt must have re-read the predecessor"
        rows = _rows(db)
        assert len(rows) == 3
        assert sum(1 for r in rows if r.superseded_by is None) == 1
        # And it superseded the row that was ACTUALLY current, not the stale one.
        assert third.record.supersedes == successor.id
        assert rows[-1].id == third.record.id

    def test_the_guarded_pointer_refuses_a_stale_predecessor(
        self, db, monkeypatch
    ):
        """Defence in depth: a stale predecessor beside a FRESH generation.

        This is the case the generation coupling cannot see — the two reads
        disagreeing — and it is the reason the predecessor update carries
        ``WHERE superseded_by IS NULL`` and checks the affected row count.
        """
        from app.eval_day import evaluator as ev

        predecessor, successor = self._two_generations(db)
        _leave(db, minutes=20)

        auth_calls = {"n": 0}
        real_auth = ev._authoritative

        def _stale_once(db_, ev_):
            auth_calls["n"] += 1
            return predecessor if auth_calls["n"] == 1 else real_auth(db_, ev_)

        gen_calls = {"n": 0}
        real_gen = ev._generation_after

        def _fresh_generation(pred):
            gen_calls["n"] += 1
            # Pretend the generation read landed AFTER the competitor's commit
            # while the predecessor read landed before it.
            return 3 if gen_calls["n"] == 1 else real_gen(pred)

        monkeypatch.setattr(ev, "_authoritative", _stale_once)
        monkeypatch.setattr(ev, "_generation_after", _fresh_generation)

        third = _record(db)

        assert gen_calls["n"] >= 2, "the guarded pointer must force a retry"
        rows = _rows(db)
        assert sum(1 for r in rows if r.superseded_by is None) == 1
        assert third.record.supersedes == successor.id
        # The stale predecessor still points at the row that really superseded it.
        db.expire(predecessor)
        assert predecessor.superseded_by == successor.id

    def test_the_pointer_update_is_guarded_in_the_source(self):
        """The predicate is the proof, so it must be present."""
        body = (EVAL_DAY / "evaluator.py").read_text(encoding="utf-8")
        assert "superseded_by.is_(None)" in body
        assert "rowcount" in body
        tokens = _code_tokens(ast.parse(body))
        # And the generation is NOT derived from a separate maximum any more.
        assert "_next_generation" not in tokens


class TestExpiredSupersessionMutation:
    """D2. Expired or stale session state must not defeat ruling I."""

    def _superseded(self, db):
        predecessor = _plain_record(db, generation=1, key="p" * 64)
        successor = _plain_record(db, generation=2, key="s" * 64,
                                  supersedes=predecessor.id)
        predecessor.superseded_by = successor.id
        db.flush()
        return predecessor, successor

    def test_an_expired_attribute_cannot_be_re_pointed(self, db):
        """The exact sequence the audit reproduced."""
        predecessor, successor = self._superseded(db)
        other = _plain_record(db, generation=3, key="o" * 64)

        db.expire(predecessor, ["superseded_by"])
        with pytest.raises(DayRecordImmutable):
            predecessor.superseded_by = other.id
            db.flush()
        db.rollback()

    def test_a_fully_expired_object_cannot_be_re_pointed(self, db):
        predecessor, successor = self._superseded(db)
        other = _plain_record(db, generation=3, key="o" * 64)

        db.expire(predecessor)
        with pytest.raises(DayRecordImmutable):
            predecessor.superseded_by = other.id
            db.flush()
        db.rollback()

    def test_an_expired_attribute_cannot_be_returned_to_null(self, db):
        predecessor, _ = self._superseded(db)

        db.expire(predecessor, ["superseded_by"])
        with pytest.raises(DayRecordImmutable):
            predecessor.superseded_by = None
            db.flush()
        db.rollback()

    def test_the_refusal_names_the_stored_value_not_a_guess(self, db):
        """The old message said "from None to None" because it never looked."""
        predecessor, successor = self._superseded(db)
        other = _plain_record(db, generation=3, key="o" * 64)
        db.expire(predecessor, ["superseded_by"])

        with pytest.raises(DayRecordImmutable) as caught:
            predecessor.superseded_by = other.id
            db.flush()
        assert f"from {successor.id!r}" in str(caught.value)
        db.rollback()

    def test_the_permitted_transition_still_works_when_expired(self, db):
        """Guard the guard: the fix must not refuse the authorized write."""
        predecessor = _plain_record(db, generation=1, key="p" * 64)
        successor = _plain_record(db, generation=2, key="s" * 64,
                                  supersedes=predecessor.id)
        db.expire(predecessor, ["superseded_by"])

        predecessor.superseded_by = successor.id
        db.flush()
        assert predecessor.superseded_by == successor.id

    def test_the_old_value_is_read_from_the_database(self):
        body = (EVAL_DAY / "models.py").read_text(encoding="utf-8")
        assert "connection.execute" in body
        assert "load_history" in body
        assert "active_history" in body


class TestStaleSupersessionMutation:
    """The case active history CANNOT reach: a value that is loaded, but old.

    ``active_history`` makes SQLAlchemy fetch the committed value when the
    attribute is UNLOADED. It does nothing when the attribute was loaded
    correctly and another transaction has since changed it — the session then
    holds a NULL that is simply out of date, and the forbidden
    non-NULL -> different non-NULL re-point looks authorized again. Only
    reading the database at flush time distinguishes them, which is why the
    guard does exactly that.

    Two real sessions on one file-backed database, because two sessions sharing
    one in-memory connection would share one transaction and there would be
    nothing stale about either.
    """

    @pytest.fixture()
    def two_sessions(self, tmp_path):
        from database.models import Organization, User

        url = f"sqlite:///{(tmp_path / 'authority.db').as_posix()}"
        eng = sa.create_engine(url, future=True)
        for table in (Organization.__table__, User.__table__,
                      EcoDayRecord.__table__):
            table.create(eng, checkfirst=True)

        with SaSession(eng) as setup:
            setup.add(Organization(id=ORG, name="Org A"))
            setup.add(User(id=USER, name="W", email="w@example.test",
                           organization_id=ORG))
            setup.commit()

        first, second = SaSession(eng), SaSession(eng)
        try:
            yield first, second
        finally:
            first.close()
            second.close()
            eng.dispose()

    def test_a_loaded_but_stale_null_cannot_be_re_pointed(self, two_sessions):
        writer, competitor = two_sessions

        predecessor = _plain_record(writer, generation=1, key="p" * 64)
        writer.commit()
        pid = predecessor.id

        # The writer reads the predecessor while it is genuinely current, so
        # its copy is LOADED and correct at the moment it is read.
        stale = writer.get(EcoDayRecord, pid)
        assert stale.superseded_by is None

        # A competitor supersedes it and commits.
        successor = _plain_record(competitor, generation=2, key="s" * 64,
                                  supersedes=pid)
        competitor.execute(
            sa.update(EcoDayRecord)
            .where(EcoDayRecord.id == pid,
                   EcoDayRecord.superseded_by.is_(None))
            .values(superseded_by=successor.id))
        competitor.commit()

        # The writer's copy still says NULL. Active history cannot help here:
        # the value is loaded, just wrong.
        assert stale.__dict__.get("superseded_by") is None
        other = _plain_record(writer, generation=3, key="o" * 64)

        with pytest.raises(DayRecordImmutable):
            stale.superseded_by = other.id
            writer.flush()
        writer.rollback()

        # And the database still records the real supersession.
        assert writer.execute(
            sa.select(EcoDayRecord.superseded_by)
            .where(EcoDayRecord.id == pid)).scalar() == successor.id

    def test_a_genuinely_null_predecessor_is_still_linkable(self, two_sessions):
        """Guard the guard: the database read must not refuse everything."""
        writer, _ = two_sessions

        predecessor = _plain_record(writer, generation=1, key="p" * 64)
        successor = _plain_record(writer, generation=2, key="s" * 64,
                                  supersedes=predecessor.id)
        predecessor.superseded_by = successor.id
        writer.flush()
        assert predecessor.superseded_by == successor.id


class TestIntegrityErrorTriage:
    """Four distinct paths, and an unrelated error is none of them."""

    def _existing_day(self, db):
        _config(db, effective_from=DAY - timedelta(days=30))
        led = _worked_day(db)
        _record(db)
        return led

    def test_an_unrelated_error_on_a_later_record_is_re_raised(self, db,
                                                               monkeypatch):
        """The audit's case: a day that ALREADY holds a record.

        Previously this was retried four times and surfaced as contention,
        discarding the real constraint failure.
        """
        from app.config_versioning import DatabaseConfigVersionProvider
        from app.eval_day import evaluate_day, record_evaluation
        from app.eval_day import evaluator as ev

        led = self._existing_day(db)
        assert len(_rows(db)) == 1, "the day must already hold a record"

        evaluation = evaluate_day(
            ledger=led,
            intervals=list(db.execute(sa.select(WorkStateInterval).where(
                WorkStateInterval.ledger_id == led.id)).scalars().all()),
            provider=DatabaseConfigVersionProvider(db),
        )
        # NOT NULL on requirement_outcome: an error with nothing to do with
        # identity, generation or supersession.
        object.__setattr__(evaluation, "requirement_outcome", None)
        monkeypatch.setattr(ev, "_existing", lambda *a, **k: None)

        with pytest.raises(sa.exc.IntegrityError):
            record_evaluation(db, evaluation,
                              leave_input_snapshot_key="k" * 64)
        db.rollback()

    def test_a_taken_generation_is_identified_positively(self, db):
        from app.eval_day.evaluator import _generation_taken

        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        result = _record(db)

        class _Ev:
            organization_id = ORG
            user_id = USER
            work_date = DAY

        assert _generation_taken(db, _Ev(), 1) is True
        assert _generation_taken(db, _Ev(), 2) is False
        assert result.record.evaluation_generation == 1

    def test_contention_is_raised_only_for_repeated_contention(self, db,
                                                               monkeypatch):
        """Exhausting the retries is the ONLY route to contention."""
        from app.eval_day import DayRecordAuthorityContention
        from app.eval_day import evaluator as ev

        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        _record(db)
        _leave(db, minutes=15)

        # Always stale: every attempt derives a generation already taken.
        monkeypatch.setattr(ev, "_generation_after", lambda pred: 1)
        with pytest.raises(DayRecordAuthorityContention):
            _record(db)
        db.rollback()

    def test_the_four_paths_are_distinct_in_the_source(self):
        body = (EVAL_DAY / "evaluator.py").read_text(encoding="utf-8")
        assert "_StalePredecessor" in body
        assert "_generation_taken" in body


class TestSnapshotDuplicateRace:
    """Concurrent identical snapshot creation resolves to one row."""

    def test_the_loser_returns_the_existing_snapshot(self, db, monkeypatch):
        """Blinding the pre-read is exactly a losing writer's state."""
        kwargs = dict(organization_id=ORG, user_id=USER, work_date=DAY,
                      overlap_seconds=900,
                      contributions=(_c(request_id=8001, seconds=900),))
        winner = get_or_create_leave_snapshot(db, **kwargs)
        db.flush()

        # Blinding the writer's own pre-read once is exactly the state of a
        # writer whose SELECT ran before the winner's INSERT was committed.
        calls = {"n": 0}
        original_execute = db.execute

        def _blind_first(statement, *args, **kwargs_):
            text = str(statement)
            if ("leave_input_snapshot" in text
                    and text.strip().upper().startswith("SELECT")):
                calls["n"] += 1
                if calls["n"] == 1:
                    class _Blind:
                        def scalars(self_inner):
                            class _Scalars:
                                def first(self_s):
                                    return None
                            return _Scalars()
                    return _Blind()
            return original_execute(statement, *args, **kwargs_)

        monkeypatch.setattr(db, "execute", _blind_first)
        loser = get_or_create_leave_snapshot(db, **kwargs)
        monkeypatch.undo()

        assert calls["n"] >= 2, "the writer must have re-read after the refusal"
        assert loser.id == winner.id
        rows = db.execute(
            sa.select(LeaveInputSnapshot).where(
                LeaveInputSnapshot.snapshot_key == winner.snapshot_key)
        ).scalars().all()
        assert len(rows) == 1

    def test_the_recovery_is_specific_to_the_duplicate(self):
        body = (EVAL_DAY / "leave_snapshot.py").read_text(encoding="utf-8")
        assert "begin_nested" in body
        assert "IntegrityError" in body

    def test_children_are_written_once(self, db):
        kwargs = dict(organization_id=ORG, user_id=USER, work_date=DAY,
                      overlap_seconds=900,
                      contributions=(_c(request_id=8101, seconds=900),))
        first = get_or_create_leave_snapshot(db, **kwargs)
        second = get_or_create_leave_snapshot(db, **kwargs)
        assert first.id == second.id
        assert len(read_contributions(db, snapshot=first)) == 1


class TestSchemaGuards:
    """The model/migration parity gaps the audit named."""

    MIGRATION = (BACKEND / "alembic" / "versions"
                 / "d3a1c8b57f92_leave_input_snapshot_and_day_authority.py")

    def test_the_model_declares_the_day_record_checks(self):
        names = {c.name for c in EcoDayRecord.__table__.constraints
                 if c.name}
        assert "ck_eco_day_record_generation_positive" in names
        assert "ck_eco_day_record_leave_key_nonempty" in names

    def test_the_migration_adds_the_same_day_record_checks(self):
        """PROBE F's target: the migration must not lag the model."""
        body = self.MIGRATION.read_text(encoding="utf-8")
        assert "ck_eco_day_record_generation_positive" in body
        assert "evaluation_generation >= 1" in body
        assert "ck_eco_day_record_leave_key_nonempty" in body
        assert "length(trim(leave_input_snapshot_key)) > 0" in body
        assert "ADD CONSTRAINT" in body

    def _migration_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_d3a1c8b57f92_parity", self.MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_migration_expressions_match_the_model_expressions(self):
        """Parity by comparing the expressions, not by matching text.

        A migration that added a DIFFERENT check under the right name would
        satisfy a source scan and still leave production enforcing something
        else.
        """
        module = self._migration_module()
        migration_checks = dict(module.DAY_RECORD_CHECKS)

        model_checks = {
            c.name: str(c.sqltext)
            for c in EcoDayRecord.__table__.constraints
            if isinstance(c, sa.CheckConstraint) and c.name
        }
        for name, expression in migration_checks.items():
            assert name in model_checks, f"{name} is not on the model"
            assert model_checks[name] == expression, (
                f"{name}: model has {model_checks[name]!r}, "
                f"migration has {expression!r}")

    def test_the_postgresql_dialect_renders_both_checks(self):
        """The production dialect, compiled — not executed."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        ddl = str(CreateTable(EcoDayRecord.__table__).compile(
            dialect=postgresql.dialect()))
        assert "evaluation_generation >= 1" in ddl
        assert "length(trim(leave_input_snapshot_key)) > 0" in ddl

    def test_the_snapshot_key_equals_its_content_hash_by_constraint(self):
        names = {c.name for c in LeaveInputSnapshot.__table__.constraints
                 if c.name}
        assert "ck_leave_input_snapshot_key_is_hash" in names
        body = self.MIGRATION.read_text(encoding="utf-8")
        assert "snapshot_key = content_hash" in body

    def test_an_empty_day_record_leave_key_is_refused_by_the_database(self, db):
        """Ruling E below the writer."""
        with pytest.raises(sa.exc.IntegrityError):
            _plain_record(db, generation=9, key="", content_hash="h" * 64)
        db.rollback()

    def test_a_whitespace_day_record_leave_key_is_refused(self, db):
        with pytest.raises(sa.exc.IntegrityError):
            _plain_record(db, generation=9, key="   ", content_hash="h" * 64)
        db.rollback()

    def test_a_snapshot_key_that_is_not_its_hash_is_refused(self, db):
        db.add(LeaveInputSnapshot(
            snapshot_key="k" * 64, organization_id=ORG, user_id=USER,
            work_date=DAY, content_hash="z" * 64, overlap_seconds=0,
            contribution_count=0, captured_at_utc=NOW))
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_a_zero_generation_is_refused(self, db):
        with pytest.raises(sa.exc.IntegrityError):
            _plain_record(db, generation=0, key="g" * 64)
        db.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# OWNER RULING C — A2-narrow. Withdrawn leave representation.
# ═════════════════════════════════════════════════════════════════════════════
def _withdraw_set(db, *, request, interval_set, version=2):
    """Withdrawal: a NEW set version holding no intervals. Nothing mutated."""
    successor = LeaveIntervalSet(
        organization_id=ORG, user_id=USER, leave_request_id=request.id,
        set_version=version, supersedes=interval_set.id,
        timezone_iana="Asia/Kolkata", recorded_by="TEST", recorded_at_utc=NOW,
    )
    db.add(successor)
    db.flush()
    return successor


def _redate_set(db, *, request, interval_set, to_day, minutes=15, version=2):
    """A CORRECTION, not a withdrawal: a new version with the interval moved."""
    successor = LeaveIntervalSet(
        organization_id=ORG, user_id=USER, leave_request_id=request.id,
        set_version=version, supersedes=interval_set.id,
        timezone_iana="Asia/Kolkata", recorded_by="TEST", recorded_at_utc=NOW,
    )
    db.add(successor)
    db.flush()
    begin = datetime(to_day.year, to_day.month, to_day.day, 18, 0, tzinfo=UTC)
    db.add(LeaveInterval(
        set_id=successor.id, organization_id=ORG, seq=1, work_date=to_day,
        start_at_utc=begin, end_at_utc=begin + timedelta(minutes=minutes),
        duration_seconds=minutes * 60))
    db.flush()
    return successor


class TestWithdrawnLeaveRepresentation:
    """Ruling C, at the resolver rather than through a recorded day."""

    def _overlap(self, db, on=DAY):
        from app.eval_day.exclusion import approved_leave_overlap

        return approved_leave_overlap(
            db, organization_id=ORG, user_id=USER, work_date=on)

    def test_a_withdrawn_request_is_represented(self, db):
        request, interval_set = _leave(db, minutes=15)
        _withdraw_set(db, request=request, interval_set=interval_set)

        overlap = self._overlap(db)
        assert overlap.is_resolved
        assert overlap.overlap_seconds == 0          # arithmetic unchanged
        assert len(overlap.contributions) == 1
        assert overlap.contributions[0].leave_request_id == request.id

    def test_the_selected_empty_set_version_is_used(self, db):
        """Not the version whose intervals used to claim the date."""
        request, interval_set = _leave(db, minutes=15)
        _withdraw_set(db, request=request, interval_set=interval_set,
                      version=2)

        contribution = self._overlap(db).contributions[0]
        assert contribution.set_version == 2
        assert interval_set.set_version == 1

    def test_the_empty_list_fingerprint_is_used(self, db):
        from app.eval_day.exclusion import EMPTY_INTERVAL_FINGERPRINT

        request, interval_set = _leave(db, minutes=15)
        _withdraw_set(db, request=request, interval_set=interval_set)

        contribution = self._overlap(db).contributions[0]
        assert contribution.interval_fingerprint == EMPTY_INTERVAL_FINGERPRINT
        # Independently: it is the fingerprint of an empty list, and non-empty.
        assert contribution.interval_fingerprint == leave_interval_fingerprint(())
        assert len(contribution.interval_fingerprint) == 64
        assert contribution.interval_count == 0
        assert contribution.contributed_seconds == 0

    def test_the_raw_status_is_preserved(self, db):
        """Withdrawing boundaries says nothing about the grant's status."""
        for status in (APPROVED, LOCKED, REJECTED):
            request, interval_set = _leave(db, minutes=15, status=status)
            _withdraw_set(db, request=request, interval_set=interval_set)
            found = [c for c in self._overlap(db).contributions
                     if c.leave_request_id == request.id]
            assert [c.status for c in found] == [status]
            # Clean up so each status is tested in isolation.
            db.rollback()

    def test_a_re_dated_correction_is_not_a_withdrawal(self, db):
        """A2-narrow: the request still holds boundaries, just elsewhere."""
        other_day = DAY + timedelta(days=1)
        request, interval_set = _leave(db, minutes=15)
        _redate_set(db, request=request, interval_set=interval_set,
                    to_day=other_day)

        # The ORIGINAL date must not report a withdrawal.
        original = self._overlap(db, on=DAY)
        assert original.is_resolved
        assert original.overlap_seconds == 0
        assert original.contributions == ()

        # And the new date resolves the moved leave normally.
        moved = self._overlap(db, on=other_day)
        assert moved.overlap_seconds == 15 * 60
        assert [c.leave_request_id for c in moved.contributions] == [request.id]
        assert moved.contributions[0].set_version == 2

    def test_a_never_recorded_day_still_has_no_contributions(self, db):
        overlap = self._overlap(db)
        assert overlap.is_resolved
        assert overlap.overlap_seconds == 0
        assert overlap.contributions == ()

    def test_current_version_intervals_behave_exactly_as_before(self, db):
        request, _ = _leave(db, minutes=15)
        overlap = self._overlap(db)
        assert overlap.overlap_seconds == 15 * 60
        assert overlap.interval_count == 1
        assert overlap.leave_request_ids == (request.id,)
        assert [c.contributed_seconds for c in overlap.contributions] == [900]

    def test_a_withdrawal_and_live_leave_coexist_on_one_day(self, db):
        live, _ = _leave(db, minutes=10)
        gone, gone_set = _leave(db, minutes=15)
        _withdraw_set(db, request=gone, interval_set=gone_set)

        overlap = self._overlap(db)
        assert overlap.overlap_seconds == 10 * 60
        by_request = {c.leave_request_id: c for c in overlap.contributions}
        assert set(by_request) == {live.id, gone.id}
        assert by_request[live.id].contributed_seconds == 10 * 60
        assert by_request[gone.id].contributed_seconds == 0
        assert by_request[gone.id].interval_count == 0

    def test_no_unrelated_request_is_pulled_into_the_day(self, db):
        """A withdrawal on ANOTHER date must not appear on this one."""
        other_day = DAY + timedelta(days=3)
        request, interval_set = _leave(db, on=other_day, minutes=15)
        _withdraw_set(db, request=request, interval_set=interval_set)

        assert self._overlap(db, on=DAY).contributions == ()
        # It does appear on its own date.
        assert [c.leave_request_id
                for c in self._overlap(db, on=other_day).contributions] == [
                    request.id]

    def test_another_worker_withdrawal_is_not_visible(self, db):
        from database.models import LeaveRequest

        request = LeaveRequest(
            id=77001, user_id=OTHER_USER, organization_id=ORG,
            leave_type="Casual Leave",
            from_date=datetime(DAY.year, DAY.month, DAY.day),
            to_date=datetime(DAY.year, DAY.month, DAY.day),
            description="x", status=APPROVED)
        db.add(request)
        db.flush()
        first = LeaveIntervalSet(
            organization_id=ORG, user_id=OTHER_USER,
            leave_request_id=request.id, set_version=1, supersedes=None,
            timezone_iana="Asia/Kolkata", recorded_by="TEST",
            recorded_at_utc=NOW)
        db.add(first)
        db.flush()
        begin = datetime(DAY.year, DAY.month, DAY.day, 18, 0, tzinfo=UTC)
        db.add(LeaveInterval(
            set_id=first.id, organization_id=ORG, seq=1, work_date=DAY,
            start_at_utc=begin, end_at_utc=begin + timedelta(minutes=15),
            duration_seconds=900))
        db.flush()
        db.add(LeaveIntervalSet(
            organization_id=ORG, user_id=OTHER_USER,
            leave_request_id=request.id, set_version=2, supersedes=first.id,
            timezone_iana="Asia/Kolkata", recorded_by="TEST",
            recorded_at_utc=NOW))
        db.flush()

        assert self._overlap(db).contributions == ()

    def test_a_recorded_day_carries_the_withdrawal_into_the_snapshot(self, db):
        """End to end: resolver -> snapshot -> authoritative day record."""
        _config(db, effective_from=DAY - timedelta(days=30))
        _worked_day(db)
        request, interval_set = _leave(db, minutes=15)
        first = _record(db)
        assert first.record.exclusion_minutes == 15

        _withdraw_set(db, request=request, interval_set=interval_set)
        second = _record(db)

        assert second.record.exclusion_minutes == 0
        assert second.record.evaluation_generation == 2
        assert second.record.supersedes == first.record.id
        snapshot = read_leave_snapshot(
            db, organization_id=ORG,
            snapshot_key=second.record.leave_input_snapshot_key)
        assert snapshot.overlap_seconds == 0
        assert snapshot.contribution_count == 1


class TestDayVersionHistoryReader:
    """The new reader, on its own terms."""

    def test_it_reports_a_withdrawal(self, db):
        from app.leave_intervals import day_version_history

        request, interval_set = _leave(db, minutes=15)
        _withdraw_set(db, request=request, interval_set=interval_set)

        rows = day_version_history(
            db, organization_id=ORG, user_id=USER, work_date=DAY)
        assert len(rows) == 1
        assert rows[0].leave_request_id == request.id
        assert rows[0].current_set_version == 2
        assert rows[0].current_set_exists is True
        assert rows[0].current_interval_count == 0
        assert rows[0].current_on_this_date is False
        assert rows[0].is_withdrawn is True

    def test_it_reports_a_re_dating_as_not_withdrawn(self, db):
        from app.leave_intervals import day_version_history

        request, interval_set = _leave(db, minutes=15)
        _redate_set(db, request=request, interval_set=interval_set,
                    to_day=DAY + timedelta(days=1))

        rows = day_version_history(
            db, organization_id=ORG, user_id=USER, work_date=DAY)
        assert len(rows) == 1
        assert rows[0].current_interval_count == 1
        assert rows[0].current_on_this_date is False
        assert rows[0].is_withdrawn is False

    def test_it_reports_a_live_current_interval(self, db):
        from app.leave_intervals import day_version_history

        request, _ = _leave(db, minutes=15)
        rows = day_version_history(
            db, organization_id=ORG, user_id=USER, work_date=DAY)
        assert len(rows) == 1
        assert rows[0].current_on_this_date is True
        assert rows[0].is_withdrawn is False

    def test_it_returns_nothing_for_a_date_never_claimed(self, db):
        from app.leave_intervals import day_version_history

        _leave(db, minutes=15)
        assert day_version_history(
            db, organization_id=ORG, user_id=USER,
            work_date=DAY + timedelta(days=5)) == ()

    def test_it_is_tenant_scoped(self, db):
        from app.leave_intervals import day_version_history

        request, interval_set = _leave(db, minutes=15)
        _withdraw_set(db, request=request, interval_set=interval_set)

        assert day_version_history(
            db, organization_id=OTHER_ORG, user_id=USER, work_date=DAY) == ()
        assert day_version_history(
            db, organization_id=ORG, user_id=OTHER_USER, work_date=DAY) == ()


class TestRulingCScopeHeld:
    """What ruling C must NOT have changed."""

    def test_intervals_for_day_is_still_current_version_only(self, db):
        from app.leave_intervals import intervals_for_day

        request, interval_set = _leave(db, minutes=15)
        assert len(intervals_for_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY)) == 1

        _withdraw_set(db, request=request, interval_set=interval_set)
        assert intervals_for_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY) == ()

    def test_exclusion_reads_leave_only_through_the_service(self):
        """The accessor guard, restated here so ruling C cannot loosen it."""
        tree = ast.parse((EVAL_DAY / "exclusion.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.update(a.name for a in node.names)
        assert "intervals_for_day" in imported
        assert "day_version_history" in imported
        # The interval MODEL is still never imported, so neither the tenant nor
        # the current-version filtering can be bypassed.
        assert "LeaveInterval" not in imported

    def test_no_attribution_rule_is_re_derived(self):
        """Ruling C forbids a new timezone or calendar rule."""
        tokens = _code_tokens(
            ast.parse((EVAL_DAY / "exclusion.py").read_text(encoding="utf-8")))
        for banned in ("attributed_work_date", "from_date", "to_date",
                       "is_half_day", "half_day_period", "astimezone",
                       "ZoneInfo", "timedelta"):
            assert banned not in tokens, f"exclusion.py uses {banned}"

    def test_the_reader_re_derives_no_date_either(self):
        tokens = _code_tokens(
            ast.parse((BACKEND / "app" / "leave_intervals" / "service.py")
                      .read_text(encoding="utf-8")))
        # The reader compares the STORED work_date; it must not recompute one.
        assert "from_date" not in tokens
        assert "to_date" not in tokens

    def test_the_withdrawal_branch_is_narrow_in_the_source(self):
        body = (EVAL_DAY / "exclusion.py").read_text(encoding="utf-8")
        assert "is_withdrawn" in body
        assert "day_version_history" in body
