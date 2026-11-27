"""
The ECO day read contract and visibility surface (GV-17).
=========================================================

Two properties under test:

1. **the surface can tell a §24.6 day from a §24.7 day, from canonical
   persisted evidence alone, without anybody having invented a new name for
   either** — sections 1 to 5, the read contract;
2. **§24.7's "visible to both Worker and administrator" is discharged with
   approved copy quoted verbatim and nothing authored here** — section 6, the
   visibility surface.

Every expected production string in section 6 is imported from the register
rather than retyped. A test that hard-codes approved copy becomes a second,
unreviewed transcription of it; the character-for-character claim belongs to
the anti-drift guard in tests/test_eco_locked_copy.py and lives only there.
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
from app.eco_day_view import EcoDayView, read_eco_day
from app.eco_view_authority import (
    DenialReason,
    EcoViewGrant,
    ViewAuthorityDecision,
)
from app.eval_day import EcoDayState, evaluate_and_record_day
from app.requirement_resolution import RequirementCandidate
from app.schedule_policy import keys as K
from app.work_ledger import LedgerState, Provenance, WorkDayLedger
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]

ORG, ORG_B = 8710, 8720
WORKER, ADMIN, OTHER = 8711, 8712, 8713
WORKER_B, ADMIN_B = 8721, 8722
OWNER = 8700

ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)
DAY = date(2026, 9, 7)
DAY_WEEK = "2026-09-07"
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
ALL_SEVEN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]
REQUIRED_MINUTES = 480


def _local(on: date, hour: int, minute: int = 0) -> datetime:
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=UTC) - IST


@pytest.fixture()
def db():
    import app.clock_session_boundary.models      # noqa: F401
    import app.config_versioning.models           # noqa: F401
    import app.eco_view_authority.models          # noqa: F401
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
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(eng, expire_on_commit=False) as s:
        s.add_all([dbm.Organization(id=ORG, name="Tenant A"),
                   dbm.Organization(id=ORG_B, name="Tenant B")])
        s.commit()
        s.add_all([
            dbm.User(id=WORKER, name="W", email="w@v.test",
                     organization_id=ORG, role="employee"),
            dbm.User(id=ADMIN, name="A", email="a@v.test",
                     organization_id=ORG, role="admin"),
            dbm.User(id=OTHER, name="O", email="o@v.test",
                     organization_id=ORG, role="employee"),
            dbm.User(id=WORKER_B, name="WB", email="wb@v.test",
                     organization_id=ORG_B, role="employee"),
            dbm.User(id=ADMIN_B, name="AB", email="ab@v.test",
                     organization_id=ORG_B, role="admin"),
            dbm.User(id=OWNER, name="Owner", email="owner@v.test",
                     organization_id=None, role="super_admin"),
        ])
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(ALL_SEVEN)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@v.test")
        s.commit()
        # Explicit historical PolicyVersion: evaluate_and_record_day
        # refuses with POLICY_UNRESOLVED without one. Satisfies the
        # production rule rather than weakening it.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


def _session(db, *, clock_in, clock_out, close_prov=Provenance.WORKER,
             actor="w@v.test"):
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=0, break_seconds=0, offline_seconds=0,
        is_active=clock_out is None)
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=WORKER,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=Provenance.WORKER, actor_label=actor)
    if clock_out is not None:
        append_boundary_event(db, session_id=row.id, user_id=WORKER,
                              boundary=BOUNDARY_CLOCK_OUT,
                              boundary_at=clock_out, provenance=close_prov,
                              actor_label=actor)
    db.commit()
    return row


def _requirement(db):
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id="view-v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="TENANT_FALLBACK", rank=6,
                                         minutes=REQUIRED_MINUTES)])
    db.flush()


def _seal_and_evaluate(db):
    assert produce_and_seal_work_day(
        db, organization_id=ORG, user_id=WORKER,
        work_date=DAY).outcome is ProducerOutcome.SEALED
    rec = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                  work_date=DAY)
    assert rec.is_recorded, f"{rec.reason} {rec.detail}"
    db.flush()
    return rec


def _grant(db, *, organization_id=ORG, user_id=ADMIN):
    db.add(EcoViewGrant(organization_id=organization_id, user_id=user_id,
                        granted_by="owner@v.test"))
    db.commit()


def _read(db, *, caller_id, target=WORKER, on=DAY):
    return read_eco_day(db, caller=db.get(dbm.User, caller_id),
                        user_id=target, work_date=on)


# ══════════════════════════════════════════════════════════════════════════════
# 1. The deferred day, and what makes it explainable
# ══════════════════════════════════════════════════════════════════════════════
class TestDeferredDay:
    def test_an_unverified_exit_reads_as_deferred(self, db):
        """Proofs 1, 2, 3, 4."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)

        view = _read(db, caller_id=WORKER)
        assert isinstance(view, EcoDayView)
        # 1. identified as deferred
        assert view.is_deferred
        assert view.evaluation_state == EcoDayState.DEFERRED_PENDING_LEDGER.value
        # 2. contributes 0
        assert view.contributes_to_accumulator_minutes == 0
        # 3. not a shortfall
        assert view.shortfall_computed is False
        assert view.evaluation_state != EcoDayState.SHORTFALL.value
        # 4. not excused — NOT_A_WORKING_DAY is the excused shape, and this is
        #    not it. "Neither counted nor excused" (§24.7) holds in both
        #    directions.
        assert view.evaluation_state != EcoDayState.NOT_A_WORKING_DAY.value
        assert view.evaluation_state != EcoDayState.NO_SHORTFALL.value

    def test_the_two_deferral_causes_stay_distinguishable(self, db):
        """Proof 5 — §19.1, at the read surface.

        Both reach DEFERRED_PENDING_LEDGER, as §24.6 and §24.7 both require.
        What tells them apart is the canonical ledger evidence, and no third
        name was minted to carry the difference.
        """
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)

        unverified = _read(db, caller_id=WORKER)
        assert unverified.is_deferred
        assert unverified.is_unverified_exit is True
        assert unverified.is_missing_clock_out is False
        assert unverified.ledger_state == LedgerState.SEALED.value
        assert unverified.has_unverified_exit is True

    def test_a_genuine_missing_clock_out_reads_as_the_other_cause(self, db):
        """Proof 5, the §24.6 side."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=None)
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=DAY).outcome is ProducerOutcome.SEALED
        evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                work_date=DAY)
        db.flush()

        view = _read(db, caller_id=WORKER)
        assert view.is_deferred
        assert view.is_missing_clock_out is True
        assert view.is_unverified_exit is False
        assert view.ledger_state == LedgerState.MISSING_CLOCK_OUT.value
        assert view.has_unverified_exit is False

    def test_the_two_causes_are_mutually_exclusive_by_construction(self, db):
        """Neither day can claim both, whatever the evidence says."""
        view = EcoDayView(
            organization_id=ORG, user_id=WORKER, work_date=DAY,
            evaluation_state=EcoDayState.DEFERRED_PENDING_LEDGER.value,
            evaluation_generation=1, shortfall_computed=False,
            contributes_to_accumulator_minutes=0, ledger_version=1,
            ledger_state=LedgerState.MISSING_CLOCK_OUT.value,
            has_unverified_exit=True,            # contradictory input…
            integrity_fault_reason=None,
            supersedes_record_id=None, superseded_by_record_id=None)
        # …and the properties still cannot both be true: §24.7 requires SEALED.
        assert view.is_missing_clock_out is True
        assert view.is_unverified_exit is False

    def test_a_verified_day_is_not_deferred(self, db):
        """Non-regression: an ordinary day reads as an ordinary day."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        _seal_and_evaluate(db)

        view = _read(db, caller_id=WORKER)
        assert not view.is_deferred
        assert view.shortfall_computed is True
        assert view.is_unverified_exit is False
        assert view.is_missing_clock_out is False
        assert view.ledger_state == LedgerState.SEALED.value

    def test_the_mixed_session_day_reads_as_an_unverified_exit(self, db):
        """The case owner ruling B3/Option A fixed, reaching the read surface.

        The day's FINAL departure is verified and the ledger column says so —
        and the day is still an unverified exit, because §19 step 6 is a
        property of the whole sealed interval set.
        """
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15))
        _seal_and_evaluate(db)

        ledger = db.query(WorkDayLedger).one()
        assert ledger.departure_provenance == Provenance.WORKER.value

        view = _read(db, caller_id=WORKER)
        assert view.is_deferred
        assert view.is_unverified_exit is True
        assert view.contributes_to_accumulator_minutes == 0

    def test_the_predicate_is_eval_days_own(self, db):
        """Proof: the boundary is defined once, not twice.

        The contract's answer must equal what EVAL-DAY's §19 step-6 predicate
        sees. Restating the rule here would let the two drift, and a read
        surface disagreeing with the evaluator is exactly how §24.6 and §24.7
        get conflated again.
        """
        from app.eval_day.evaluator import _has_unverified_exit
        from app.work_ledger.service import read_intervals

        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 12),
                 close_prov=Provenance.OFFBOARDING, actor="SYSTEM")
        _session(db, clock_in=_local(DAY, 13), clock_out=_local(DAY, 15))
        _seal_and_evaluate(db)

        ledger = db.query(WorkDayLedger).one()
        assert _read(db, caller_id=WORKER).has_unverified_exit is \
            _has_unverified_exit(ledger, read_intervals(db, ledger))


