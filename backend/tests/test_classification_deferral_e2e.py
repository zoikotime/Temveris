"""
Classification deferral, end to end (D-1, C-1, P-2, P-4 gate).
==============================================================

Drives the REAL recording boundary — profile capture -> sealed ledger with a
break -> ``evaluate_and_record_day`` -> a persisted ``EcoDayRecord`` -> the week
fold. Nothing is stubbed.

THE OPT-IN GATE
---------------
A day requires classification only when a ``BreakTreatmentProfile`` governs the
work date. That follows from P-4, which gates rest qualification on "the
resolved break-treatment profile": with no profile in force there is no rule by
which a classification could change any arithmetic. These tests pin both
halves — a Tenant with no profile is completely unaffected, and a Tenant with
one defers until an authority has spoken.
"""

from __future__ import annotations

import datetime as dt

import pytest

import database.models as dbm
from database.database import Base

from app.break_classification import (
    ClassificationResult,
    assert_break_classification,
    grant_classification_authority,
)
from app.break_treatment import capture_break_treatment_profile
from app.config_versioning import ConfigValue, ConfigVersion, compute_content_hash
from app.eval_day import EcoDayRecord, EcoDayState, evaluate_and_record_day
from app.eval_week.fold import REASON_DEFERRED_DAY, fold_week
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = dt.timezone.utc
ORG, USER = 9420, 9421
DAY = dt.date(2026, 8, 31)
MONDAY_WEEK = "2026-08-31"
NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
FLOOR = dt.date(2020, 1, 1)
REQUIRED_MINUTES = 480


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
    if db.get(dbm.Organization, ORG) is None:
        db.add(dbm.Organization(id=ORG, name="E2E Org"))
        db.flush()
        db.add(dbm.User(id=USER, name="W", email="w@e2e.test",
                        organization_id=ORG, role="employee"))
        db.flush()
    from tests._eco_policy import ensure_test_policy
    ensure_test_policy(db, organization_id=ORG, commit=False)
    db.flush()
    return ORG


def _config(db):
    values = [{"scope_user_id": None, "tier": "TENANT_FALLBACK", "rank": 6,
               "key": "required_minutes", "value_minutes": REQUIRED_MINUTES,
               "source_ref": None}]
    key = "e2e-cfg-v1"
    version = ConfigVersion(
        organization_id=ORG, version_key=key, effective_from=FLOOR,
        captured_at_utc=dt.datetime(2020, 1, 1, tzinfo=UTC),
        captured_by_label="SYSTEM",
        content_hash=compute_content_hash(
            organization_id=ORG, version_key=key, effective_from=FLOOR,
            values=values))
    db.add(version)
    db.flush()
    for v in values:
        db.add(ConfigValue(config_version_id=version.id,
                           organization_id=ORG, **v))
    db.flush()


