"""
P-1 / P-2 / P-3 / C-1 / D-1 / D-2 — the break-classification foundation.
=========================================================================

Covers the authority and assertion model, the post-seal rule, the
content-addressed snapshot, the break-treatment profile, the deferral reason
and its rendering — the whole increment, and nothing beyond it.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No netting, no horizon, no delivery and no variance.

GV-02/GV-03 execution is not here either, but for a different reason now: the
P-2 CALCULATION landed in a later increment and those vectors were promoted on
2026-09-21. Their execution lives in ``test_gv02_gv03_execution.py`` and the
arithmetic regressions in ``test_break_treatment_calculation.py``; this suite
stays scoped to the FOUNDATION — authority, assertion, snapshot, profile
storage, the deferral reason and its rendering.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib

import pytest
import sqlalchemy as sa

import database.models as dbm
from database.database import Base

from app.break_classification import (
    BREAK_SOURCE_PREFIX,
    BreakClassificationAssertion,
    ClassificationImmutable,
    ClassificationOutcome,
    ClassificationResult,
    ClassificationSnapshot,
    EcoClassificationAuthority,
    InvalidClassificationAssertion,
    InvalidClassificationAuthority,
    REASON_CONFLICTING_ASSERTIONS,
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_NOT_SEALED,
    REASON_MALFORMED_SOURCE_REF,
    REASON_NO_LEDGER_DAY,
    assert_break_classification,
    compute_assertion_content_hash,
    compute_snapshot_hash,
    grant_classification_authority,
    resolve_classification_for_day,
)
from app.break_treatment import (
    BreakTreatmentImmutable,
    BreakTreatmentProfile,
    BreakTreatmentProfileCollision,
    InvalidBreakTreatmentProfile,
    ProfileOutcome,
    capture_break_treatment_profile,
    compute_profile_content_hash,
    resolve_profile_as_of,
)
from app.eval_day.evaluator import DayEvaluation
from app.eval_day.states import (
    DEFERRED_STATES,
    EcoDayState,
    EvaluationDeferralReason,
    REASON_BEARING_STATES,
    is_valid_deferral_reason,
)
from app.work_ledger import LedgerState, WorkDayLedger

UTC = dt.timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]

ORG, USER = 9310, 9311
DAY = dt.date(2026, 9, 7)
MONDAY_WEEK = "2026-09-07"
NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
FLOOR = dt.date(2020, 1, 1)


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    Base.metadata.create_all(engine)


@pytest.fixture()
def org(db):
    if db.get(dbm.Organization, ORG) is None:
        db.add(dbm.Organization(id=ORG, name="Classification Org"))
        db.flush()
        db.add(dbm.User(id=USER, name="W", email="w@classify.test",
                        organization_id=ORG, role="employee"))
        db.flush()
    return ORG


def _authority(db, *, label="hr.compliance@tenant.test"):
    row = grant_classification_authority(
        db, organization_id=ORG, user_id=USER, authority_label=label,
        granted_by="owner@tenant.test")
    db.flush()          # the assertion needs a real id to reference
    return row


def _ledger(db, *, state=LedgerState.SEALED.value, on=DAY):
    led = WorkDayLedger(
        organization_id=ORG, user_id=USER,
        work_date=dt.datetime(on.year, on.month, on.day),
        workweek_key=MONDAY_WEEK, ledger_version=1, state=state,
        timezone_iana="Asia/Kolkata",
        is_working_day=(state != LedgerState.NOT_A_WORKING_DAY.value),
        working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST")
    db.add(led)
    db.flush()
    return led


def _assert(db, authority, ref="break_session:1",
            result=ClassificationResult.BRK_R.value,
            evidence="ruling:2026-collective-agreement-clause-7"):
    return assert_break_classification(
        db, organization_id=ORG, user_id=USER, work_date=DAY,
        source_ref=ref, classification=result, evidence_ref=evidence,
        authority_id=authority.id)


# ══════════════════════════════════════════════════════════════════════════════
# 1. The deferral reason (D-1)
# ══════════════════════════════════════════════════════════════════════════════
class TestDeferralReason:
    """D-1: one carrier, two conditions, distinguished by the reason."""

    _COMMON = dict(organization_id=ORG, user_id=USER, work_date=DAY,
                   ledger_version=1, requirement_outcome="NOT_APPLICABLE",
                   shortfall_computed=False,
                   contributes_to_accumulator_minutes=0)

    def test_no_new_eco_day_state_was_added(self):
        """The whole point of D-1: seven states, unchanged."""
        assert len(list(EcoDayState)) == 7
        assert "CLASSIFICATION_UNRESOLVED" not in {s.value for s in EcoDayState}
        assert DEFERRED_STATES == {EcoDayState.DEFERRED_PENDING_LEDGER}

    def test_departure_deferral_carries_no_reason(self):
        """NULL is the reading this state has always had, and it still is."""
        ev = DayEvaluation(state=EcoDayState.DEFERRED_PENDING_LEDGER,
                           **self._COMMON)
        assert ev.evaluation_deferral_reason is None

    def test_classification_deferral_carries_the_reason(self):
        ev = DayEvaluation(
            state=EcoDayState.DEFERRED_PENDING_LEDGER,
            evaluation_deferral_reason=(
                EvaluationDeferralReason.CLASSIFICATION_UNRESOLVED.value),
            **self._COMMON)
        assert ev.evaluation_deferral_reason == "CLASSIFICATION_UNRESOLVED"
        assert ev.contributes_to_accumulator_minutes == 0
        assert ev.shortfall_computed is False
        # S2: every measurement stays NULL.
        for field in ("required_minutes", "qualifying_minutes",
                      "gross_shortfall_minutes", "exclusion_minutes",
                      "attributable_minutes", "schedule_variance_minutes"):
            assert getattr(ev, field) is None, field

    @pytest.mark.parametrize("state", [
        EcoDayState.SHORTFALL, EcoDayState.NO_SHORTFALL,
        EcoDayState.NOT_A_WORKING_DAY,
    ])
    def test_a_reason_on_a_computed_state_fails_closed(self, state):
        """D-1's invalid combinations. A computed day was not deferred."""
        with pytest.raises(ValueError, match="must not carry"):
            DayEvaluation(
                organization_id=ORG, user_id=USER, work_date=DAY,
                ledger_version=1, state=state,
                requirement_outcome="RESOLVED", shortfall_computed=True,
                required_minutes=480, qualifying_minutes=480,
                gross_shortfall_minutes=0, exclusion_minutes=0,
                attributable_minutes=0,
                contributes_to_accumulator_minutes=0,
                evaluation_deferral_reason="CLASSIFICATION_UNRESOLVED")

    @pytest.mark.parametrize("state", [
        EcoDayState.REQUIREMENT_UNRESOLVED, EcoDayState.POLICY_UNRESOLVED,
    ])
    def test_a_reason_on_an_uncomputed_state_fails_closed(self, state):
        with pytest.raises(ValueError, match="must not carry"):
            DayEvaluation(
                organization_id=ORG, user_id=USER, work_date=DAY,
                ledger_version=1, state=state,
                requirement_outcome="NOT_APPLICABLE", shortfall_computed=False,
                evaluation_deferral_reason="CLASSIFICATION_UNRESOLVED")

    def test_an_unknown_reason_fails_closed(self):
        with pytest.raises(ValueError, match="is not one of"):
            DayEvaluation(
                state=EcoDayState.DEFERRED_PENDING_LEDGER,
                evaluation_deferral_reason="SOMETHING_ELSE", **self._COMMON)

    def test_the_vocabulary_is_closed(self):
        assert [r.value for r in EvaluationDeferralReason] == [
            "CLASSIFICATION_UNRESOLVED"]
        assert is_valid_deferral_reason("CLASSIFICATION_UNRESOLVED")
        assert not is_valid_deferral_reason("classification_unresolved")
        assert not is_valid_deferral_reason("UNKNOWN")
        assert REASON_BEARING_STATES == {EcoDayState.DEFERRED_PENDING_LEDGER}

    def test_the_reason_is_inside_the_result_hash(self):
        """D-1: "not cosmetic metadata"."""
        plain = DayEvaluation(state=EcoDayState.DEFERRED_PENDING_LEDGER,
                              **self._COMMON)
        classified = DayEvaluation(
            state=EcoDayState.DEFERRED_PENDING_LEDGER,
            evaluation_deferral_reason="CLASSIFICATION_UNRESOLVED",
            **self._COMMON)
        assert plain.result_fields() != classified.result_fields()
        assert "evaluation_deferral_reason" in plain.result_fields()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Classification authority and assertions (P-1)