# ══════════════════════════════════════════════════════════════════════════════
# 2. Fail-closed and historical
# ══════════════════════════════════════════════════════════════════════════════
class TestEvidenceIntegrity:
    def test_an_integrity_fault_day_is_never_evaluated(self, db):
        """Proof 6. §24.15: not evaluated, not repaired, not clamped.

        No ECO record exists for such a day, so the read returns None rather
        than a view asserting anything about it — which is the fail-closed
        answer, not an empty success.
        """
        from app.work_ledger.service import DayKey, record_integrity_fault

        _requirement(db)
        record_integrity_fault(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=DAY),
            reason="OVERLAPPING_INTERVALS", timezone_iana=ZONE,
            utc_offset_seconds=19800, is_working_day=True,
            working_day_source="org", created_by="SYSTEM")
        db.flush()

        rec = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                      work_date=DAY)
        assert not rec.is_recorded
        assert _read(db, caller_id=WORKER) is None

    def test_a_day_with_no_record_reads_as_none_not_as_a_view(self, db):
        assert _read(db, caller_id=WORKER) is None

    def test_the_historical_ledger_version_is_used(self, db):
        """Proof 7. The record names its own version; that is what is read."""
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)

        view = _read(db, caller_id=WORKER)
        ledger = db.query(WorkDayLedger).one()
        assert view.ledger_version == int(ledger.ledger_version)
        assert view.evaluation_generation == 1
        assert view.superseded_by_record_id is None

    def test_current_configuration_cannot_change_a_historical_read(self, db):
        """Proof 8 — the guarantee §12.4 demands, asserted end to end.

        Every input a later administrator could plausibly change is changed:
        the working-week boundary, the working days, the timezone and the
        requirement. The recorded answer does not move, because none of them is
        consulted — the view comes off immutable rows.
        """
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)
        before = _read(db, caller_id=WORKER)

        db.query(dbm.AppSettings).filter(
            dbm.AppSettings.key == K.SETTING_WEEK_START_DAY).update(
            {"value": "SUNDAY"})
        db.query(dbm.AppSettings).filter(
            dbm.AppSettings.key == K.SETTING_WORKING_DAYS).update(
            {"value": json.dumps(["TUESDAY"])})
        db.query(dbm.OrgSettings).filter(
            dbm.OrgSettings.organization_id == ORG).update(
            {"default_timezone": "America/New_York"})
        db.commit()
        from app import _tz
        _tz.invalidate_tz_cache()

        assert _read(db, caller_id=WORKER) == before

    def test_a_correction_produces_a_new_reading_not_a_rewritten_one(self, db):
        """Lineage (§23): the superseded record keeps its own evidence."""
        from app.work_ledger.integrity import CandidateInterval
        from app.work_ledger.service import (
            DayKey, read_intervals, supersede_day)
        from app.work_ledger.states import SYNTHESIZED_CLOSURE_PROVENANCE

        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)
        assert _read(db, caller_id=WORKER).is_unverified_exit

        sealed = db.query(WorkDayLedger).one()
        syn = {p.value for p in SYNTHESIZED_CLOSURE_PROVENANCE}

        def _aware(dt):
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

        supersede_day(
            db, DayKey(organization_id=ORG, user_id=WORKER, work_date=DAY),
            expected_version=int(sealed.ledger_version),
            intervals=[
                CandidateInterval(
                    seq=i.seq, state=i.state,
                    start_at_utc=_aware(i.start_at_utc),
                    end_at_utc=_aware(i.end_at_utc),
                    provenance=(Provenance.ADMIN_OVERRIDE.value
                                if i.provenance in syn else i.provenance),
                    source_ref=i.source_ref)
                for i in read_intervals(db, sealed)],
            timezone_iana=sealed.timezone_iana,
            utc_offset_seconds=int(sealed.utc_offset_seconds),
            is_working_day=bool(sealed.is_working_day),
            working_day_source=sealed.working_day_source,
            departure_at=_aware(sealed.departure_at),
            departure_provenance=Provenance.ADMIN_OVERRIDE.value,
            departure_source_ref=sealed.departure_source_ref,
            source_fingerprint=sealed.source_fingerprint,
            created_by="admin@v.test")
        db.flush()
        evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                work_date=DAY)
        db.flush()

        after = _read(db, caller_id=WORKER)
        assert not after.is_deferred
        assert after.evaluation_generation == 2
        assert after.supersedes_record_id is not None
        assert after.ledger_version == int(sealed.ledger_version) + 1