def _sealed_day_with_break(db, *, gap_minutes=30, with_break=True):
    """A sealed WRK day. The break is real evidence the producer would carve."""
    led = WorkDayLedger(
        organization_id=ORG, user_id=USER,
        work_date=dt.datetime(DAY.year, DAY.month, DAY.day),
        workweek_key=MONDAY_WEEK, ledger_version=1,
        state=LedgerState.SEALED.value, timezone_iana="Asia/Kolkata",
        is_working_day=True, working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST")
    db.add(led)
    db.flush()

    seconds = (REQUIRED_MINUTES - gap_minutes) * 60
    begin = dt.datetime(DAY.year, DAY.month, DAY.day, 9, 0, tzinfo=UTC)
    db.add(WorkStateInterval(
        ledger_id=led.id, organization_id=ORG, seq=1, state="WRK",
        start_at_utc=begin, end_at_utc=begin + dt.timedelta(seconds=seconds),
        duration_seconds=seconds, provenance="WORKER", created_at=NOW))

    brk = None
    if with_break:
        brk = dbm.BreakSession(
            user_id=USER, organization_id=ORG,
            break_start=dt.datetime(DAY.year, DAY.month, DAY.day, 13, 0),
            break_end=dt.datetime(DAY.year, DAY.month, DAY.day, 13, 30),
            break_seconds=1800, is_active=False, break_type="lunch")
        db.add(brk)
    db.flush()
    return led, brk


def _profile(db, *, cap=30):
    capture_break_treatment_profile(
        db, organization_id=ORG, profile_ref="e2e-bt-v1",
        effective_from=FLOOR, meal_qualifies=False, rest_qualifies=True,
        rest_qualifying_cap_minutes=cap, captured_by="owner@e2e.test")
    db.flush()


def _record(db):
    out = evaluate_and_record_day(db, organization_id=ORG, user_id=USER,
                                  work_date=DAY)
    return out


class TestClassificationDeferralEndToEnd:
    def test_without_a_profile_the_day_is_unaffected(self, db, org):
        """The opt-in gate. A Tenant that captured no profile sees no change."""
        _config(db)
        _sealed_day_with_break(db)
        out = _record(db)
        assert out.is_recorded
        assert out.record.state == EcoDayState.SHORTFALL.value
        assert out.record.evaluation_deferral_reason is None
        assert out.record.classification_snapshot_key is None
        assert out.record.attributable_minutes == 30

    def test_a_profile_and_an_unclassified_break_defers(self, db, org):
        """P-1: no fallback to the worker label, so the day defers (D-1)."""
        _config(db)
        _profile(db)
        _sealed_day_with_break(db)
        out = _record(db)

        assert out.is_recorded
        r = out.record
        assert r.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert r.evaluation_deferral_reason == "CLASSIFICATION_UNRESOLVED"
        assert r.classification_snapshot_key is not None
        assert r.contributes_to_accumulator_minutes == 0
        assert r.shortfall_computed is False
        for field in ("required_minutes", "qualifying_minutes",
                      "gross_shortfall_minutes", "exclusion_minutes",
                      "attributable_minutes"):
            assert getattr(r, field) is None, field

    def test_a_profile_with_no_break_on_the_day_evaluates_normally(
            self, db, org):
        _config(db)
        _profile(db)
        _sealed_day_with_break(db, with_break=False)
        out = _record(db)
        assert out.record.state == EcoDayState.SHORTFALL.value
        assert out.record.evaluation_deferral_reason is None
        assert out.record.classification_snapshot_key is None

    def test_an_unclassifiable_assertion_defers(self, db, org):
        _config(db)
        _profile(db)
        _, brk = _sealed_day_with_break(db)
        a = grant_classification_authority(
            db, organization_id=ORG, user_id=USER,
            authority_label="hr@e2e.test", granted_by="owner")
        db.flush()
        assert_break_classification(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{brk.id}",
            classification=ClassificationResult.UNCLASSIFIABLE.value,
            evidence_ref="reviewed:inconclusive", authority_id=a.id)
        db.flush()

        r = _record(db).record
        assert r.evaluation_deferral_reason == "CLASSIFICATION_UNRESOLVED"

    def test_resolution_creates_a_new_generation_and_never_mutates(
            self, db, org):
        """D-1: "resolution creates a new immutable evaluation generation"."""
        _config(db)
        _profile(db)
        _, brk = _sealed_day_with_break(db)
        deferred = _record(db).record
        assert deferred.evaluation_generation == 1
        deferred_key = deferred.classification_snapshot_key
        deferred_hash = deferred.content_hash

        a = grant_classification_authority(
            db, organization_id=ORG, user_id=USER,
            authority_label="hr@e2e.test", granted_by="owner")
        db.flush()
        assert_break_classification(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{brk.id}",
            classification=ClassificationResult.BRK_M.value,
            evidence_ref="ruling:clause-7", authority_id=a.id)
        db.flush()

        resolved = _record(db).record
        assert resolved.id != deferred.id
        assert resolved.evaluation_generation == 2
        assert resolved.supersedes == deferred.id
        assert resolved.classification_snapshot_key != deferred_key
        assert resolved.state == EcoDayState.SHORTFALL.value
        assert resolved.evaluation_deferral_reason is None

        # The prior generation is untouched, byte for byte.
        db.refresh(deferred)
        assert deferred.content_hash == deferred_hash
        assert deferred.evaluation_deferral_reason == "CLASSIFICATION_UNRESOLVED"
        assert deferred.classification_snapshot_key == deferred_key
        assert deferred.superseded_by == resolved.id

    def test_an_unchanged_classification_replays_idempotently(self, db, org):
        _config(db)
        _profile(db)
        _, brk = _sealed_day_with_break(db)
        a = grant_classification_authority(
            db, organization_id=ORG, user_id=USER,
            authority_label="hr@e2e.test", granted_by="owner")
        db.flush()
        assert_break_classification(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{brk.id}",
            classification=ClassificationResult.BRK_M.value,
            evidence_ref="ruling:clause-7", authority_id=a.id)
        db.flush()

        first = _record(db).record
        again = _record(db).record
        assert again.id == first.id
        assert again.content_hash == first.content_hash
        assert db.query(EcoDayRecord).filter_by(
            organization_id=ORG, user_id=USER, work_date=DAY).count() == 1

    def test_no_threshold_event_or_notification_is_created(self, db, org):
        """A deferred day emits nothing downstream."""
        from app.alert.models import EcoNotification
        from app.eval_event.models import EcoThresholdEvent

        _config(db)
        _profile(db)
        _sealed_day_with_break(db)
        r = _record(db).record
        assert r.evaluation_deferral_reason == "CLASSIFICATION_UNRESOLVED"
        assert db.query(EcoThresholdEvent).count() == 0
        assert db.query(EcoNotification).count() == 0


class TestProfileResolutionFailsClosed:
    """The opt-in gate distinguishes ABSENCE from FAILURE.

    Owner ruling: only ``NO_VERSION`` means "no applicable profile, therefore
    classification is not required". ``AMBIGUOUS`` and ``INVALID`` are
    resolution FAILURES and must fail closed rather than be read as an absent
    profile.

    WHY THESE TESTS SUBSTITUTE THE RESOLVER
    ---------------------------------------
    Both failures are currently unreachable through the schema — the unique
    constraint on ``(organization_id, effective_from)`` blocks AMBIGUOUS even
    against a raw INSERT, and the NOT NULL flags plus the ``cap >= 0`` CHECK
    block INVALID. That is a good thing and must NOT be weakened to make a test
    reachable. So the resolver is substituted at the call site: the contract
    under test is what ``classification_for_day`` DOES with each outcome, and
    substituting the narrowest possible seam tests exactly that without
    touching a production constraint.
    """

    def _force(self, monkeypatch, outcome, *, invalid_reason=None):
        from app.break_treatment.outcomes import ProfileResolution
        import app.eval_day.classification as mod

        def _fake(db, *, organization_id, as_of):
            return ProfileResolution(
                outcome=outcome, organization_id=organization_id,
                as_of=as_of, invalid_reason=invalid_reason)

        monkeypatch.setattr(mod, "resolve_profile_as_of", _fake)

    def test_no_version_remains_non_blocking(self, db, org, monkeypatch):
        """The unchanged half: absence still means "not required"."""
        from app.break_treatment.outcomes import ProfileOutcome
        from app.eval_day.classification import classification_for_day

        self._force(monkeypatch, ProfileOutcome.NO_VERSION)
        _config(db)
        led, _ = _sealed_day_with_break(db)

        assert classification_for_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            ledger=led) is None

        r = _record(db).record
        assert r.state == EcoDayState.SHORTFALL.value
        assert r.evaluation_deferral_reason is None
        assert r.classification_snapshot_key is None

    def test_an_ambiguous_profile_refuses_the_day(self, db, org, monkeypatch):
        from app.break_classification import REASON_PROFILE_AMBIGUOUS
        from app.break_treatment.outcomes import ProfileOutcome
        from app.eval_day.classification import classification_for_day

        self._force(monkeypatch, ProfileOutcome.AMBIGUOUS,
                    invalid_reason="two profiles effective 2020-01-01")
        _config(db)
        led, _ = _sealed_day_with_break(db)

        gate = classification_for_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY, ledger=led)
        assert gate is not None, "an ambiguous profile must not read as absent"
        assert gate.is_refused
        assert gate.reason == REASON_PROFILE_AMBIGUOUS
        assert "AMBIGUOUS" in gate.detail
        # NOT a classification deferral: nothing was examined.
        assert not gate.defers
        assert gate.snapshot_key is None

        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason == REASON_PROFILE_AMBIGUOUS
        assert db.query(EcoDayRecord).filter_by(
            organization_id=ORG, user_id=USER, work_date=DAY).count() == 0

    def test_an_invalid_profile_refuses_the_day(self, db, org, monkeypatch):
        from app.break_classification import REASON_PROFILE_INVALID
        from app.break_treatment.outcomes import ProfileOutcome
        from app.eval_day.classification import classification_for_day

        self._force(monkeypatch, ProfileOutcome.INVALID,
                    invalid_reason="rest_qualifying_cap_minutes -5")
        _config(db)
        led, _ = _sealed_day_with_break(db)

        gate = classification_for_day(
            db, organization_id=ORG, user_id=USER, work_date=DAY, ledger=led)
        assert gate is not None
        assert gate.is_refused
        assert gate.reason == REASON_PROFILE_INVALID
        # The resolver's own diagnosis is carried through, not re-worded.
        assert "rest_qualifying_cap_minutes -5" in gate.detail
        assert not gate.defers

        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason == REASON_PROFILE_INVALID
        assert db.query(EcoDayRecord).filter_by(
            organization_id=ORG, user_id=USER, work_date=DAY).count() == 0

    def test_a_failure_never_becomes_classification_unresolved(
            self, db, org, monkeypatch):
        """The distinction the ruling is most explicit about."""
        from app.break_treatment.outcomes import ProfileOutcome
        from app.eval_day.classification import classification_for_day

        for outcome in (ProfileOutcome.AMBIGUOUS, ProfileOutcome.INVALID):
            self._force(monkeypatch, outcome, invalid_reason="x")
            led = db.query(WorkDayLedger).filter_by(
                organization_id=ORG, user_id=USER).first()
            if led is None:
                _config(db)
                led, _ = _sealed_day_with_break(db)
            gate = classification_for_day(
                db, organization_id=ORG, user_id=USER, work_date=DAY,
                ledger=led)
            assert gate.reason != "CLASSIFICATION_UNRESOLVED"
            assert "CLASSIFICATION_UNRESOLVED" not in (gate.detail or "")

    def test_the_production_constraints_were_not_weakened(self, db, org):
        """These tests substitute a seam; they do not relax the schema."""
        from app.break_treatment.models import BreakTreatmentProfile

        t = BreakTreatmentProfile.__table__
        names = {c.name for c in t.constraints}
        assert "uq_break_treatment_profile_effective" in names
        assert "ck_break_treatment_profile_cap_nonnegative" in names
        assert t.c.meal_qualifies.nullable is False
        assert t.c.rest_qualifies.nullable is False


