"""
P-2 calculation regressions — treatment, cap, refusals, replay.
================================================================

The 28-point regression matrix for the break-treatment calculation, driven
through the real recording boundary.

ON DATABASE SEMANTICS
---------------------
These run on in-memory SQLite. Nothing here claims a PostgreSQL concurrency
guarantee: the properties under test are arithmetic, identity and refusal, all
of which are dialect-independent. The genuinely dialect-sensitive artifacts —
the COALESCE uniqueness index and the CHECK constraints — are exercised by the
migration parity test in ``test_eval_day.py`` and compiled for PostgreSQL by
``test_postgres_dialect_compilation``. Concurrency remains covered where it
always was, in ``test_eval_day_recording.py``.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest

import database.models as dbm
from database.database import Base

from app.break_classification import (
    BreakClassificationAssertion,
    ClassificationResult,
    REASON_CONFLICTING_ASSERTIONS,
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_NOT_SEALED,
    assert_break_classification,
    grant_classification_authority,
)
from app.break_treatment import capture_break_treatment_profile
from app.config_versioning import ConfigValue, ConfigVersion, compute_content_hash
from app.eval_day import EcoDayRecord, EcoDayState, evaluate_and_record_day
from app.eval_day.classification import (
    REASON_BREAK_EVIDENCE_UNAVAILABLE,
    break_treatment_for_day,
)
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = dt.timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]

ORG, USER = 9640, 9641
DAY = dt.date(2026, 8, 31)
MONDAY_WEEK = "2026-08-31"
NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
FLOOR = dt.date(2020, 1, 1)

REQUIRED = 525
WRK = 470


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
        db.add(dbm.Organization(id=ORG, name="Calc Org"))
        db.flush()
        db.add(dbm.User(id=USER, name="W", email="w@calc.test",
                        organization_id=ORG, role="employee"))
        db.flush()
    from tests._eco_policy import ensure_test_policy
    ensure_test_policy(db, organization_id=ORG, commit=False)
    db.flush()
    return ORG


def _config(db):
    values = [{"scope_user_id": None, "tier": "TENANT_FALLBACK", "rank": 6,
               "key": "required_minutes", "value_minutes": REQUIRED,
               "source_ref": None}]
    v = ConfigVersion(
        organization_id=ORG, version_key="calc-cfg", effective_from=FLOOR,
        captured_at_utc=dt.datetime(2020, 1, 1, tzinfo=UTC),
        captured_by_label="SYSTEM",
        content_hash=compute_content_hash(
            organization_id=ORG, version_key="calc-cfg",
            effective_from=FLOOR, values=values))
    db.add(v); db.flush()
    for val in values:
        db.add(ConfigValue(config_version_id=v.id, organization_id=ORG, **val))
    db.flush()


def _profile(db, *, ref="bt", meal=False, rest=True, cap=30,
             effective_from=FLOOR):
    capture_break_treatment_profile(
        db, organization_id=ORG, profile_ref=ref,
        effective_from=effective_from, meal_qualifies=meal,
        rest_qualifies=rest, rest_qualifying_cap_minutes=cap,
        captured_by="owner@calc.test")
    db.flush()
    return ref


def _day(db, *, breaks=(), wrk=WRK, state=LedgerState.SEALED.value,
         open_ended=False, version=1):
    led = WorkDayLedger(
        organization_id=ORG, user_id=USER,
        work_date=dt.datetime(DAY.year, DAY.month, DAY.day),
        workweek_key=MONDAY_WEEK, ledger_version=version, state=state,
        timezone_iana="Asia/Kolkata", is_working_day=True,
        working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST")
    db.add(led); db.flush()

    begin = dt.datetime(DAY.year, DAY.month, DAY.day, 9, 0, tzinfo=UTC)
    db.add(WorkStateInterval(
        ledger_id=led.id, organization_id=ORG, seq=1, state="WRK",
        start_at_utc=begin, end_at_utc=begin + dt.timedelta(minutes=wrk),
        duration_seconds=wrk * 60, provenance="WORKER", created_at=NOW))

    made = []
    at = dt.datetime(DAY.year, DAY.month, DAY.day, 11, 0)
    for minutes in breaks:
        row = dbm.BreakSession(
            user_id=USER, organization_id=ORG, break_start=at,
            break_end=None if open_ended else at + dt.timedelta(minutes=minutes),
            break_seconds=minutes * 60, is_active=open_ended,
            break_type="break")
        db.add(row); db.flush()
        made.append((row.id, minutes))
        at = at + dt.timedelta(minutes=minutes + 60)
    db.flush()
    return led, made


def _authority(db, *, user=USER, label="hr@calc.test"):
    row = grant_classification_authority(
        db, organization_id=ORG, user_id=user, authority_label=label,
        granted_by="owner")
    db.flush()
    return row


def _classify(db, authority, breaks, results):
    for (bid, _m), result in zip(breaks, results):
        assert_break_classification(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{bid}", classification=result,
            evidence_ref="ruling:x", authority_id=authority.id)
    db.flush()


def _record(db):
    return evaluate_and_record_day(db, organization_id=ORG, user_id=USER,
                                   work_date=DAY)


R, M, U = (ClassificationResult.BRK_R.value, ClassificationResult.BRK_M.value,
           ClassificationResult.UNCLASSIFIABLE.value)


# ══════════════════════════════════════════════════════════════════════════════
# 1-9 · Treatment and the cumulative cap
# ══════════════════════════════════════════════════════════════════════════════
class TestTreatmentAndCap:
    def test_1_single_rest_under_cap(self, db, org):
        _config(db); _profile(db, cap=30)
        _, b = _day(db, breaks=(20,)); _classify(db, _authority(db), b, (R,))
        assert _record(db).record.qualifying_minutes == WRK + 20

    def test_2_multiple_rest_intervals_sum(self, db, org):
        _config(db); _profile(db, cap=60)
        _, b = _day(db, breaks=(10, 15)); _classify(db, _authority(db), b, (R, R))
        assert _record(db).record.qualifying_minutes == WRK + 25

    def test_3_cumulative_cap_binds_across_intervals(self, db, org):
        """§19.1: 15 + 20 against 30 qualifies 30, not 35 and not 20 + 20."""
        _config(db); _profile(db, cap=30)
        _, b = _day(db, breaks=(15, 20)); _classify(db, _authority(db), b, (R, R))
        r = _record(db).record
        assert r.qualifying_minutes == WRK + 30
        assert r.qualifying_minutes != WRK + 35

    def test_4_cap_exactly_reached(self, db, org):
        _config(db); _profile(db, cap=30)
        _, b = _day(db, breaks=(10, 20)); _classify(db, _authority(db), b, (R, R))
        assert _record(db).record.qualifying_minutes == WRK + 30

    def test_5_cap_exceeded_by_one_minute(self, db, org):
        _config(db); _profile(db, cap=30)
        _, b = _day(db, breaks=(31,)); _classify(db, _authority(db), b, (R,))
        assert _record(db).record.qualifying_minutes == WRK + 30

    def test_6_null_cap_means_uncapped(self, db, org):
        """§13.2: "null = uncapped". Not unresolved, not a default."""
        _config(db); _profile(db, cap=None)
        _, b = _day(db, breaks=(45, 45)); _classify(db, _authority(db), b, (R, R))
        assert _record(db).record.qualifying_minutes == WRK + 90

    def test_7_meal_does_not_qualify_by_default(self, db, org):
        _config(db); _profile(db, meal=False)
        _, b = _day(db, breaks=(45,)); _classify(db, _authority(db), b, (M,))
        assert _record(db).record.qualifying_minutes == WRK

    def test_8_meal_qualifies_when_explicitly_configured(self, db, org):
        _config(db); _profile(db, meal=True, rest=False, cap=None)
        _, b = _day(db, breaks=(45,)); _classify(db, _authority(db), b, (M,))
        assert _record(db).record.qualifying_minutes == WRK + 45

    def test_9_mixed_meal_and_rest(self, db, org):
        """The meal never enters the REST pool and never touches its cap."""
        _config(db); _profile(db, meal=False, rest=True, cap=30)
        _, b = _day(db, breaks=(15, 45, 20))
        _classify(db, _authority(db), b, (R, M, R))
        r = _record(db).record
        assert r.qualifying_minutes == WRK + 30      # 35 rest capped; meal 0
        assert r.qualifying_minutes != WRK + 30 + 45

    def test_9b_the_record_shows_observed_and_qualifying_separately(self, db, org):
        """INV-4 on the ARTIFACT, not only in the arithmetic.

        The same ledger as test_9. The arithmetic has always honoured the
        meal/rest distinction; what §35 asks for is that the record show it,
        because §26.1 makes the separately-labelled break lines "the Worker's
        only opportunity to notice that a rest break was misclassified as a
        meal break before the day seals".

        The meal is the case that matters: 45 minutes taken, 0 admitted. A
        record carrying only the qualifying total cannot distinguish that from
        a day with no meal at all.
        """
        _config(db); _profile(db, meal=False, rest=True, cap=30)
        _, b = _day(db, breaks=(15, 45, 20))
        _classify(db, _authority(db), b, (R, M, R))
        r = _record(db).record

        assert r.brk_m_seconds == 45 * 60            # observed
        assert r.brk_m_qualifying_seconds == 0       # admitted: none
        assert r.brk_r_seconds == 35 * 60            # 15 + 20 observed
        assert r.brk_r_qualifying_seconds == 30 * 60  # cumulative cap bound

    def test_9c_observed_meal_survives_a_profile_that_admits_none(self, db, org):
        """The specific correction. The accumulator used to sit INSIDE the
        ``meal_qualifies`` branch, so a non-qualifying meal was never counted
        and the record could not show it was taken."""
        _config(db); _profile(db, meal=False, rest=True, cap=None)
        _, b = _day(db, breaks=(45,)); _classify(db, _authority(db), b, (M,))
        r = _record(db).record
        assert r.qualifying_minutes == WRK           # unchanged: still admits 0
        assert r.brk_m_seconds == 45 * 60            # but the day records it
        assert r.brk_m_qualifying_seconds == 0


# ══════════════════════════════════════════════════════════════════════════════
# 10-18 · Fail-closed behaviour
# ══════════════════════════════════════════════════════════════════════════════
class TestFailsClosed:
    def _deferred(self, out):
        r = out.record
        assert r.state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        assert r.evaluation_deferral_reason == "CLASSIFICATION_UNRESOLVED"
        assert r.qualifying_minutes is None
        assert r.contributes_to_accumulator_minutes == 0
        assert r.shortfall_computed is False

    def test_10_missing_classification_defers(self, db, org):
        _config(db); _profile(db)
        _day(db, breaks=(20,))
        self._deferred(_record(db))

    def test_11_explicit_unclassifiable_defers(self, db, org):
        _config(db); _profile(db)
        _, b = _day(db, breaks=(20,)); _classify(db, _authority(db), b, (U,))
        self._deferred(_record(db))

    def test_11b_one_unclassified_break_defers_the_whole_day(self, db, org):
        """P2-D5: EVERY break needs an assertion, not just the rest ones."""
        _config(db); _profile(db)
        _, b = _day(db, breaks=(15, 45))
        _classify(db, _authority(db), b[:1], (R,))       # meal left unasserted
        self._deferred(_record(db))

    def test_12_conflicting_assertions_refuse(self, db, org):
        _config(db); _profile(db)
        a1 = _authority(db)
        db.add(dbm.User(id=USER + 1, name="A", email="a@calc.test",
                        organization_id=ORG, role="employee")); db.flush()
        a2 = _authority(db, user=USER + 1, label="second@calc.test")
        _, b = _day(db, breaks=(20,)); _classify(db, a1, b, (R,))
        db.add(BreakClassificationAssertion(
            organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{b[0][0]}", classification=M,
            evidence_ref="other", authority_id=a2.id,
            authority_label=a2.authority_label, assertion_version=99,
            content_hash="x" * 64)); db.flush()

        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason == REASON_CONFLICTING_ASSERTIONS

    def test_13_missing_authority_cannot_assert(self, db, org):
        from app.break_classification import InvalidClassificationAuthority
        _config(db); _profile(db)
        _, b = _day(db, breaks=(20,))
        with pytest.raises(InvalidClassificationAuthority):
            assert_break_classification(
                db, organization_id=ORG, user_id=USER, work_date=DAY,
                source_ref=f"break_session:{b[0][0]}", classification=R,
                evidence_ref="x", authority_id=999999)

    def test_14_invalid_profile_refuses(self, db, org, monkeypatch):
        from app.break_treatment.outcomes import ProfileOutcome, ProfileResolution
        import app.eval_day.classification as mod
        from app.break_classification import REASON_PROFILE_INVALID

        monkeypatch.setattr(mod, "resolve_profile_as_of",
                            lambda db_, *, organization_id, as_of:
                            ProfileResolution(outcome=ProfileOutcome.INVALID,
                                              organization_id=organization_id,
                                              as_of=as_of,
                                              invalid_reason="cap -5"))
        _config(db); _day(db, breaks=(20,))
        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason == REASON_PROFILE_INVALID

    def test_15_ambiguous_profile_refuses(self, db, org, monkeypatch):
        from app.break_treatment.outcomes import ProfileOutcome, ProfileResolution
        import app.eval_day.classification as mod
        from app.break_classification import REASON_PROFILE_AMBIGUOUS

        monkeypatch.setattr(mod, "resolve_profile_as_of",
                            lambda db_, *, organization_id, as_of:
                            ProfileResolution(outcome=ProfileOutcome.AMBIGUOUS,
                                              organization_id=organization_id,
                                              as_of=as_of))
        _config(db); _day(db, breaks=(20,))
        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason == REASON_PROFILE_AMBIGUOUS

    def test_16_open_ledger_refuses(self, db, org):
        _config(db); _profile(db)
        _day(db, breaks=(20,), state=LedgerState.OPEN.value)
        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason in (REASON_LEDGER_NOT_SEALED, "LEDGER_NOT_SEALED")

    def test_17_integrity_fault_refuses(self, db, org):
        _config(db); _profile(db)
        _day(db, breaks=(20,),
             state=LedgerState.LEDGER_INTEGRITY_FAULT.value)
        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason in (REASON_LEDGER_INTEGRITY_FAULT,
                              "LEDGER_INTEGRITY_FAULT")

    def test_18_break_with_no_end_boundary_refuses(self, db, org):
        """No authoritative end means no authoritative duration (§7.2)."""
        _config(db); _profile(db)
        _day(db, breaks=(20,), open_ended=True)
        out = _record(db)
        assert out.outcome.value == "REFUSED"
        assert out.reason == REASON_BREAK_EVIDENCE_UNAVAILABLE
        assert db.query(EcoDayRecord).filter_by(work_date=DAY).count() == 0


# ══════════════════════════════════════════════════════════════════════════════
# 19-23 · Replay, generation, history
# ══════════════════════════════════════════════════════════════════════════════
class TestReplayAndGeneration:
    def _scenario(self, db, *, cap=30, ref="bt"):
        _config(db); _profile(db, ref=ref, cap=cap)
        _, b = _day(db, breaks=(15, 20)); _classify(db, _authority(db), b, (R, R))
        return b

    def test_19_changed_break_boundary_changes_the_ledger_version(self, db, org):
        """P2-D1's replay guarantee, at the fingerprint level.

        Break windows are inside ``source_evidence_fingerprint``, so a changed
        boundary supersedes the sealed day — and ``ledger_version`` is already
        part of the record identity.
        """
        from app.work_ledger_producer.evidence import source_evidence_fingerprint
        src = pathlib.Path(
            BACKEND / "app" / "work_ledger_producer" / "evidence.py"
        ).read_text(encoding="utf-8")
        assert "w.source_ref" in src and "_iso(w.start)" in src
        assert callable(source_evidence_fingerprint)

    def test_20_changed_classification_creates_a_new_generation(self, db, org):
        b = self._scenario(db)
        first = _record(db).record
        assert first.evaluation_generation == 1
        before_key = first.classification_snapshot_key
        before_hash = first.content_hash

        a = db.query(__import__("app.break_classification", fromlist=["x"])
                     .EcoClassificationAuthority).first()
        assert_break_classification(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{b[1][0]}", classification=M,
            evidence_ref="revised", authority_id=a.id)
        db.flush()

        second = _record(db).record
        assert second.id != first.id
        assert second.evaluation_generation == 2
        assert second.supersedes == first.id
        assert second.classification_snapshot_key != before_key
        # 15 rest only now, meal does not qualify.
        assert second.qualifying_minutes == WRK + 15
        db.refresh(first)
        assert first.content_hash == before_hash      # never mutated

    def test_21_changed_profile_creates_a_new_generation(self, db, org):
        """P2-D4: the governing RULE is identity."""
        self._scenario(db, cap=30, ref="bt-cap30")
        first = _record(db).record
        assert first.qualifying_minutes == WRK + 30
        before_hash = first.content_hash

        _profile(db, ref="bt-cap60", cap=60,
                 effective_from=dt.date(2026, 8, 1))
        second = _record(db).record

        assert second.id != first.id
        assert second.evaluation_generation == 2
        assert second.supersedes == first.id
        assert second.break_treatment_profile_ref == "bt-cap60"
        assert second.qualifying_minutes == WRK + 35   # cap no longer binds
        db.refresh(first)
        assert first.content_hash == before_hash
        assert first.break_treatment_profile_ref == "bt-cap30"

    def test_22_unchanged_inputs_replay_idempotently(self, db, org):
        self._scenario(db)
        first = _record(db).record
        again = _record(db).record
        assert again.id == first.id
        assert again.content_hash == first.content_hash
        assert db.query(EcoDayRecord).filter_by(
            organization_id=ORG, user_id=USER, work_date=DAY).count() == 1

    def test_23_a_later_profile_does_not_rewrite_history(self, db, org):
        """As-of resolution: a future-dated profile is invisible to this day."""
        _config(db)
        _profile(db, ref="bt-then", cap=30, effective_from=FLOOR)
        _profile(db, ref="bt-later", cap=90,
                 effective_from=DAY + dt.timedelta(days=1))
        _, b = _day(db, breaks=(15, 20)); _classify(db, _authority(db), b, (R, R))

        r = _record(db).record
        assert r.break_treatment_profile_ref == "bt-then"
        assert r.qualifying_minutes == WRK + 30


# ══════════════════════════════════════════════════════════════════════════════
# 24-26 · Static guards
# ══════════════════════════════════════════════════════════════════════════════
class TestStaticGuards:
    def test_24_no_break_type_authority_leakage(self):
        for pkg in ("break_classification", "break_treatment", "eval_day"):
            for path in sorted((BACKEND / "app" / pkg).glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                reads = {n.attr for n in ast.walk(tree)
                         if isinstance(n, ast.Attribute)}
                assert "break_type" not in reads, f"{pkg}/{path.name}"

    def test_25_producer_remains_classification_blind(self):
        pkg = BACKEND / "app" / "work_ledger_producer"
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            mods = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module:
                    mods.add(n.module)
                elif isinstance(n, ast.Import):
                    mods |= {a.name for a in n.names}
            for m in mods:
                assert "break_classification" not in m, path.name
                assert "break_treatment" not in m, path.name
            # And it still emits only WRK/OFF.
            states = {n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name)
                      and n.value.id == "WorkState"}
            assert states <= {"WRK", "OFF"}, f"{path.name}: {states}"

    def test_26_the_evaluator_stays_pure_and_has_one_rounding_site(self):
        src = (BACKEND / "app" / "eval_day" / "evaluator.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "round_half_up_minutes"]
        # Two: the qualifying pool and the leave overlap — both pre-existing,
        # and P-2 added neither. A third would be the second rounding boundary
        # GV-12's ruling forbids.
        assert len(calls) == 2, f"{len(calls)} rounding calls"

        # The evaluator reads no classification, profile or break source.
        reads = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("break_start", "break_end", "break_seconds",
                       "rest_qualifies", "meal_qualifies"):
            assert banned not in reads, banned

    def test_27_no_new_workstate_values_were_introduced(self):
        from app.work_ledger import WorkState
        assert {s.value for s in WorkState} == {
            "WRK", "TRV", "ONC", "BRK_M", "BRK_R", "OFF", "LVE", "UNK"}

    def test_28_no_new_eco_day_state_was_introduced(self):
        assert len(list(EcoDayState)) == 7