# ══════════════════════════════════════════════════════════════════════════════
# 3. Authorization — the B2 primitive, not a second rule
# ══════════════════════════════════════════════════════════════════════════════
class TestAuthorization:
    @pytest.fixture(autouse=True)
    def _a_recorded_day(self, db):
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)

    def test_a_worker_reads_their_own_record(self, db):
        """Proof 9."""
        assert isinstance(_read(db, caller_id=WORKER), EcoDayView)

    def test_a_worker_cannot_read_another_worker(self, db):
        """Proof 10. A denial comes back AS a denial, never as an empty read."""
        out = _read(db, caller_id=OTHER, target=WORKER)
        assert isinstance(out, ViewAuthorityDecision)
        assert not out.is_authorized
        assert out.reason is DenialReason.NO_GRANT

    def test_a_granted_administrator_reads_a_worker_record(self, db):
        """Proof 11."""
        _grant(db)
        assert isinstance(_read(db, caller_id=ADMIN), EcoDayView)

    def test_a_cross_tenant_read_is_denied(self, db):
        """Proof 12."""
        _grant(db, organization_id=ORG_B, user_id=ADMIN_B)
        out = _read(db, caller_id=ADMIN_B, target=WORKER)
        assert isinstance(out, ViewAuthorityDecision)
        assert out.reason is DenialReason.CROSS_TENANT

    def test_no_tenant_context_is_denied(self, db):
        """Proof 13 — including super_admin."""
        out = _read(db, caller_id=OWNER, target=WORKER)
        assert out.reason is DenialReason.NO_TENANT_CONTEXT

    def test_no_grant_is_denied(self, db):
        """Proof 14."""
        out = _read(db, caller_id=ADMIN, target=WORKER)
        assert out.reason is DenialReason.NO_GRANT

    def test_role_strings_change_nothing(self, db):
        """Proof 15, behavioural."""
        admin = db.get(dbm.User, ADMIN)
        for role in ("admin", "hr", "owner", "super_admin", "manager", None):
            admin.role = role
            db.commit()
            assert _read(db, caller_id=ADMIN).reason is DenialReason.NO_GRANT
        _grant(db)
        for role in ("employee", None):
            admin.role = role
            db.commit()
            assert isinstance(_read(db, caller_id=ADMIN), EcoDayView)

    def test_there_is_no_organization_id_parameter(self):
        """The tenant boundary cannot be named by a caller of this contract
        either — the same guarantee the B2 resolver makes, one layer up."""
        import inspect

        sig = inspect.signature(read_eco_day)
        assert set(sig.parameters) == {"db", "caller", "user_id", "work_date"}

    def test_authorization_is_not_duplicated(self):
        """Proof 15, structural. One gate, called once, and no second rule."""
        src = (BACKEND / "app" / "eco_day_view" / "service.py").read_text(
            encoding="utf-8")
        assert src.count("require_eco_view_authority(") == 1
        tree = ast.parse(src)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("role", "head_user_id", "department", "designation",
                       "email", "is_admin"):
            assert banned not in names, f"the read contract reads {banned}"