class TestEvalWeek:
    def test_a_classification_deferred_day_blocks_the_week(self, db, org):
        _config(db)
        _profile(db)
        _sealed_day_with_break(db)
        r = _record(db).record

        out = fold_week([r])
        assert out.is_complete is False
        assert out.reason == REASON_DEFERRED_DAY
        assert out.blocking_states == ("DEFERRED_PENDING_LEDGER",)

    def test_the_day_level_reason_is_preserved_in_provenance(self, db, org):
        """Ruling 4: same weekly outcome, distinguishable day-level reason."""
        _config(db)
        _profile(db)
        _sealed_day_with_break(db)
        r = _record(db).record

        out = fold_week([r])
        assert out.deferral_reasons == ("CLASSIFICATION_UNRESOLVED",)

    def test_a_departure_deferral_still_reports_no_reason(self, db, org):
        """The existing condition is unchanged: NULL is its reading."""
        from app.eval_day.evaluator import DayEvaluation

        day = DayEvaluation(
            organization_id=ORG, user_id=USER, work_date=DAY, ledger_version=1,
            state=EcoDayState.DEFERRED_PENDING_LEDGER,
            requirement_outcome="NOT_APPLICABLE", shortfall_computed=False,
            contributes_to_accumulator_minutes=0)
        out = fold_week([day])
        assert out.reason == REASON_DEFERRED_DAY
        assert out.deferral_reasons == (None,)

    def test_no_new_weekly_state_was_added(self):
        from app.eval_week.outcomes import WeeklyState
        assert "CLASSIFICATION" not in " ".join(s.value for s in WeeklyState)

    def test_a_resolved_day_lets_the_week_complete(self, db, org):
        _config(db)
        _profile(db)
        _, brk = _sealed_day_with_break(db)
        a = grant_classification_authority(
            db, organization_id=ORG, user_id=USER,
            authority_label="hr@e2e.test", granted_by="owner")
        db.flush()
        assert_break_classification(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{brk.id}",
            classification=ClassificationResult.BRK_M.value,
            evidence_ref="ruling:clause-7", authority_id=a.id)
        db.flush()

        r = _record(db).record
        out = fold_week([r])
        assert out.is_complete is True
        assert out.weekly_total_minutes == 30
        assert out.deferral_reasons == ()