# ══════════════════════════════════════════════════════════════════════════════
class TestAuthorityAndAssertion:
    def test_an_authority_is_explicit_and_durable(self, db, org):
        row = _authority(db)
        assert row.organization_id == ORG and row.user_id == USER
        assert row.is_live and row.revoked_at_utc is None

    def test_no_existing_role_model_is_consulted(self):
        """P-1: never ``User.role``, ``Team.head_user_id`` or another role."""
        pkg = BACKEND / "app" / "break_classification"
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            reads = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)}
            for banned in ("role", "head_user_id", "is_admin", "is_superuser"):
                assert banned not in reads, f"{path.name} reads {banned}"

    def test_the_worker_selected_label_is_never_read(self):
        """``break_sessions.break_type`` stays observational (P-1)."""
        pkg = BACKEND / "app" / "break_classification"
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            reads = {n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)}
            assert "break_type" not in reads, path.name

    def test_an_assertion_preserves_every_required_element(self, db, org):
        a = _authority(db)
        row = _assert(db, a)
        assert row.organization_id == ORG              # organization
        assert row.user_id == USER                     # worker
        assert row.source_ref == "break_session:1"     # source reference
        assert row.classification == "BRK_R"           # result
        assert row.evidence_ref                        # evidence/provenance
        assert row.authority_id == a.id                # asserting authority
        assert row.authority_label == a.authority_label
        assert row.asserted_at_utc is not None         # timestamp
        assert row.assertion_version == 1              # lineage
        assert len(row.content_hash) == 64

    def test_the_content_hash_excludes_the_recording_timestamp(self, db, org):
        a = _authority(db)
        row = _assert(db, a)
        assert row.content_hash == compute_assertion_content_hash(
            organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref="break_session:1", classification="BRK_R",
            evidence_ref="ruling:2026-collective-agreement-clause-7",
            authority_label=a.authority_label)

    def test_re_asserting_the_identical_finding_is_idempotent(self, db, org):
        a = _authority(db)
        first = _assert(db, a)
        again = _assert(db, a)
        assert again.id == first.id
        assert again.assertion_version == 1

    def test_a_changed_finding_supersedes_and_never_rewrites(self, db, org):
        a = _authority(db)
        first = _assert(db, a, result=ClassificationResult.BRK_R.value)
        second = _assert(db, a, result=ClassificationResult.BRK_M.value)

        assert second.id != first.id
        assert second.assertion_version == 2
        assert second.supersedes == first.id
        db.refresh(first)
        assert first.superseded_by == second.id
        # The superseded row keeps its own finding, unrewritten.
        assert first.classification == "BRK_R"
        assert second.is_live and not first.is_live

    def test_an_assertion_cannot_be_updated_or_deleted(self, db, org):
        a = _authority(db)
        row = _assert(db, a)
        row.classification = "BRK_M"
        with pytest.raises(ClassificationImmutable):
            db.flush()
        db.rollback()

    def test_an_unknown_classification_is_refused(self, db, org):
        a = _authority(db)
        with pytest.raises(InvalidClassificationAssertion, match="not one of"):
            _assert(db, a, result="LUNCH")
        # And a case variant is not helpfully corrected.
        with pytest.raises(InvalidClassificationAssertion):
            _assert(db, a, result="brk_r")

    def test_evidence_reference_is_mandatory(self, db, org):
        a = _authority(db)
        with pytest.raises(InvalidClassificationAssertion, match="evidence_ref"):
            _assert(db, a, evidence="")

    def test_a_malformed_source_reference_is_refused(self, db, org):
        a = _authority(db)
        with pytest.raises(InvalidClassificationAssertion, match="does not name"):
            _assert(db, a, ref="clock_session:9")

    def test_a_revoked_authority_asserts_nothing_new(self, db, org):
        a = _authority(db)
        a.revoked_at_utc = NOW
        db.flush()
        with pytest.raises(InvalidClassificationAuthority, match="revoked"):
            _assert(db, a)

    def test_the_unclassifiable_result_is_explicit(self, db, org):
        a = _authority(db)
        row = _assert(db, a,
                      result=ClassificationResult.UNCLASSIFIABLE.value)
        assert row.classification == "UNCLASSIFIABLE"
        assert ClassificationResult.UNCLASSIFIABLE.value not in (
            "BRK_M", "BRK_R", "WRK")


