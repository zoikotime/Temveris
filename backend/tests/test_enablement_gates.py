"""
The §44.3 enablement-gate assessment.
=====================================

ZT-SPEC-ECO-002 v2.0 §44.3 (nine gates), §40.4 (consultation), §30.1 (at least
one administrative recipient), §44.2 (two complete workweeks).

Two claims are proved here, and the second is the one that matters:

* the checker SATISFIES a gate only on tenant-specific evidence, and
* the checker is READ-ONLY — asserted by instrumenting the engine and failing
  on any INSERT, UPDATE or DELETE emitted during a full assessment.

THE EXPECTED REAL-WORLD ANSWER IS "NOT READY"
---------------------------------------------
No Tenant in this repository has observed a shadow period, and no DPIA,
consultation attestation or worker-notice record type exists at all. A fully
configured Tenant therefore still reads ENABLEMENT_NOT_READY, and a test pins
that. It is the correct result, not a gap in the suite.
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

import database.models as dbm
from database.database import Base

from app.enablement_gates import (
    GATE_ORDER, EnablementReadiness, GateId, GateStatus,
    ShadowObservation,
    assess_enablement_gates,
)
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState

from tests import _eco_enablement as ready
from app.rollout.models import RolloutControl
from app.schedule_policy import keys as K

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "enablement_gates"

TZ = "Asia/Kolkata"
MON_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]

ORG = 9960
OTHER_ORG = 9970
WORKER, ADMIN = 9961, 9962
OTHER_ADMIN = 9971

AS_OF = date(2026, 9, 24)
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)

#: The six gates with an evidence source in this repository.
TECHNICAL_GATES = (
    GateId.WORKWEEK_TIMEZONE_ATTRIBUTION, GateId.REQUIREMENT_FALLBACK,
    GateId.NETTING_DECISION, GateId.BREAK_TREATMENT,
    GateId.ADMINISTRATIVE_RECIPIENT, GateId.SHADOW_PERIOD,
)

#: The three with none. §44.3 names them; nothing stores them.
COMPLIANCE_GATES = (GateId.DPIA, GateId.CONSULTATION_ATTESTATION,
                    GateId.WORKER_NOTICE)


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def gate_engine():
    from tests._eco_enablement import create_schema

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    create_schema(eng)
    return eng


@pytest.fixture()
def db(gate_engine):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(gate_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Gate Org"))
        s.add(dbm.Organization(id=OTHER_ORG, name="Other Org"))
        for uid, org in ((WORKER, ORG), (ADMIN, ORG), (OTHER_ADMIN, OTHER_ORG)):
            s.add(dbm.User(id=uid, name="U", email=f"{uid}@gate.test",
                           organization_id=org, role="employee"))
        s.commit()
        yield s
    _tz.invalidate_tz_cache()


# ── Evidence builders. Each one satisfies exactly ONE gate. ─────────────────
def _workweek_evidence(db, *, organization_id=ORG, week_start="MONDAY",
                       timezone_iana=TZ):
    from app import _tz
    if timezone_iana is not None:
        db.add(dbm.OrgSettings(organization_id=organization_id,
                               default_timezone=timezone_iana))
    if week_start is not None:
        db.add(dbm.AppSettings(organization_id=organization_id, team_key=None,
                               user_id=None, key=K.SETTING_WEEK_START_DAY,
                               value=week_start))
    db.add(dbm.AppSettings(organization_id=organization_id, team_key=None,
                           user_id=None, key=K.SETTING_WORKING_DAYS,
                           value=json.dumps(MON_FRI)))
    db.commit()
    _tz.invalidate_tz_cache()


def _requirement_evidence(db, *, organization_id=ORG, minutes=480):
    from app.config_first_capture.service import (
        REQUIREMENT_RANK, REQUIREMENT_TIER,
    )
    from app.config_versioning import DatabaseConfigVersionProvider
    from app.requirement_resolution import RequirementCandidate

    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=organization_id,
        config_version_id=f"gate-cfg-{organization_id}",
        effective_from=date(2026, 1, 1),
        candidates=([RequirementCandidate(tier=REQUIREMENT_TIER,
                                          rank=REQUIREMENT_RANK,
                                          minutes=minutes)]
                    if minutes is not None else []))
    db.commit()


def _netting_evidence(db, *, organization_id=ORG, netting_mode="NONE"):
    from tests._eco_policy import capture_test_policy

    capture_test_policy(db, organization_id=organization_id,
                        netting_mode=netting_mode)


def _break_treatment_evidence(db, *, organization_id=ORG):
    from app.break_treatment.models import BreakTreatmentProfile

    db.add(BreakTreatmentProfile(
        organization_id=organization_id, profile_ref=f"brk-{organization_id}",
        effective_from=date(2026, 1, 1),
        captured_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        captured_by="owner@example.test",
        meal_qualifies=False, rest_qualifies=True,
        rest_qualifying_cap_minutes=30, content_hash="0" * 64))
    db.commit()


def _recipient_evidence(db, *, organization_id=ORG, user_id=ADMIN):
    from app.recipient_authority import EcoRecipientDesignation

    db.add(EcoRecipientDesignation(organization_id=organization_id,
                                   audience="PRIMARY_ADMIN", user_id=user_id))
    db.commit()


def _shadow_evidence(days_ago=20, *, state=RolloutState.SHADOW, entered=True):
    """A ``ShadowObservation``: the fact the gate needs, as a value.

    Owner ruling D6 inverted the dependency \u2014 the checker no longer reads the
    rollout control, it is HANDED the observation. So this builder writes no
    row at all, which is also why no real tenant can be enrolled by running
    this suite.
    """
    return ShadowObservation(
        exists=True, state=state.value, min_observation_days=14.0,
        shadow_entered_at=(NOW - timedelta(days=days_ago) if entered
                           else None))


def _all_technical_evidence(db, *, organization_id=ORG):
    _workweek_evidence(db, organization_id=organization_id)
    _requirement_evidence(db, organization_id=organization_id)
    _netting_evidence(db, organization_id=organization_id)
    _break_treatment_evidence(db, organization_id=organization_id)
    _recipient_evidence(
        db, organization_id=organization_id,
        user_id=ADMIN if organization_id == ORG else OTHER_ADMIN)


def _classifications(db):
    """The platform-governed country mapping. Tenant-independent."""
    ready.classification_evidence(db)


def _admin_actor(organization_id=ORG, user_id=ADMIN):
    return ready._Actor(user_id=user_id, organization_id=organization_id)


def _assess(db, *, organization_id=ORG, shadow=None) -> EnablementReadiness:
    return assess_enablement_gates(db, organization_id=organization_id,
                                   as_of=AS_OF, now=NOW, shadow=shadow)


# ═════════════════════════════════════════════════════════════════════════════
# 1. The contract
# ═════════════════════════════════════════════════════════════════════════════
class TestContract:
    def test_there_are_exactly_nine_gates_in_specification_order(self, db):
        result = _assess(db)
        assert len(GATE_ORDER) == 9
        assert [g.gate for g in result.gates] == list(GATE_ORDER)
        assert [g.gate.value for g in result.gates] == [
            "WORKWEEK_TIMEZONE_ATTRIBUTION", "REQUIREMENT_FALLBACK",
            "NETTING_DECISION", "BREAK_TREATMENT", "ADMINISTRATIVE_RECIPIENT",
            "SHADOW_PERIOD", "DPIA", "CONSULTATION_ATTESTATION",
            "WORKER_NOTICE"]

    def test_the_status_vocabulary_is_closed(self):
        assert {s.value for s in GateStatus} == {
            "SATISFIED", "UNSATISFIED", "EVIDENCE_MISSING", "EVIDENCE_INVALID",
            "NOT_APPLICABLE"}

    def test_there_is_one_readiness_threshold_not_two(self, db):
        """§44.3 defines ONE bar. Shadow is a gate inside the list, not a
        separate threshold, so no READY_FOR_SHADOW verdict is invented."""
        emitted = _emitted_string_literals(PACKAGE / "outcomes.py") \
            | _emitted_string_literals(PACKAGE / "service.py")
        verdicts = {v for v in emitted if v.startswith("ENABLEMENT")}
        assert verdicts == {"ENABLEMENT_READY", "ENABLEMENT_NOT_READY"}
        assert not any("READY_FOR_SHADOW" in v or "READY_FOR_ENABLED" in v
                       for v in emitted)

    def test_every_gate_result_carries_the_required_fields(self, db):
        row = _assess(db).gates[0].as_row()
        assert set(row) == {"gate", "status", "blocking", "reason", "evidence",
                            "missing_evidence"}

    def test_the_report_is_json_serialisable_and_timestamped(self, db):
        payload = _assess(db).as_dict()
        assert json.loads(json.dumps(payload, sort_keys=True))
        assert payload["evaluated_at"] == NOW.isoformat()
        assert payload["organization_id"] == ORG

    def test_an_assessment_requires_a_tenant(self, db):
        with pytest.raises(ValueError):
            assess_enablement_gates(db, organization_id=None)


# ═════════════════════════════════════════════════════════════════════════════
# 2. No fake readiness
# ═════════════════════════════════════════════════════════════════════════════
class TestNoFakeReadiness:
    def test_a_bare_tenant_satisfies_nothing(self, db):
        """Every capability is implemented in this repository, and that buys
        the Tenant exactly nothing."""
        result = _assess(db)
        assert result.verdict == "ENABLEMENT_NOT_READY"
        assert not any(g.passes for g in result.gates)

    def test_a_fully_configured_tenant_is_still_not_ready(self, db):
        """Configuration is not compliance.

        A Tenant with all six operational gates satisfied — workweek,
        timezone, fallback, netting, breaks, recipient — is still NOT ready.
        The three compliance gates are about what somebody DID outside this
        system, and no amount of configuring reaches them.
        """
        _all_technical_evidence(db)
        result = _assess(db, shadow=_shadow_evidence(20))
        assert result.verdict == "ENABLEMENT_NOT_READY"
        assert all(result.gate(g).status is GateStatus.SATISFIED
                   for g in TECHNICAL_GATES)
        assert all(result.gate(g).status is GateStatus.EVIDENCE_MISSING
                   for g in COMPLIANCE_GATES)
        assert {g.gate for g in result.blocking_gates} == set(COMPLIANCE_GATES)

    def test_readiness_needs_every_blocking_gate(self, db):
        """Asserted on the aggregate rule: every blocking gate must pass."""
        _all_technical_evidence(db)
        result = _assess(db, shadow=_shadow_evidence(20))
        assert all(g.blocking for g in result.gates)
        assert result.is_ready is False
        assert result.is_ready == all(g.passes for g in result.gates)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Each gate, missing and satisfied
# ═════════════════════════════════════════════════════════════════════════════
class TestWorkweekGate:
    def test_missing_boundary_and_timezone(self, db):
        gate = _assess(db).gate(GateId.WORKWEEK_TIMEZONE_ATTRIBUTION)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert "schedule.week_start_day" in gate.missing_evidence
        assert "org_settings.default_timezone" in gate.missing_evidence

    def test_boundary_without_timezone_is_still_missing(self, db):
        _workweek_evidence(db, timezone_iana=None)
        gate = _assess(db).gate(GateId.WORKWEEK_TIMEZONE_ATTRIBUTION)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.missing_evidence == ("org_settings.default_timezone",)

    def test_complete_configuration_satisfies(self, db):
        _workweek_evidence(db)
        gate = _assess(db).gate(GateId.WORKWEEK_TIMEZONE_ATTRIBUTION)
        assert gate.status is GateStatus.SATISFIED
        assert gate.evidence["week_start_day"] == "MONDAY"
        assert gate.evidence["default_timezone"] == TZ
        assert gate.evidence["supported_attribution_rules"] == ["START_DATE"]

    def test_a_ledger_row_with_an_unsupported_rule_is_invalid(self, db):
        """Tenant EVIDENCE, not a capability claim: what the sealed rows
        actually carry."""
        _workweek_evidence(db)
        db.execute(sa.text(
            "INSERT INTO work_day_ledger (organization_id, user_id, work_date,"
            " shift_attribution_rule, workweek_key, ledger_version, state,"
            " timezone_iana, utc_offset_seconds, is_working_day,"
            " working_day_source, created_by, created_at) VALUES (:o, :u,"
            " '2026-09-07 00:00:00', 'MAJORITY_HOURS', '2026-09-07', 1,"
            " 'SEALED', 'UTC', 0, 1, 'SCHEDULE', 'TEST', :c)"),
            {"o": ORG, "u": WORKER, "c": "2026-09-07 00:00:00+00:00"})
        db.commit()
        gate = _assess(db).gate(GateId.WORKWEEK_TIMEZONE_ATTRIBUTION)
        assert gate.status is GateStatus.EVIDENCE_INVALID


class TestRequirementFallbackGate:
    def test_no_configuration_version_is_missing_evidence(self, db):
        gate = _assess(db).gate(GateId.REQUIREMENT_FALLBACK)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.missing_evidence == ("config_version",)

    def test_a_version_without_a_rank_six_fallback_is_unsatisfied(self, db):
        """Implementation exists; tenant evidence does not."""
        _requirement_evidence(db, minutes=None)
        gate = _assess(db).gate(GateId.REQUIREMENT_FALLBACK)
        assert gate.status is GateStatus.UNSATISFIED
        assert gate.evidence["fallback_candidates"] == 0

    def test_a_rank_six_fallback_satisfies(self, db):
        _requirement_evidence(db)
        gate = _assess(db).gate(GateId.REQUIREMENT_FALLBACK)
        assert gate.status is GateStatus.SATISFIED
        assert gate.evidence["rank"] == 6
        assert gate.evidence["fallback_minutes"] == [480]


class TestNettingGate:
    def test_no_policy_version_is_missing_evidence(self, db):
        gate = _assess(db).gate(GateId.NETTING_DECISION)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.missing_evidence == ("policy_version",)

    def test_a_policy_version_records_the_decision(self, db):
        _netting_evidence(db, netting_mode="WEEKLY_BANK")
        gate = _assess(db).gate(GateId.NETTING_DECISION)
        assert gate.status is GateStatus.SATISFIED
        assert gate.evidence["netting_mode"] == "WEEKLY_BANK"

    def test_the_explicitness_limitation_is_reported_not_hidden(self, db):
        """§44.3 says "not accepted as a default", and the data cannot show
        that: ``netting_mode`` is NOT NULL, so every policy carries one. The
        gate must SAY so rather than implying a check it cannot perform."""
        _netting_evidence(db)
        gate = _assess(db).gate(GateId.NETTING_DECISION)
        assert "OD-02" in gate.reason
        assert "not independently verifiable" in gate.reason


class TestBreakTreatmentGate:
    def test_no_profile_is_missing_evidence(self, db):
        gate = _assess(db).gate(GateId.BREAK_TREATMENT)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.missing_evidence == ("break_treatment_profile",)

    def test_a_profile_sets_both_treatments(self, db):
        _break_treatment_evidence(db)
        gate = _assess(db).gate(GateId.BREAK_TREATMENT)
        assert gate.status is GateStatus.SATISFIED
        assert gate.evidence["meal_qualifies"] is False
        assert gate.evidence["rest_qualifies"] is True

    def test_a_profile_effective_later_does_not_count(self, db):
        from app.break_treatment.models import BreakTreatmentProfile

        db.add(BreakTreatmentProfile(
            organization_id=ORG, profile_ref="future", effective_from=
            date(2027, 1, 1), captured_at_utc=datetime(2027, 1, 1, tzinfo=UTC),
            captured_by="o@e.test", meal_qualifies=False, rest_qualifies=True,
            rest_qualifying_cap_minutes=30, content_hash="1" * 64))
        db.commit()
        assert _assess(db).gate(GateId.BREAK_TREATMENT).status \
            is GateStatus.EVIDENCE_MISSING


class TestRecipientGate:
    def test_no_primary_admin_is_missing_evidence(self, db):
        gate = _assess(db).gate(GateId.ADMINISTRATIVE_RECIPIENT)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.evidence["designation_count"] == 0

    def test_a_primary_admin_satisfies(self, db):
        _recipient_evidence(db)
        gate = _assess(db).gate(GateId.ADMINISTRATIVE_RECIPIENT)
        assert gate.status is GateStatus.SATISFIED
        assert gate.evidence["designation_count"] == 1

    def test_another_audience_is_never_substituted(self, db):
        """D-1: no substitution. An HR_ADMIN designation must not satisfy a
        gate that §30.1 writes about the administrative recipient."""
        from app.recipient_authority import EcoRecipientDesignation

        db.add(EcoRecipientDesignation(organization_id=ORG,
                                       audience="HR_ADMIN", user_id=ADMIN))
        db.commit()
        gate = _assess(db).gate(GateId.ADMINISTRATIVE_RECIPIENT)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.evidence["designation_count"] == 0


class TestShadowPeriodGate:
    def test_no_observation_supplied_is_missing_evidence(self, db):
        """Owner ruling D6: the observation is HANDED to the checker.

        A caller that supplies nothing has not proved a shadow period; it has
        said nothing about one, and the gate reports exactly that.
        """
        gate = _assess(db).gate(GateId.SHADOW_PERIOD)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.missing_evidence == ("shadow_observation",)

    def test_no_rollout_control_is_missing_evidence(self, db):
        obs = ShadowObservation.absent()
        gate = _assess(db, shadow=obs).gate(GateId.SHADOW_PERIOD)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.missing_evidence == ("rollout_control",)

    def test_an_off_control_has_not_begun(self, db):
        obs = _shadow_evidence(state=RolloutState.OFF, entered=False)
        gate = _assess(db, shadow=obs).gate(GateId.SHADOW_PERIOD)
        assert gate.status is GateStatus.UNSATISFIED
        assert "has not begun" in gate.reason

    def test_shadow_without_an_entry_timestamp_is_invalid(self, db):
        obs = _shadow_evidence(entered=False)
        gate = _assess(db, shadow=obs).gate(GateId.SHADOW_PERIOD)
        assert gate.status is GateStatus.EVIDENCE_INVALID

    def test_less_than_the_required_period_is_unsatisfied(self, db):
        gate = _assess(db, shadow=_shadow_evidence(13)).gate(
            GateId.SHADOW_PERIOD)
        assert gate.status is GateStatus.UNSATISFIED
        assert gate.evidence["observation_days_elapsed"] == 13.0
        assert gate.evidence["min_observation_days"] == 14.0

    def test_exactly_the_required_period_satisfies(self, db):
        gate = _assess(db, shadow=_shadow_evidence(14)).gate(
            GateId.SHADOW_PERIOD)
        assert gate.status is GateStatus.SATISFIED

    def test_the_period_is_read_from_the_observation_not_restated(self):
        """The gate and ``rollout.transition`` must enforce ONE number. Read
        from the supplied observation, never hard-coded here — asserted
        structurally so a later edit cannot fork them."""
        source = (PACKAGE / "service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        guard = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "_assess_shadow_period")
        numbers = {n.value for n in ast.walk(guard)
                   if isinstance(n, ast.Constant) and isinstance(n.value, int)}
        assert 14 not in numbers, "the observation window is restated here"
        attrs = {n.attr for n in ast.walk(guard) if isinstance(n, ast.Attribute)}
        assert "min_observation_days" in attrs

    def test_the_checker_cannot_reach_the_rollout_control_at_all(self):
        """The port is the WHOLE coupling. Not a convenience over an import
        that is still there — the import is gone, and this fails if it comes
        back."""
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any(m.startswith("app.rollout") for m in imported), imported

    def test_the_replay_harness_cannot_satisfy_this_gate(self, db):
        """THE CRITICAL SEPARATION.

        The replay harness's fourteen-day fixture observes nothing, creates no
        rollout row, and its dates are fixture dates. Running it — even to a
        HARNESS_CLEAN verdict — must leave this gate exactly where it was.
        """
        from tests._replay_harness import WINDOW_DAYS, window_dates

        assert WINDOW_DAYS == 14  # the same number, and still not evidence
        before = _assess(db).gate(GateId.SHADOW_PERIOD)
        assert before.status is GateStatus.EVIDENCE_MISSING

        # Everything the harness would leave behind is already absent: it
        # writes no rollout control at all.
        assert db.query(RolloutControl).count() == 0
        after = _assess(db).gate(GateId.SHADOW_PERIOD)
        assert after.status is GateStatus.EVIDENCE_MISSING
        assert after.missing_evidence == ("shadow_observation",)

        # And the harness window's dates are nowhere in the gate's evidence.
        harness_dates = {d.isoformat() for d in window_dates()}
        assert not (harness_dates & set(json.dumps(after.as_row()).split('"')))

    def test_the_gate_reads_only_the_rollout_control(self):
        """No harness import, no fixture dates, no replay module."""
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any("replay" in m for m in imported)
        assert not any("tests" in m for m in imported)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Compliance evidence — read, never fabricated
# ═════════════════════════════════════════════════════════════════════════════
class TestComplianceGates:
    @pytest.mark.parametrize("gate_id", COMPLIANCE_GATES)
    def test_a_tenant_with_no_evidence_is_missing_evidence(self, db, gate_id):
        """The stores existing satisfies nobody. Only a Tenant's own record
        does, which is the distinction §44.3 turns on."""
        _all_technical_evidence(db)
        gate = _assess(db).gate(gate_id)
        assert gate.status is GateStatus.EVIDENCE_MISSING
        assert gate.missing_evidence

    def test_an_unassigned_tenant_is_missing_evidence_not_not_applicable(self, db):
        """§44.3's "Yes for EU/UK" and "Yes where applicable" license
        NOT_APPLICABLE — but only where non-applicability is ESTABLISHED.

        A Tenant nobody ever assigned a jurisdiction to has told us nothing,
        and waiving a compliance gate on silence is how a control gets enabled
        in a jurisdiction that forbids it.
        """
        _classifications(db)
        result = _assess(db)
        for gate_id in (GateId.DPIA, GateId.CONSULTATION_ATTESTATION):
            gate = result.gate(gate_id)
            assert gate.status is GateStatus.EVIDENCE_MISSING
            assert "tenant_jurisdiction_assignment" in gate.missing_evidence

    def test_a_declared_empty_jurisdiction_set_reaches_not_applicable(self, db):
        """Owner ruling B9: B. THE distinction the representation exists for.

        A Tenant ASSIGNED an empty set has declared it operates in no mapped
        jurisdiction. That is a statement, and it does establish
        non-applicability — unlike never having been assigned at all.
        """
        _classifications(db)
        ready.jurisdiction_evidence(db, organization_id=ORG, countries=())
        result = _assess(db)
        assert result.gate(GateId.DPIA).status is GateStatus.NOT_APPLICABLE
        assert result.gate(GateId.CONSULTATION_ATTESTATION).status \
            is GateStatus.NOT_APPLICABLE
        # But the unconditional gate is untouched by jurisdiction.
        assert result.gate(GateId.WORKER_NOTICE).status \
            is GateStatus.EVIDENCE_MISSING

    def test_the_uk_reaches_the_dpia_gate_without_being_eu(self, db):
        """Owner ruling D1: C. §40.2's substantive requirement is EU; §44.3's
        rollout gate is EU/UK. A UK-only Tenant is inside the second and not
        the first, and a single scope would have made that unsayable."""
        _classifications(db)
        ready.jurisdiction_evidence(db, organization_id=ORG,
                                    countries=(ready.UNITED_KINGDOM,))
        result = _assess(db)
        dpia = result.gate(GateId.DPIA)
        assert dpia.status is GateStatus.EVIDENCE_MISSING
        assert dpia.evidence["is_uk"] is True
        assert dpia.evidence["is_eu"] is False
        # GB is not in §40.4's named consultation set.
        assert result.gate(GateId.CONSULTATION_ATTESTATION).status \
            is GateStatus.NOT_APPLICABLE

    def test_recorded_evidence_satisfies_each_gate(self, db):
        """The gates must be SATISFIABLE. Without this, every assertion above
        would pass against a checker that always said no."""
        ready.make_enablement_ready(db, organization_id=ORG,
                                    admin_user_id=ADMIN)
        result = _assess(db, shadow=_shadow_evidence(20))
        for gate_id in COMPLIANCE_GATES:
            assert result.gate(gate_id).status is GateStatus.SATISFIED, gate_id
        assert result.verdict == "ENABLEMENT_READY"

    def test_evidence_recorded_as_not_required_does_not_satisfy_an_applicable_gate(self, db):
        """A Tenant does not overrule the platform-governed mapping by
        recording an exemption for itself."""
        from app.compliance_evidence import EvidenceStatus, record_dpia_evidence

        _classifications(db)
        ready.jurisdiction_evidence(db, organization_id=ORG,
                                    countries=(ready.GERMANY,))
        record_dpia_evidence(db, organization_id=ORG, actor=_admin_actor(),
                             effective_from=date(2020, 1, 1),
                             status=EvidenceStatus.NOT_REQUIRED)
        db.commit()
        gate = _assess(db).gate(GateId.DPIA)
        assert gate.status is GateStatus.UNSATISFIED
        assert "NOT_REQUIRED" in gate.reason

    def test_lapsed_evidence_does_not_satisfy_the_gate(self, db):
        from app.compliance_evidence import record_dpia_evidence

        _classifications(db)
        ready.jurisdiction_evidence(db, organization_id=ORG,
                                    countries=(ready.GERMANY,))
        record_dpia_evidence(db, organization_id=ORG, actor=_admin_actor(),
                             effective_from=date(2020, 1, 1),
                             valid_until=date(2026, 1, 1))
        db.commit()
        gate = _assess(db).gate(GateId.DPIA)
        assert gate.status is GateStatus.UNSATISFIED
        assert "lapsed" in gate.reason

    def test_future_dated_evidence_is_not_yet_in_force(self, db):
        """A confirmation dated next month does not satisfy a gate today."""
        from app.compliance_evidence import record_worker_notice

        record_worker_notice(db, organization_id=ORG, actor=_admin_actor(),
                             effective_from=date(2099, 1, 1))
        db.commit()
        assert _assess(db).gate(GateId.WORKER_NOTICE).status \
            is GateStatus.EVIDENCE_MISSING

    def test_the_worker_notice_gate_is_unconditional(self, db):
        """Ruling D4/E4: §44.3 attaches no condition to this row, so no
        jurisdiction can make it NOT_APPLICABLE."""
        _classifications(db)
        for countries in ((), (ready.GERMANY,), (ready.UNITED_KINGDOM,)):
            ready.jurisdiction_evidence(db, organization_id=ORG,
                                        countries=countries,
                                        effective_from=date(2020, 1, 1)
                                        if not countries else date(2021, 1, 1)
                                        if countries == (ready.GERMANY,)
                                        else date(2022, 1, 1))
            assert _assess(db).gate(GateId.WORKER_NOTICE).status \
                is not GateStatus.NOT_APPLICABLE

    def test_no_worker_level_notice_record_was_created(self, db):
        """Owner ruling D4 authorized a TENANT-level confirmation and forbade
        Worker-level notice and acknowledgement records.

        The store now exists — it was authorized — so the guard that mattered
        is no longer "does a table exist" but "is any of this per-Worker".
        """
        from app.compliance_evidence.models import EcoWorkerNoticeConfirmation

        columns = {c.name for c in EcoWorkerNoticeConfirmation.__table__.columns}
        # The confirming ADMIN is attributed; no Worker is.
        assert columns & {"user_id", "worker_id", "employee_id",
                          "acknowledged_at", "acknowledged_by"} == set()
        assert "actor_user_id" in columns

        # And the gate never reads a roster (E4).
        tree = ast.parse((PACKAGE / "service.py").read_text(encoding="utf-8"))
        guard = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "_assess_worker_notice")
        names = {n.id for n in ast.walk(guard) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(guard)
                  if isinstance(n, ast.Attribute)}
        assert not (names & {"User", "Employee", "users", "employees"}), names


# ═════════════════════════════════════════════════════════════════════════════
# 5. Tenant scoping
# ═════════════════════════════════════════════════════════════════════════════
class TestTenantIsolation:
    def test_tenant_a_evidence_never_satisfies_tenant_b(self, db):
        _all_technical_evidence(db, organization_id=ORG)
        other = _assess(db, organization_id=OTHER_ORG)
        assert other.verdict == "ENABLEMENT_NOT_READY"
        assert not any(other.gate(g).passes for g in TECHNICAL_GATES)

    def test_each_tenant_is_assessed_on_its_own_evidence(self, db):
        _all_technical_evidence(db, organization_id=ORG)
        _workweek_evidence(db, organization_id=OTHER_ORG)
        mine = _assess(db, organization_id=ORG)
        theirs = _assess(db, organization_id=OTHER_ORG)
        assert mine.gate(GateId.ADMINISTRATIVE_RECIPIENT).status \
            is GateStatus.SATISFIED
        assert theirs.gate(GateId.ADMINISTRATIVE_RECIPIENT).status \
            is GateStatus.EVIDENCE_MISSING
        assert theirs.gate(GateId.WORKWEEK_TIMEZONE_ATTRIBUTION).status \
            is GateStatus.SATISFIED

    def test_an_observation_is_not_tenant_scoped_by_the_checker(self, db):
        """Owner ruling D6 moved this boundary, and the move is worth naming.

        The observation is now supplied by the caller, so the CALLER is what
        keeps one Tenant's shadow period out of another's assessment — and
        ``rollout.transition`` builds it from the row it just read for that
        one organization. What this suite can still prove is that the checker
        adds no Tenant of its own: given an observation, it reports on it, and
        given none it reports EVIDENCE_MISSING for whoever asked.
        """
        assert _assess(db, organization_id=OTHER_ORG).gate(
            GateId.SHADOW_PERIOD).status is GateStatus.EVIDENCE_MISSING
        assert _assess(db, organization_id=ORG,
                       shadow=_shadow_evidence(30)).gate(
            GateId.SHADOW_PERIOD).status is GateStatus.SATISFIED


# ═════════════════════════════════════════════════════════════════════════════
# 6. Read-only — proved, not asserted in prose
# ═════════════════════════════════════════════════════════════════════════════
class TestReadOnly:
    def test_a_full_assessment_emits_no_write_statement(self, db,
                                                        gate_engine):
        """Engine-level instrumentation. Every statement the assessment issues
        is inspected; any INSERT, UPDATE or DELETE fails the test."""
        _all_technical_evidence(db)
        statements = []

        @sa_event.listens_for(gate_engine, "before_cursor_execute")
        def _record(conn, cursor, statement, params, context, executemany):
            statements.append(statement.strip().split()[0].upper())

        try:
            _assess(db)
        finally:
            sa_event.remove(gate_engine, "before_cursor_execute", _record)

        assert statements, "the assessment issued no statements at all"
        assert set(statements) <= {"SELECT", "PRAGMA"}, sorted(set(statements))

    def test_the_assessment_changes_no_row_count(self, db):
        _all_technical_evidence(db)
        tables = ("rollout_control", "eco_recipient_designation",
                  "config_version", "config_value", "policy_version",
                  "break_treatment_profile", "work_day_ledger",
                  "eco_day_record", "app_settings", "org_settings")

        def counts():
            return {t: db.execute(sa.text(
                f"SELECT count(*) FROM {t}")).scalar() for t in tables}  # noqa: S608

        before = counts()
        _assess(db)
        _assess(db)
        assert counts() == before

    def test_the_rollout_state_is_untouched(self, db):
        """A real control row, assessed — and unchanged afterwards.

        The checker cannot reach this row any more, which is the stronger
        property; this asserts the observable consequence as well, because a
        future edit could reintroduce the reach.
        """
        db.add(RolloutControl(
            organization_id=ORG, control_key=CONTROL_EARLY_CLOCK_OUT_V2,
            state=RolloutState.SHADOW.value, state_version=1,
            last_transition_by="test",
            shadow_entered_at=NOW - timedelta(days=20)))
        db.commit()
        row = db.query(RolloutControl).filter(
            RolloutControl.organization_id == ORG).one()
        before = (row.state, row.state_version, row.shadow_entered_at)
        _assess(db, shadow=_shadow_evidence(20))
        db.expire_all()
        row = db.query(RolloutControl).filter(
            RolloutControl.organization_id == ORG).one()
        assert (row.state, row.state_version, row.shadow_entered_at) == before

    def test_the_package_calls_no_writer(self):
        """Structural, by AST call scan: the checker must not be able to write
        even if a future edit tried."""
        banned = {
            "transition", "add", "flush", "commit", "delete", "merge",
            "capture_version", "capture_policy_version", "capture_threshold",
            "raise_day_administrator_alert", "record_threshold_notification",
            "run_eco_calculation_pass", "run_eco_dispatch_pass",
            "produce_and_seal_work_day", "evaluate_and_record_day",
        }
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                      for n in ast.walk(tree) if isinstance(n, ast.Call)}
            offenders = called & banned
            assert not offenders, f"{path.name} calls {sorted(offenders)}"

    def test_the_package_declares_no_table_and_no_migration(self):
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     for t in n.targets if isinstance(t, ast.Name)}
            assert "__tablename__" not in names
        versions = BACKEND / "alembic" / "versions"
        offenders = [p.name for p in versions.glob("*.py")
                     if "enablement_gates" in p.read_text(encoding="utf-8",
                                                          errors="ignore")]
        assert offenders == []

    def test_the_transition_composes_the_checker_and_not_the_reverse(self):
        """Owner ruling D6 REVERSED this.

        The checker used to be forbidden from gating a transition. It now is
        gated ON — by ``rollout``, which composes it. What must stay true is
        the DIRECTION: ``rollout`` imports the checker, and the checker imports
        nothing from ``rollout``. A cycle here would mean either component
        could be the one deciding, and only one of them owns the state.
        """
        def _modules(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            out = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    out.add(node.module)
                elif isinstance(node, ast.Import):
                    out.update(a.name for a in node.names)
            return out

        composes = {p.name for p in (BACKEND / "app" / "rollout").glob("*.py")
                    if any("enablement_gates" in m for m in _modules(p))}
        assert composes == {"service.py"}, composes

        for path in sorted(PACKAGE.glob("*.py")):
            assert not any(m.startswith("app.rollout") for m in _modules(path)), \
                path.name

    def test_no_route_or_command_exposes_it(self):
        """No HTTP surface and no operational command in this increment."""
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for banned in ("APIRouter", "@router.", "fastapi", "argparse"):
                assert banned not in source, f"{path.name}: {banned}"
        assert not list(BACKEND.glob("run_enablement*.py"))


# ═════════════════════════════════════════════════════════════════════════════
# Helper
# ═════════════════════════════════════════════════════════════════════════════
def _emitted_string_literals(path: pathlib.Path) -> set:
    """Every string literal a module EMITS, docstrings excluded.

    Docstrings quote §44.3 and name values in order to disclaim them; a plain
    text scan would flag the sentence that states the rule.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) \
                and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant):
            docstrings.add(id(body[0].value))
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings}