# ══════════════════════════════════════════════════════════════════════════════
# 4. The copy gate holds
# ══════════════════════════════════════════════════════════════════════════════
class TestCopyGateHolds:
    PACKAGE = BACKEND / "app" / "eco_day_view"

    def test_no_deferred_copy_was_registered(self):
        """Proof 18. B1 is still Legal-blocked and nothing slipped past it."""
        from app.early_clock_out import LOCKED_COPY

        assert "eco.status.deferred" not in LOCKED_COPY
        assert len(LOCKED_COPY) == 12
        for key, value in LOCKED_COPY.items():
            assert value.strip(), f"{key} is an empty placeholder"
            assert "TODO" not in value.upper(), key

    def test_the_contract_carries_no_rendered_copy(self):
        """The view returns states and evidence, never a label or a sentence."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(EcoDayView)}
        for banned in ("label", "status_label", "display", "title", "body",
                       "text", "message", "copy", "explanation", "summary",
                       "description"):
            assert banned not in fields, f"the view carries {banned}"

    def test_no_status_vocabulary_was_invented_in_the_package(self):
        """Proof 19 support. §28.1's six values are locked to §31, and §31 does
        not register them — so none of them may appear as a string here."""
        for path in sorted(self.PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            literals = {n.value for n in ast.walk(tree)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            for banned in ("Deferred", "Under review", "Not a working day",
                           "Counted", "Complete", "Excluded",
                           "Pending Review", "pending review"):
                assert banned not in literals, (
                    f"{path.name} invents the status string {banned!r}")

    def test_the_prohibited_terminology_gate_is_still_armed(self):
        """Proof 19. Untouched by this increment, and proven to still bite."""
        from app.alert.rendering import ProhibitedTerminology, assert_clean
        from app.early_clock_out import find_prohibited_terms

        assert find_prohibited_terms("this is a violation")
        with pytest.raises(ProhibitedTerminology):
            assert_clean("the Worker is in breach of policy")
        assert_clean("Workweek: 2026-09-07")

    def test_no_prohibited_term_appears_in_the_package(self):
        for path in sorted(self.PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            literals = " ".join(
                n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str))
            from app.early_clock_out import find_prohibited_terms
            assert not find_prohibited_terms(literals), path.name


# ══════════════════════════════════════════════════════════════════════════════
# 5. Scope — the read foundation stays foundation
# ══════════════════════════════════════════════════════════════════════════════
class TestScopeHeld:
    PACKAGE = BACKEND / "app" / "eco_day_view"

    def test_nothing_in_the_application_wires_this_package(self):
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "eco_day_view" in path.parts:
                continue
            if "eco_day_view" in path.read_text(encoding="utf-8",
                                                errors="ignore"):
                offenders.append(str(path.relative_to(BACKEND)))
        assert offenders == [], f"unexpected wiring: {offenders}"

    #: The ONE module in this package permitted to be an HTTP surface: the
    #: GV-17 read route, authorized as the visibility seam. Every other module
    #: stays transport-free, so the read contract and the renderer remain
    #: callable and testable without FastAPI.
    HTTP_SEAM = "router.py"

    def test_only_the_authorized_seam_declares_an_http_surface(self):
        """``router.py`` LEFT this ban when the owner authorized the seam.

        The ban was never about FastAPI as such — it was about the read
        contract and the renderer staying pure. They still are; the route is
        the only module that speaks HTTP, and it computes nothing.
        """
        for path in sorted(self.PACKAGE.glob("*.py")):
            if path.name == self.HTTP_SEAM:
                continue
            src = path.read_text(encoding="utf-8")
            for banned in ("APIRouter", "fastapi", "@router", "HTTPException",
                           "Depends(", "BaseModel"):
                assert banned not in src, f"{path.name} declares an HTTP surface"

    def test_the_seam_is_a_route_and_nothing_more(self):
        """What the one permitted HTTP module may contain: a router and a
        handler. No model, no schema, no second authorization rule."""
        src = (self.PACKAGE / self.HTTP_SEAM).read_text(encoding="utf-8")
        assert "APIRouter" in src
        for banned in ("__tablename__", "Column(", "BaseModel",
                       "get_current_ledger", "evaluate_day"):
            assert banned not in src, f"router.py contains {banned}"

    def test_it_declares_no_table_and_no_migration(self):
        for path in sorted(self.PACKAGE.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for banned in ("__tablename__", "op.create_table", "Column("):
                assert banned not in src, f"{path.name} declares storage"
        for path in sorted((BACKEND / "alembic" / "versions").glob("*.py")):
            body = path.read_text(encoding="utf-8", errors="ignore")
            assert "eco_day_view" not in body, path.name

    def test_it_writes_nothing(self):
        """Proof 17 support: a read contract that can write is not a read
        contract."""
        tree = ast.parse(
            (self.PACKAGE / "service.py").read_text(encoding="utf-8"))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("add", "add_all", "delete", "merge", "commit", "flush",
                       "bulk_save_objects"):
            assert banned not in called, f"the read contract calls {banned}"

    def test_no_notification_recipient_or_reviewer_behaviour(self):
        """Proofs 16 and 17.

        ``audience`` LEFT THE BANNED LIST, and the claim was narrowed rather
        than dropped. The original list conflated two different things under
        one word: a NOTIFICATION audience, which resolves who gets told and is
        ALERT's concern, and a SURFACE audience, which selects which approved
        copy WF-ECO-03 vs WF-ECO-04 shows. Step 2 introduced the second under
        owner audience model A.

        So the delivery vocabulary stays banned outright, and the surface
        audience is pinned POSITIVELY below instead — two members, no recipient
        resolution — which is a stronger statement than the word's absence was.
        """
        for path in sorted(self.PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                     | {n.attr for n in ast.walk(tree)
                        if isinstance(n, ast.Attribute)})
            for banned in ("recipient", "notify", "notification",
                           "reviewer", "deliver", "dispatch", "alert",
                           "outbox", "retry", "email", "websocket", "push"):
                assert not any(banned in x.lower() for x in names), (
                    f"{path.name} performs {banned} work")

    def test_the_audience_is_a_surface_selector_not_a_recipient(self):
        """What ``audience`` is permitted to mean here, stated positively."""
        from app.eco_day_view import Audience

        # Exactly two surfaces, and nothing that names a person.
        assert {a.value for a in Audience} == {"WORKER", "ADMINISTRATOR"}
        # It carries no identity: no user id, no address, no resolution.
        for member in Audience:
            assert isinstance(member.value, str)
        # It decides no authority — that is the B2 primitive's job. Checked as
        # a CALL and an IMPORT rather than as a substring: the module docstring
        # names the primitive to say the renderer relies on it having already
        # run, and that sentence is documentation worth keeping.
        tree = ast.parse((self.PACKAGE / "rendering.py").read_text(
            encoding="utf-8"))
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "require_eco_view_authority" not in called
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                imported |= {a.name for a in n.names}
            elif isinstance(n, ast.Import):
                imported |= {a.name for a in n.names}
        assert "require_eco_view_authority" not in imported
        # And it names no person: no identity, no address, no send.
        names = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                 | {n.attr for n in ast.walk(tree)
                    if isinstance(n, ast.Attribute)})
        for banned in ("user_id", "email", "send"):
            assert banned not in names, f"rendering.py names {banned}"

    def test_it_reads_no_clock_and_recomputes_nothing(self):
        for path in sorted(self.PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                     | {n.attr for n in ast.walk(tree)
                        if isinstance(n, ast.Attribute)})
            for banned in ("now", "utcnow", "today", "evaluate_day",
                           "resolve_requirement_strict", "get_current_ledger"):
                assert banned not in names, f"{path.name} names {banned}"

    def test_eco_v1_rollout_and_the_golden_state_are_untouched(self):
        import hashlib

        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

        blob = (BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
                ).read_bytes()
        # Re-pinned by the PROMOTION of GV-26. Its STATE-CONTRACT citation
        # was satisfied by the existing D-8.1 contract — LedgerState owns
        # ledger facts and EcoDayState stays the seven-member evaluation
        # vocabulary, so no evaluation state was added — and its ALERT
        # citation was STALE: §24.15 asks for a platform INCIDENT, which has
        # no audience, no recipient, no channel and no delivery, so ALERT's
        # entry point could never have fired for it either. Owner ruling
        # D-9.1 authorized app.eco_ledger_incident. Harness-only: exactly
        # one vector changed, the count moved 27/1 -> 28/0, and the
        # normative projection is byte-for-byte unchanged.
        assert hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest() == \
            "2a7a0fb34145fe7fd210db585cc44f198974a364"

    def test_gv17_is_promoted_and_this_package_is_why(self):
        """This package is half the reason GV-17 could be promoted.

        The guard previously asserted GV-17 was still blocked, which was the
        right claim while VISIBILITY-SURFACE was open: an increment that
        promoted the vector from inside the surface it was waiting on would
        have been marking its own homework. GV-17 was promoted on 2026-09-17
        after its own promotion-readiness audit, so the claim inverts.
        """
        vectors = json.loads(
            (BACKEND / "tests" / "golden" / "eco_golden_vectors.json")
            .read_text(encoding="utf-8"))["vectors"]
        gv17 = next(v for v in vectors if v["id"] == "GV-17")
        assert gv17["harness"]["execution"] == "executable"
        assert gv17["harness"]["blocking_dependencies"] == []
        # The normative half is what must NOT have moved.
        assert gv17["expected_fields"]["state"] == "DEFERRED_PENDING_LEDGER"
        assert gv17["expected_fields"]["visible_to_worker"] is True
        assert gv17["expected_fields"]["visible_to_administrator"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 6. GV-17 VISIBILITY-SURFACE — WF-ECO-03 and WF-ECO-04 (Step 2)
# ══════════════════════════════════════════════════════════════════════════════
class TestVisibilitySurface:
    """§24.7's "visible to both Worker and administrator", end to end.

    Every expected string is IMPORTED from the register, never retyped — a test
    that hard-codes production copy is a second, unreviewed transcription of it,
    and the anti-drift guard in tests/test_eco_locked_copy.py already owns the
    character-for-character claim.
    """

    @pytest.fixture(autouse=True)
    def _a_deferred_day(self, db):
        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)

    def _render(self, db, *, caller_id, audience, target=WORKER):
        from app.eco_day_view import render_eco_day

        view = _read(db, caller_id=caller_id, target=target)
        assert isinstance(view, EcoDayView), view
        return render_eco_day(view, audience=audience)

    # -- Worker: WF-ECO-03 --------------------------------------------------
    def test_the_worker_sees_the_deferred_status_and_explanation(self, db):
        from app.early_clock_out import (OWNER_AUTHORIZED_COPY,
                                         SECTION_28_1_STATUS_COPY)
        from app.eco_day_view import Audience

        out = self._render(db, caller_id=WORKER, audience=Audience.WORKER)
        assert out.is_renderable
        assert out.status == SECTION_28_1_STATUS_COPY["eco.status.deferred"]
        assert out.body == OWNER_AUTHORIZED_COPY["eco.deferred.body"]
        assert out.copy_keys == ("eco.status.deferred", "eco.deferred.body")

    def test_the_worker_body_is_exactly_the_approved_string(self, db):
        """No prefix, no suffix, no wrapper — the approved sentence alone."""
        from app.early_clock_out import OWNER_AUTHORIZED_COPY
        from app.eco_day_view import Audience

        out = self._render(db, caller_id=WORKER, audience=Audience.WORKER)
        assert out.body == OWNER_AUTHORIZED_COPY["eco.deferred.body"]

    def test_the_worker_body_carries_no_administrator_copy(self, db):
        from app.early_clock_out import LOCKED_COPY
        from app.eco_day_view import Audience

        out = self._render(db, caller_id=WORKER, audience=Audience.WORKER)
        assert LOCKED_COPY["eco.admin.disclaimer"] not in out.body
        assert "eco.admin.disclaimer" not in out.copy_keys

    # -- Administrator: WF-ECO-04 -------------------------------------------
    def test_the_administrator_sees_the_same_body_plus_the_disclaimer(self, db):
        """Audience model A, and §29.1 / §33.4's mandatory closing statement."""
        import dataclasses

        from app.early_clock_out import LOCKED_COPY, OWNER_AUTHORIZED_COPY
        from app.eco_day_view import Audience

        _grant(db)
        out = self._render(db, caller_id=ADMIN, audience=Audience.ADMINISTRATOR)
        assert out.is_renderable
        # Model A: the SAME approved explanation, not a variant of it.
        assert OWNER_AUTHORIZED_COPY["eco.deferred.body"] in out.body
        # §33.4: in the body itself, not a footer — the type has no footer.
        assert LOCKED_COPY["eco.admin.disclaimer"] in out.body
        assert out.copy_keys == ("eco.status.deferred", "eco.deferred.body",
                                 "eco.admin.disclaimer")
        fields = {f.name for f in dataclasses.fields(type(out))}
        assert "footer" not in fields

    def test_the_disclaimer_value_is_unchanged(self, db):
        from app.early_clock_out import LOCKED_COPY

        assert LOCKED_COPY["eco.admin.disclaimer"] == (
            "This is an attendance condition recorded by Temveris. It is not "
            "a finding of misconduct and carries no recommendation. Any "
            "employment decision is yours and must be made by a person.")

    def test_both_audiences_receive_the_identical_explanation(self, db):
        """Model A, asserted as an equality rather than assumed."""
        from app.eco_day_view import Audience

        _grant(db)
        worker = self._render(db, caller_id=WORKER, audience=Audience.WORKER)
        admin = self._render(db, caller_id=ADMIN,
                             audience=Audience.ADMINISTRATOR)
        assert worker.status == admin.status
        assert admin.body.startswith(worker.body)

    # -- Terminology --------------------------------------------------------
    def test_the_worker_body_is_terminology_clean_with_no_exemption(self, db):
        from app.early_clock_out import find_prohibited_terms
        from app.eco_day_view import Audience

        out = self._render(db, caller_id=WORKER, audience=Audience.WORKER)
        assert find_prohibited_terms(out.body) == []

    def test_the_admin_body_is_clean_apart_from_the_mandated_exemption(self, db):
        """The disclaimer's one term is specification-mandated and negating;
        removing that exact registered string leaves a clean body."""
        from app.early_clock_out import LOCKED_COPY, find_prohibited_terms
        from app.eco_day_view import Audience

        _grant(db)
        out = self._render(db, caller_id=ADMIN, audience=Audience.ADMINISTRATOR)
        without = out.body.replace(LOCKED_COPY["eco.admin.disclaimer"], " ")
        assert find_prohibited_terms(without) == []

    def test_the_gate_refuses_a_body_carrying_prohibited_vocabulary(self):
        """Guard the guard: the §31.1 scan is a real filter, and the exemption
        mechanism cannot be handed an arbitrary string."""
        from app.eco_day_view.rendering import (ProhibitedTerminology,
                                                _assert_clean)

        _assert_clean("Workweek 2026-09-07")
        with pytest.raises(ProhibitedTerminology):
            _assert_clean("this day is a breach of policy")
        with pytest.raises(ValueError):
            _assert_clean("anything", exempt=("not a registered string",))

    # -- Only the deferred state is renderable ------------------------------
    def test_a_full_day_with_no_shortfall_renders_complete(self, db):
        """End to end, through a really sealed and evaluated day.

        This test previously asserted that every non-deferred day rendered
        nothing, which was true while "Complete" was unmapped. Owner Ruling 3
        mapped NO_SHORTFALL -> "Complete", so the eight-hour day below — which
        meets its 480-minute requirement exactly — now renders a label. The
        refusal it used to prove is covered per-state by ``TestStatusMapping``.
        """
        from app.early_clock_out import SECTION_28_1_STATUS_COPY
        from app.eco_day_view import Audience, render_eco_day

        other = date(2026, 9, 8)
        _session(db, clock_in=_local(other, 9), clock_out=_local(other, 17))
        assert produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER,
            work_date=other).outcome is ProducerOutcome.SEALED
        evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                work_date=other)
        db.flush()

        view = _read(db, caller_id=WORKER, on=other)
        assert not view.is_deferred
        assert view.evaluation_state == EcoDayState.NO_SHORTFALL.value
        out = render_eco_day(view, audience=Audience.WORKER)
        assert out.status == SECTION_28_1_STATUS_COPY["eco.status.complete"]
        assert out.body is None and not out.has_explanation
        assert out.copy_keys == ("eco.status.complete",)
        assert out.is_renderable

    def test_a_missing_clock_out_day_renders_the_same_deferred_copy(self, db):
        """§24.6 and §24.7 both reach DEFERRED_PENDING_LEDGER, so both render
        the deferred copy — while staying distinguishable in the evidence."""
        from app.eco_day_view import Audience, render_eco_day

        other = date(2026, 9, 8)
        _session(db, clock_in=_local(other, 9), clock_out=None)
        produce_and_seal_work_day(db, organization_id=ORG, user_id=WORKER,
                                  work_date=other)
        evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                work_date=other)
        db.flush()

        view = _read(db, caller_id=WORKER, on=other)
        assert view.is_deferred and view.is_missing_clock_out
        assert not view.is_unverified_exit          # still distinguishable
        out = render_eco_day(view, audience=Audience.WORKER)
        assert out.is_renderable

    # -- Authorization is the B2 primitive's, not the renderer's ------------
    def test_an_unauthorized_reader_never_reaches_the_renderer(self, db):
        """Proof that rendering cannot be used to bypass authority: the read
        returns a denial, and a denial is not an EcoDayView."""
        out = _read(db, caller_id=OTHER, target=WORKER)
        assert isinstance(out, ViewAuthorityDecision)
        assert out.reason is DenialReason.NO_GRANT
        assert not isinstance(out, EcoDayView)

    def test_a_cross_tenant_reader_never_reaches_the_renderer(self, db):
        _grant(db, organization_id=ORG_B, user_id=ADMIN_B)
        out = _read(db, caller_id=ADMIN_B, target=WORKER)
        assert isinstance(out, ViewAuthorityDecision)
        assert out.reason is DenialReason.CROSS_TENANT

    def test_an_ungranted_administrator_never_reaches_the_renderer(self, db):
        out = _read(db, caller_id=ADMIN, target=WORKER)
        assert isinstance(out, ViewAuthorityDecision)
        assert out.reason is DenialReason.NO_GRANT

    def test_rendering_performs_no_authorization_of_its_own(self, db):
        """The renderer takes a view, not a caller — it structurally cannot
        authorize, and a caller cannot reach it without passing B2 first."""
        import inspect

        from app.eco_day_view import render_eco_day

        params = set(inspect.signature(render_eco_day).parameters)
        assert params == {"view", "audience"}
        for banned in ("db", "caller", "user_id", "session"):
            assert banned not in params

    # -- Historical determinism ---------------------------------------------
    def test_current_configuration_cannot_change_the_rendered_result(self, db):
        """The rendering follows the persisted record, not today's settings."""
        from app import _tz
        from app.eco_day_view import Audience

        before = self._render(db, caller_id=WORKER, audience=Audience.WORKER)

        db.query(dbm.AppSettings).filter(
            dbm.AppSettings.key == K.SETTING_WEEK_START_DAY).update(
            {"value": "SUNDAY"})
        db.query(dbm.AppSettings).filter(
            dbm.AppSettings.key == K.SETTING_WORKING_DAYS).update(
            {"value": json.dumps(["TUESDAY"])})
        db.query(dbm.OrgSettings).filter(
            dbm.OrgSettings.organization_id == ORG).update(
            {"default_timezone": "America/New_York"})
        db.commit()
        _tz.invalidate_tz_cache()

        assert self._render(db, caller_id=WORKER,
                            audience=Audience.WORKER) == before

    def test_rendering_creates_no_records_of_any_kind(self, db):
        """No events, no notifications, no delivery attempts, no writes."""
        from app.eco_day_view import Audience

        counts = {}
        for table in ("eco_threshold_event", "eco_notification",
                      "eco_notification_delivery_attempt", "eco_day_record",
                      "work_day_ledger"):
            if db.execute(sa.text(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' "
                    "AND name='" + table + "'")).scalar():
                counts[table] = db.execute(
                    sa.text("SELECT count(*) FROM " + table)).scalar()

        _grant(db)
        for audience in (Audience.WORKER, Audience.ADMINISTRATOR):
            caller = WORKER if audience is Audience.WORKER else ADMIN
            self._render(db, caller_id=caller, audience=audience)
        db.flush()

        for table, before in counts.items():
            after = db.execute(
                sa.text("SELECT count(*) FROM " + table)).scalar()
            assert after == before, table + " changed during rendering"

    def test_the_rendering_carries_no_minutes_or_evidence(self):
        """Text only. Minutes and evidence stay on EcoDayView with their
        provenance, so a rendering cannot become a second source of truth."""
        import dataclasses

        from app.eco_day_view import RenderedEcoDay

        fields = {f.name for f in dataclasses.fields(RenderedEcoDay)}
        assert fields == {"audience", "status", "body", "copy_keys"}
        for banned in ("minutes", "contributes", "ledger_state",
                       "shortfall_computed", "ledger_version"):
            assert banned not in fields