# ══════════════════════════════════════════════════════════════════════════════
# 3. The post-seal rule (P-2)
# ══════════════════════════════════════════════════════════════════════════════
class TestPostSeal:
    def _resolve(self, db, ledger, refs=("break_session:1",)):
        return resolve_classification_for_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            ledger=ledger, expected_source_refs=list(refs))

    def test_a_sealed_ledger_is_accepted(self, db, org):
        a = _authority(db)
        _assert(db, a)
        out = self._resolve(db, _ledger(db))
        assert out.outcome is ClassificationOutcome.RESOLVED
        assert out.classifications == {"break_session:1": "BRK_R"}

    def test_an_open_ledger_is_refused(self, db, org):
        out = self._resolve(db, _ledger(db, state=LedgerState.OPEN.value))
        assert out.is_refused and out.reason == REASON_LEDGER_NOT_SEALED

    def test_a_missing_ledger_is_refused(self, db, org):
        out = self._resolve(db, None)
        assert out.is_refused and out.reason == REASON_NO_LEDGER_DAY

    def test_an_integrity_fault_is_refused(self, db, org):
        out = self._resolve(
            db, _ledger(db, state=LedgerState.LEDGER_INTEGRITY_FAULT.value))
        assert out.is_refused and out.reason == REASON_LEDGER_INTEGRITY_FAULT

    def test_a_malformed_expected_reference_is_refused(self, db, org):
        out = self._resolve(db, _ledger(db), refs=("session:1",))
        assert out.is_refused and out.reason == REASON_MALFORMED_SOURCE_REF

    def test_conflicting_live_assertions_fail_closed(self, db, org):
        a1 = _authority(db)
        a2 = grant_classification_authority(
            db, organization_id=ORG, user_id=USER + 1,
            authority_label="second@tenant.test", granted_by="owner")
        db.add(dbm.User(id=USER + 1, name="A", email="a@classify.test",
                        organization_id=ORG, role="employee"))
        db.flush()
        _assert(db, a1)
        # A second LIVE row for the same break, written around the service.
        db.add(BreakClassificationAssertion(
            organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref="break_session:1", classification="BRK_M",
            evidence_ref="other", authority_id=a2.id,
            authority_label=a2.authority_label, assertion_version=99,
            content_hash="x" * 64))
        db.flush()

        out = self._resolve(db, _ledger(db))
        assert out.is_refused
        assert out.reason == REASON_CONFLICTING_ASSERTIONS

    def test_the_producer_contains_no_classification_logic(self):
        """D-18 stands: the producer is classification-blind."""
        pkg = BACKEND / "app" / "work_ledger_producer"
        for path in sorted(pkg.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            tree = ast.parse(body)
            imported = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module:
                    imported.add(n.module)
                elif isinstance(n, ast.Import):
                    imported |= {al.name for al in n.names}
            for mod in imported:
                assert "break_classification" not in mod, path.name
                assert "break_treatment" not in mod, path.name


# ══════════════════════════════════════════════════════════════════════════════
# 4. The classification snapshot (C-1)
# ══════════════════════════════════════════════════════════════════════════════
class TestSnapshot:
    def _resolve(self, db, refs=("break_session:1",), ledger=None):
        # One ledger per day: the natural key is unique, so a helper that
        # created a second would collide rather than re-resolve.
        if ledger is None:
            ledger = db.query(WorkDayLedger).filter_by(
                organization_id=ORG, user_id=USER).first() or _ledger(db)
        return resolve_classification_for_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            ledger=ledger, expected_source_refs=list(refs))

    def test_the_key_is_the_content_hash(self, db, org):
        a = _authority(db)
        _assert(db, a)
        out = self._resolve(db)
        row = db.query(ClassificationSnapshot).filter_by(
            snapshot_key=out.snapshot_key).one()
        assert row.snapshot_key == row.content_hash
        assert len(row.snapshot_key) == 64

    def test_the_key_is_deterministic(self, db, org):
        a = _authority(db)
        row = _assert(db, a)
        members = [("break_session:1", "BRK_R", row.content_hash)]
        assert compute_snapshot_hash(
            organization_id=ORG, user_id=USER, work_date=DAY,
            members=members) == compute_snapshot_hash(
                organization_id=ORG, user_id=USER, work_date=DAY,
                members=members)

    def test_member_order_does_not_change_the_key(self, db, org):
        m = [("break_session:2", "BRK_M", "h2"),
             ("break_session:1", "BRK_R", "h1")]
        assert compute_snapshot_hash(
            organization_id=ORG, user_id=USER, work_date=DAY, members=m
        ) == compute_snapshot_hash(
            organization_id=ORG, user_id=USER, work_date=DAY,
            members=list(reversed(m)))

    def test_an_unchanged_classification_replays_to_the_same_snapshot(
            self, db, org):
        a = _authority(db)
        _assert(db, a)
        first = self._resolve(db)
        again = self._resolve(db)
        assert again.snapshot_key == first.snapshot_key
        assert db.query(ClassificationSnapshot).filter_by(
            snapshot_key=first.snapshot_key).count() == 1

    def test_a_changed_classification_changes_the_key(self, db, org):
        a = _authority(db)
        _assert(db, a, result="BRK_R")
        before = self._resolve(db).snapshot_key
        _assert(db, a, result="BRK_M")
        after = self._resolve(db).snapshot_key
        assert after != before

    def test_a_snapshot_cannot_be_mutated(self, db, org):
        a = _authority(db)
        _assert(db, a)
        out = self._resolve(db)
        row = db.query(ClassificationSnapshot).filter_by(
            snapshot_key=out.snapshot_key).one()
        row.assertion_count = 99
        with pytest.raises(ClassificationImmutable):
            db.flush()
        db.rollback()

    def test_the_members_are_stored_canonically(self, db, org):
        a = _authority(db)
        _assert(db, a)
        out = self._resolve(db)
        row = db.query(ClassificationSnapshot).filter_by(
            snapshot_key=out.snapshot_key).one()
        members = json.loads(row.members_json)
        assert members == sorted(members, key=lambda m: m[0])
        assert members[0][0] == "break_session:1"

    def test_a_break_with_no_assertion_is_unclassifiable(self, db, org):
        """P-1: no fallback to the worker label. The day defers."""
        out = self._resolve(db)
        assert out.outcome is ClassificationOutcome.UNRESOLVED
        assert out.defers
        assert out.unclassifiable_refs == ("break_session:1",)
        assert out.snapshot_key is not None   # still frozen, still replayable

    def test_an_unclassifiable_assertion_defers(self, db, org):
        a = _authority(db)
        _assert(db, a, result=ClassificationResult.UNCLASSIFIABLE.value)
        out = self._resolve(db)
        assert out.defers
        assert out.unclassifiable_refs == ("break_session:1",)


# ══════════════════════════════════════════════════════════════════════════════
# 5. The break-treatment profile (P-3)
# ══════════════════════════════════════════════════════════════════════════════
class TestBreakTreatmentProfile:
    def _capture(self, db, *, ref="bt-v1", eff=FLOOR, rest=True, cap=30,
                 meal=False):
        return capture_break_treatment_profile(
            db, organization_id=ORG, profile_ref=ref, effective_from=eff,
            meal_qualifies=meal, rest_qualifies=rest,
            rest_qualifying_cap_minutes=cap, captured_by="owner@tenant.test")

    def test_it_carries_the_locked_semantic_fields(self, db, org):
        row = self._capture(db)
        assert row.rest_qualifies is True        # P-4's gate
        assert row.meal_qualifies is False       # BRK_M never qualifies here
        assert row.rest_qualifying_cap_minutes == 30   # cumulative per day
        assert len(row.content_hash) == 64

    def test_the_content_hash_excludes_capture_provenance(self, db, org):
        row = self._capture(db)
        assert row.content_hash == compute_profile_content_hash(
            organization_id=ORG, profile_ref="bt-v1", effective_from=FLOOR,
            meal_qualifies=False, rest_qualifies=True,
            rest_qualifying_cap_minutes=30)

    def test_it_is_immutable(self, db, org):
        row = self._capture(db)
        db.flush()      # an UPDATE is only an update once the row exists
        row.rest_qualifies = False
        with pytest.raises(BreakTreatmentImmutable):
            db.flush()
        db.rollback()

    def test_one_key_and_one_date_each_resolve_to_one_profile(self, db, org):
        self._capture(db)
        db.flush()
        with pytest.raises(BreakTreatmentProfileCollision):
            self._capture(db, ref="bt-v1", eff=dt.date(2021, 1, 1))
        db.rollback()
        self._capture(db)
        db.flush()
        with pytest.raises(BreakTreatmentProfileCollision):
            self._capture(db, ref="bt-v2", eff=FLOOR)
        db.rollback()

    def test_a_negative_cap_is_refused(self, db, org):
        with pytest.raises(InvalidBreakTreatmentProfile, match=">= 0"):
            self._capture(db, cap=-1)

    def test_null_cap_means_uncapped(self, db, org):
        self._capture(db, cap=None)
        db.flush()
        out = resolve_profile_as_of(db, organization_id=ORG, as_of=DAY)
        assert out.is_resolved
        assert out.rest_qualifying_cap_minutes is None

    def test_historical_resolution_uses_the_version_in_force(self, db, org):
        self._capture(db, ref="old", eff=FLOOR, cap=30)
        self._capture(db, ref="new", eff=DAY + dt.timedelta(days=1), cap=60)
        db.flush()
        out = resolve_profile_as_of(db, organization_id=ORG, as_of=DAY)
        assert out.is_resolved and out.profile_ref == "old"
        assert out.rest_qualifying_cap_minutes == 30

    def test_below_the_floor_there_is_no_version(self, db, org):
        self._capture(db, eff=dt.date(2026, 12, 1))
        db.flush()
        out = resolve_profile_as_of(db, organization_id=ORG, as_of=DAY)
        assert out.outcome is ProfileOutcome.NO_VERSION
        assert out.rest_qualifies is None    # carries no value at all