# ══════════════════════════════════════════════════════════════════════════════
# 7. The state -> §28.1 Status mapping (Step 4)
# ══════════════════════════════════════════════════════════════════════════════
class TestStatusMapping:
    """Which of the six §28.1 labels the renderer may actually produce.

    All six are registered and approved. Only TWO are derivable from the
    persisted state model, and the other four fail closed rather than being
    mapped by nearest fit.
    """

    def _view(self, **kw):
        """A synthetic view. The mapping is a pure function of the state, so it
        is exercised directly rather than through a sealed day per case."""
        base = dict(
            organization_id=ORG, user_id=WORKER, work_date=DAY,
            evaluation_state=EcoDayState.SHORTFALL.value,
            evaluation_generation=1, shortfall_computed=True,
            contributes_to_accumulator_minutes=60, ledger_version=1,
            ledger_state=LedgerState.SEALED.value, has_unverified_exit=False,
            integrity_fault_reason=None,
            supersedes_record_id=None, superseded_by_record_id=None)
        base.update(kw)
        return EcoDayView(**base)

    # -- The two established mappings ---------------------------------------
    def test_deferred_renders_the_deferred_label_and_body(self):
        from app.early_clock_out import (OWNER_AUTHORIZED_COPY,
                                         SECTION_28_1_STATUS_COPY)
        from app.eco_day_view import Audience, render_eco_day

        out = render_eco_day(
            self._view(evaluation_state=EcoDayState.DEFERRED_PENDING_LEDGER.value,
                       shortfall_computed=False,
                       contributes_to_accumulator_minutes=0),
            audience=Audience.WORKER)
        assert out.status == SECTION_28_1_STATUS_COPY["eco.status.deferred"]
        assert out.body == OWNER_AUTHORIZED_COPY["eco.deferred.body"]
        assert out.copy_keys == ("eco.status.deferred", "eco.deferred.body")
        assert out.is_renderable and out.has_explanation

    def test_not_a_working_day_renders_the_label_only(self):
        """§24.2. The label is approved; no body was authorized for it."""
        from app.early_clock_out import SECTION_28_1_STATUS_COPY
        from app.eco_day_view import Audience, render_eco_day

        out = render_eco_day(
            self._view(evaluation_state=EcoDayState.NOT_A_WORKING_DAY.value),
            audience=Audience.WORKER)
        assert out.status == SECTION_28_1_STATUS_COPY["eco.status.notaworkingday"]
        assert out.body is None
        assert out.copy_keys == ("eco.status.notaworkingday",)
        # Renderable on the strength of the LABEL, with no explanation.
        assert out.is_renderable
        assert not out.has_explanation

    def test_the_label_only_state_is_identical_for_both_audiences(self):
        """No body means no administrator disclaimer either — the disclaimer
        belongs to a body, and there is none to carry it."""
        from app.eco_day_view import Audience, render_eco_day

        view = self._view(
            evaluation_state=EcoDayState.NOT_A_WORKING_DAY.value)
        worker = render_eco_day(view, audience=Audience.WORKER)
        admin = render_eco_day(view, audience=Audience.ADMINISTRATOR)
        assert worker.status == admin.status
        assert worker.body is None and admin.body is None
        assert worker.copy_keys == admin.copy_keys

    # -- The four that must fail closed -------------------------------------
    def test_no_shortfall_renders_complete_label_only(self):
        """Owner Ruling 3. NO_SHORTFALL -> "Complete", explicitly NOT
        distinguished from "Excluded" — that is a workflow outcome (Ruling 2),
        not a synonym for an evaluation state."""
        from app.early_clock_out import SECTION_28_1_STATUS_COPY
        from app.eco_day_view import Audience, render_eco_day

        out = render_eco_day(
            self._view(evaluation_state=EcoDayState.NO_SHORTFALL.value,
                       contributes_to_accumulator_minutes=0),
            audience=Audience.WORKER)
        assert out.status == SECTION_28_1_STATUS_COPY["eco.status.complete"]
        assert out.body is None
        assert out.has_explanation is False
        assert out.is_renderable
        assert out.copy_keys == ("eco.status.complete",)

    def test_complete_is_identical_for_both_audiences(self):
        """Label-only, so no body and therefore no administrator disclaimer."""
        from app.eco_day_view import Audience, render_eco_day

        view = self._view(evaluation_state=EcoDayState.NO_SHORTFALL.value)
        worker = render_eco_day(view, audience=Audience.WORKER)
        admin = render_eco_day(view, audience=Audience.ADMINISTRATOR)
        assert worker.status == admin.status
        assert worker.body is None and admin.body is None

    def test_shortfall_renders_counted_label_only(self):
        """Owner ruling on SHORTFALL. §28's own table renders a day with a
        positive gross shortfall as "Counted".

        The label describes the CALCULATION — the day was counted in the ECO
        arithmetic — not the Worker.
        """
        from app.early_clock_out import SECTION_28_1_STATUS_COPY
        from app.eco_day_view import Audience, render_eco_day

        out = render_eco_day(
            self._view(evaluation_state=EcoDayState.SHORTFALL.value),
            audience=Audience.WORKER)
        assert out.status == SECTION_28_1_STATUS_COPY["eco.status.counted"]
        assert out.body is None
        assert out.has_explanation is False
        assert out.is_renderable
        assert out.copy_keys == ("eco.status.counted",)

    def test_counted_is_identical_for_both_audiences(self):
        from app.eco_day_view import Audience, render_eco_day

        view = self._view(evaluation_state=EcoDayState.SHORTFALL.value)
        worker = render_eco_day(view, audience=Audience.WORKER)
        admin = render_eco_day(view, audience=Audience.ADMINISTRATOR)
        assert worker.status == admin.status
        assert worker.body is None and admin.body is None
        assert worker.copy_keys == admin.copy_keys

    def test_a_fully_excluded_shortfall_still_renders_counted(self):
        """The distinction the owner ruled explicitly.

        `gross > 0` with `attributable == 0` and exclusions applied is still
        SHORTFALL, and still renders "Counted". "Excluded" belongs to the
        exclusion-recording workflow (§9.4 / §29), not to an evaluation state —
        and the renderer could not tell the difference anyway, because it
        reads no exclusion field.
        """
        from app.early_clock_out import SECTION_28_1_STATUS_COPY
        from app.eco_day_view import Audience, render_eco_day

        out = render_eco_day(
            self._view(evaluation_state=EcoDayState.SHORTFALL.value,
                       contributes_to_accumulator_minutes=0),
            audience=Audience.WORKER)
        assert out.status == SECTION_28_1_STATUS_COPY["eco.status.counted"]
        assert out.status != SECTION_28_1_STATUS_COPY["eco.status.excluded"]

    @pytest.mark.parametrize("state", [
        EcoDayState.REQUIREMENT_UNRESOLVED.value,
        EcoDayState.EVALUABLE.value,
    ])
    def test_an_underivable_state_renders_no_label(self, state):
        """The regression this step exists to lock in.

        REQUIREMENT_UNRESOLVED has no §28.1 label at all; EVALUABLE is never
        persisted. Mapping either by nearest fit would make the Status column
        assert something the evidence does not support.

        NO_SHORTFALL and SHORTFALL both LEFT this list by owner ruling, in that
        order. Each was refused while its Complete/Excluded or
        Counted/Excluded/Under-review ambiguity was open; the owner closed both
        the same way — by ruling that "Excluded" and "Under review" are
        workflow outcomes and so were never candidates for an evaluation-state
        mapping in the first place.
        """
        from app.eco_day_view import Audience, render_eco_day

        out = render_eco_day(self._view(evaluation_state=state),
                             audience=Audience.WORKER)
        assert out.status is None, state
        assert out.body is None, state
        assert out.copy_keys == (), state
        assert not out.is_renderable and not out.has_explanation

    def test_every_unsupported_state_records_why(self):
        """The refusal is documented per state, as data, so adding a mapping
        means deleting an entry and saying what changed."""
        from app.eco_day_view.rendering import (UNSUPPORTED_STATUS_STATES,
                                                _STATUS_KEY_FOR_STATE)

        assert set(UNSUPPORTED_STATUS_STATES) == {
            "REQUIREMENT_UNRESOLVED", "EVALUABLE", "POLICY_UNRESOLVED"}
        for state, reason in UNSUPPORTED_STATUS_STATES.items():
            assert reason.strip(), state
        # Together the two tables partition the whole vocabulary: no state is
        # both mapped and refused, and none is silently neither.
        assert set(_STATUS_KEY_FOR_STATE) & set(UNSUPPORTED_STATUS_STATES) == set()
        assert set(_STATUS_KEY_FOR_STATE) | set(UNSUPPORTED_STATUS_STATES) == {
            s.value for s in EcoDayState}

    def test_under_review_and_excluded_remain_workflow_only(self):
        """Owner Rulings 1 and 2: these are WORKFLOW outcomes, never inferred.

        §28.1 defines "Under review" as what a raised dispute sets (§9.4), and
        §9.4's resolution emits ``EARLY_CLOCK_OUT_EXCLUDED`` — so "Excluded"
        follows an administrator recording an exclusion (§29). Both are set by
        a path that does not exist, and the owner ruled that even once it does,
        the renderer must not derive either from an evaluation state.

        "Counted" joins them for a different reason (Ruling 4): SHORTFALL alone
        does not establish it.
        """
        import dataclasses

        from app.early_clock_out import SECTION_28_1_STATUS_COPY
        from app.eco_day_view.rendering import _STATUS_KEY_FOR_STATE

        for key in ("eco.status.underreview", "eco.status.excluded"):
            assert key in SECTION_28_1_STATUS_COPY            # approved…
            assert key not in _STATUS_KEY_FOR_STATE.values()  # …and unreachable
        # "Complete" and "Counted" ARE reachable now, by successive owner
        # rulings. These two are the ONLY labels that remain unreachable, and
        # they remain so because they are workflow outcomes rather than
        # evaluation states — not because a mapping is merely undecided.
        for key in ("eco.status.complete", "eco.status.counted"):
            assert key in _STATUS_KEY_FOR_STATE.values(), key

        # WHAT GUARDS THIS CHANGED, AND IT IS WORTH KNOWING.
        # `exclusion_minutes` used to be ABSENT from EcoDayView, so "Excluded"
        # was underivable because the data was not there. Owner Ruling B
        # authorized exposing it for the §28.1 day detail, so absence is no
        # longer the guarantee — the RENDERER'S DISCIPLINE is. It reads
        # `evaluation_state` and nothing else, which
        # `test_the_audience_is_a_surface_selector_not_a_recipient` and the
        # Step-4 mapping tests both pin.
        fields = {f.name for f in dataclasses.fields(EcoDayView)}
        assert "exclusion_minutes" in fields          # exposed, by ruling
        tree = ast.parse(
            (BACKEND / "app" / "eco_day_view" / "rendering.py").read_text(
                encoding="utf-8"))
        read_off_view = {n.attr for n in ast.walk(tree)
                         if isinstance(n, ast.Attribute)
                         and isinstance(n.value, ast.Name)
                         and n.value.id == "view"}
        # Owner ruling D-2 added the SECOND field the renderer may read.
        # ``evaluation_deferral_reason`` selects between the two deferred
        # bodies, which are not interchangeable — it cannot reach "Under
        # review" or "Excluded", because it only ever chooses a BODY within a
        # status the state already selected. The discipline this guard protects
        # is unchanged: no measurement field is read, so no §28.1 label is
        # derived from one.
        assert read_off_view == {"evaluation_state",
                                 "evaluation_deferral_reason"}, read_off_view
        assert not (read_off_view & {
            "exclusion_minutes", "attributable_minutes",
            "gross_shortfall_minutes", "required_minutes"})
        # A dispute discriminator still does not exist anywhere.
        assert not any("disput" in f for f in fields)

    def test_the_mapping_produces_only_approved_registered_keys(self):
        from app.early_clock_out import SECTION_28_1_STATUS_COPY
        from app.eco_day_view.rendering import _STATUS_KEY_FOR_STATE

        for state, key in _STATUS_KEY_FOR_STATE.items():
            assert key in SECTION_28_1_STATUS_COPY, key
            assert state in {s.value for s in EcoDayState}, state

    def test_an_unknown_state_string_fails_closed(self):
        """A state this renderer has never heard of renders nothing, rather
        than falling through to a default label."""
        from app.eco_day_view import Audience, render_eco_day

        out = render_eco_day(self._view(evaluation_state="SOME_FUTURE_STATE"),
                             audience=Audience.WORKER)
        assert out.status is None and out.body is None
        assert out.copy_keys == ()

    # -- No production strings in the module --------------------------------
    def test_the_renderer_contains_no_literal_production_copy(self):
        """Every string resolves through a key. A literal here would be a
        second, unreviewed transcription of approved copy.

        CODE literals only — docstrings are excluded deliberately. The module
        documents WHY four labels cannot be rendered, and cannot do that
        without naming them ("SHORTFALL covers Counted, Excluded AND Under
        review"). Prose explaining a refusal is not a transcription of
        production copy; an assignable literal is, and that is what is banned.
        """
        from app.early_clock_out import (LOCKED_COPY, OWNER_AUTHORIZED_COPY,
                                         SECTION_28_1_STATUS_COPY)

        tree = ast.parse(
            (BACKEND / "app" / "eco_day_view" / "rendering.py").read_text(
                encoding="utf-8"))

        # Docstrings are string Constants standing alone as an expression
        # statement; every other string Constant is code.
        docstrings = {
            id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        code_literals = {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings
        }

        approved = {**LOCKED_COPY, **SECTION_28_1_STATUS_COPY,
                    **OWNER_AUTHORIZED_COPY}
        for key, value in approved.items():
            assert value not in code_literals, (
                f"rendering.py embeds the production string for {key!r} as a "
                f"code literal; resolve it through the register instead")

        # The long-form copy must not appear ANYWHERE, docstring included —
        # a body or disclaimer quoted in prose would drift silently.
        src = (BACKEND / "app" / "eco_day_view" / "rendering.py").read_text(
            encoding="utf-8")
        for key in ("eco.deferred.body", "eco.admin.disclaimer"):
            assert approved[key] not in src, (
                f"rendering.py quotes the long-form string for {key!r}")

    def test_the_renderer_reads_no_database_or_configuration(self):
        """Pure over its input: a frozen value object in, text out."""
        tree = ast.parse(
            (BACKEND / "app" / "eco_day_view" / "rendering.py").read_text(
                encoding="utf-8"))
        names = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                 | {n.attr for n in ast.walk(tree)
                    if isinstance(n, ast.Attribute)})
        for banned in ("execute", "query", "session", "Session", "select",
                       "now", "utcnow", "today", "resolve_requirement_strict",
                       "get_current_ledger", "evaluate_day"):
            assert banned not in names, f"rendering.py names {banned}"

    def test_a_historical_view_renders_without_recalculation(self, db):
        """End to end on a real sealed day: the rendering follows the persisted
        record, and rendering twice changes nothing."""
        from app.eco_day_view import Audience, render_eco_day

        _requirement(db)
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17),
                 close_prov=Provenance.STALE_AUTOCLOSE, actor="SYSTEM")
        _seal_and_evaluate(db)

        view = _read(db, caller_id=WORKER)
        first = render_eco_day(view, audience=Audience.WORKER)
        second = render_eco_day(view, audience=Audience.WORKER)
        assert first == second
        assert first.status is not None
        # The view it rendered from is unchanged.
        assert _read(db, caller_id=WORKER) == view