# ══════════════════════════════════════════════════════════════════════════════
# 6. Rendering (D-2)
# ══════════════════════════════════════════════════════════════════════════════
class TestRendering:
    def _view(self, reason):
        from app.eco_day_view.outcomes import EcoDayView
        return EcoDayView(
            organization_id=ORG, user_id=USER, work_date=DAY,
            evaluation_state=EcoDayState.DEFERRED_PENDING_LEDGER.value,
            evaluation_generation=1, shortfall_computed=False,
            contributes_to_accumulator_minutes=0, ledger_version=1,
            ledger_state=LedgerState.SEALED.value, has_unverified_exit=False,
            integrity_fault_reason=None, supersedes_record_id=None,
            superseded_by_record_id=None,
            evaluation_deferral_reason=reason)

    def test_departure_deferral_uses_the_existing_body(self):
        from app.early_clock_out import OWNER_AUTHORIZED_COPY
        from app.eco_day_view.rendering import render_eco_day

        out = render_eco_day(self._view(None))
        assert out.status == "Deferred"
        assert out.body == OWNER_AUTHORIZED_COPY["eco.deferred.body"]
        assert "eco.deferred.body" in out.copy_keys

    def test_classification_deferral_uses_the_new_body(self):
        from app.early_clock_out import OWNER_AUTHORIZED_COPY
        from app.eco_day_view.rendering import render_eco_day

        out = render_eco_day(self._view("CLASSIFICATION_UNRESOLVED"))
        assert out.status == "Deferred"       # the SAME §28.1 label
        assert out.body == OWNER_AUTHORIZED_COPY[
            "eco.classification_deferred.body"]
        assert "eco.classification_deferred.body" in out.copy_keys

    def test_the_two_bodies_are_never_substituted(self):
        from app.early_clock_out import OWNER_AUTHORIZED_COPY
        from app.eco_day_view.rendering import render_eco_day

        departure = render_eco_day(self._view(None)).body
        classification = render_eco_day(
            self._view("CLASSIFICATION_UNRESOLVED")).body
        assert departure != classification
        assert "exit" in departure and "exit" not in classification
        assert ("classification" in classification
                and "classification" not in departure)
        assert set(OWNER_AUTHORIZED_COPY.values()) == {departure,
                                                       classification}

    def test_an_unknown_reason_fails_closed(self):
        """A reason nobody governs must not select production copy."""
        from app.eco_day_view.rendering import render_eco_day

        out = render_eco_day(self._view("SOMETHING_ELSE"))
        assert out.status is None and out.body is None
        assert out.copy_keys == ()

    def test_the_administrator_disclaimer_is_retained(self):
        from app.early_clock_out import LOCKED_COPY
        from app.eco_day_view.rendering import (
            ADMIN_DISCLAIMER_KEY, Audience, render_eco_day)

        out = render_eco_day(self._view("CLASSIFICATION_UNRESOLVED"),
                             audience=Audience.ADMINISTRATOR)
        assert LOCKED_COPY[ADMIN_DISCLAIMER_KEY] in out.body
        assert ADMIN_DISCLAIMER_KEY in out.copy_keys

    @pytest.mark.parametrize("reason", [None, "CLASSIFICATION_UNRESOLVED"])
    def test_both_bodies_pass_the_prohibited_terminology_scanner(self, reason):
        from app.early_clock_out import find_prohibited_terms
        from app.eco_day_view.rendering import render_eco_day

        body = render_eco_day(self._view(reason)).body
        assert find_prohibited_terms(body) == []


# ══════════════════════════════════════════════════════════════════════════════
# 7. Scope held
# ══════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    def test_gv02_and_gv03_are_promoted_and_normatively_unmodified(self):
        """Promoted 2026-09-21 once P-2 was implemented end to end.

        This guard asserted the opposite while the foundation existed but the
        CALCULATION did not — the risk then was promoting on the strength of a
        primitive rather than an answer. That risk is gone; the claim inverts.

        What must NOT have changed is the normative content, so the expected
        fields are still pinned character for character below.
        """
        data = json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))
        by = {v["id"]: v for v in data["vectors"]}
        for gid in ("GV-02", "GV-03"):
            assert by[gid]["harness"]["execution"] == "executable", gid
            assert by[gid]["harness"]["blocking_dependencies"] == [], gid
        assert by["GV-02"]["expected_fields"] == {
            "qualifying_minutes": 500, "attributable_minutes": 25,
            "rest_qualifying_cap_minutes": 30, "rest_cap_is_cumulative": True}
        assert by["GV-03"]["expected_fields"] == {
            "must_fail": True, "asserts_invariant": "INV-4"}
        assert sum(1 for v in data["vectors"]
                   if v["harness"]["execution"] == "executable") == 28

    def test_no_netting_horizon_variance_or_delivery_was_implemented(self):
        """This increment is the classification foundation and nothing else."""
        for pkg in ("break_classification", "break_treatment"):
            for path in sorted((BACKEND / "app" / pkg).glob("*.py")):
                code = "\n".join(
                    line for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.lstrip().startswith("#"))
                for banned in ("netting_mode", "bank_cap", "horizon",
                               "schedule_variance", "delivery_attempt"):
                    assert banned not in code, f"{pkg}/{path.name}: {banned}"

    def test_eco_v1_stays_disabled_and_rollout_is_untouched(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
