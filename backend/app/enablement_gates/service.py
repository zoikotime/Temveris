"""
The §44.3 enablement-gate assessment — read-only, per Tenant.
=============================================================

    assess_enablement_gates(db, organization_id=..., as_of=...) -> EnablementReadiness

READ-ONLY, STRUCTURALLY
-----------------------
This module issues SELECTs and nothing else. It does not write a row, flush,
commit, transition a rollout, create a designation, dispatch a notification or
touch a calculation artifact. ``tests/test_enablement_gates.py`` proves it by
instrumenting the engine and asserting that no INSERT, UPDATE or DELETE is
emitted during a full assessment — not by taking this paragraph's word for it.

It does not enable anything ITSELF. It answers "does this Tenant have evidence
satisfying every required §44.3 gate?" and stops. Under owner ruling D6,
``rollout.transition`` now composes it and refuses to promote SHADOW -> ENABLED
against a NOT_READY verdict — but the refusing is done there, by the component
that owns the state. Nothing here writes, and nothing here decides.

EVIDENCE COMES FROM THE COMPONENT THAT OWNS IT
-----------------------------------------------
Every gate reads the authoritative source rather than re-deriving the answer:
the workweek resolver, the versioned-configuration provider, the PolicyVersion
store, the break-treatment profile, the recipient-designation model, the
governed jurisdiction mapping, the three compliance-evidence stores and —
through a port rather than an import — the durable rollout control. The checker
forms no opinion any of them could disagree with.

WHAT "CAPABILITY EXISTS" IS WORTH HERE
---------------------------------------
Nothing. A gate is satisfied by TENANT-SPECIFIC evidence, never by the feature
being implemented. The three compliance gates now read real per-Tenant records,
and a Tenant with none still reports ``EVIDENCE_MISSING`` — existence of the
tables satisfies nobody.

"CANNOT ESTABLISH APPLICABILITY" IS NOT "DOES NOT APPLY"
---------------------------------------------------------
A Tenant with no jurisdiction assignment gets ``EVIDENCE_MISSING`` on both
conditional gates, never ``NOT_APPLICABLE``. Under ruling B9 the two are
genuinely different: a Tenant that was ASSIGNED an empty jurisdiction set has
declared it operates in none, and that reaches ``NOT_APPLICABLE``; a Tenant
nobody ever assigned has told us nothing, and waiving a compliance gate on
silence is how a control gets enabled in a jurisdiction that forbids it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.compliance_evidence.outcomes import EvidenceStatus

from .outcomes import (
    GATE_ORDER, EnablementReadiness, GateId, GateResult, GateStatus,
)
from .ports import ShadowObservation


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Workweek, timezone and shift attribution
# ═════════════════════════════════════════════════════════════════════════════
def _assess_workweek(db: Session, organization_id: int) -> GateResult:
    """§44.3: "Tenant workweek start, timezone and shift attribution rule set".

    Three parts, and they are NOT the same kind of thing:

    * the workweek start is Tenant configuration, resolved by the strict
      MODEL-WORKWEEK resolver, which fails closed when it is unset (§12.1);
    * the timezone is Tenant configuration on ``OrgSettings``;
    * the shift attribution rule is NOT configurable. ``SUPPORTED_ATTRIBUTION_
      RULES`` holds exactly ``START_DATE``, and the ledger writer stamps it on
      every row rather than accepting one from a caller (W4-4's shape). So
      there is no Tenant setting to check — what CAN be checked is that no
      sealed row carries anything else, which is Tenant evidence rather than a
      capability claim.
    """
    from app.work_ledger.attribution import SUPPORTED_ATTRIBUTION_RULES
    from app.work_ledger.models import WorkDayLedger
    from app.work_ledger.workweek import (
        WorkweekBoundaryUnset, resolve_week_start_day,
    )
    import database.models as dbm
    import sqlalchemy as sa

    evidence: Dict[str, Any] = {}
    missing: List[str] = []

    try:
        evidence["week_start_day"] = resolve_week_start_day(
            db, organization_id=organization_id)
    except WorkweekBoundaryUnset:
        missing.append("schedule.week_start_day")
    except Exception as exc:  # malformed or wrongly scoped, per W4
        return GateResult(
            GateId.WORKWEEK_TIMEZONE_ATTRIBUTION, GateStatus.EVIDENCE_INVALID,
            f"the workweek boundary is recorded but unusable: "
            f"{type(exc).__name__}", evidence=evidence)

    timezone_iana = db.execute(
        sa.select(dbm.OrgSettings.default_timezone)
        .where(dbm.OrgSettings.organization_id == organization_id)).scalar()
    if timezone_iana:
        evidence["default_timezone"] = timezone_iana
    else:
        missing.append("org_settings.default_timezone")

    # Tenant evidence for the attribution rule: what the sealed rows carry.
    rules = set(db.execute(
        sa.select(WorkDayLedger.shift_attribution_rule)
        .where(WorkDayLedger.organization_id == organization_id)
        .distinct()).scalars().all())
    evidence["shift_attribution_rules_in_ledger"] = sorted(rules)
    evidence["supported_attribution_rules"] = sorted(SUPPORTED_ATTRIBUTION_RULES)
    if rules - set(SUPPORTED_ATTRIBUTION_RULES):
        return GateResult(
            GateId.WORKWEEK_TIMEZONE_ATTRIBUTION, GateStatus.EVIDENCE_INVALID,
            "sealed rows carry an attribution rule outside the supported set",
            evidence=evidence)

    if missing:
        return GateResult(
            GateId.WORKWEEK_TIMEZONE_ATTRIBUTION, GateStatus.EVIDENCE_MISSING,
            "the Tenant has not configured every part of the workweek "
            "boundary", evidence=evidence, missing_evidence=tuple(missing))
    return GateResult(
        GateId.WORKWEEK_TIMEZONE_ATTRIBUTION, GateStatus.SATISFIED,
        "workweek start and timezone are configured, and the platform's one "
        "supported attribution rule is what the ledger carries",
        evidence=evidence)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Requirement fallback at rank 6
# ═════════════════════════════════════════════════════════════════════════════
def _assess_requirement_fallback(db: Session, organization_id: int,
                                 as_of: date) -> GateResult:
    """§44.3: "Tenant fallback policy exists at rank 6 (§10)".

    Read through the versioned-configuration provider, so the answer is the one
    requirement resolution itself would get. A rank-6 candidate that exists but
    carries a non-positive value is EVIDENCE_INVALID, because Step 5 refuses
    zero as a requirement rather than resolving to it.
    """
    # The vocabulary, not the capture path: §44.3 names "rank 6 (§10)" and
    # the Tenant-fallback tier, and ``outcomes.py`` is where those two
    # constants are defined. Importing them beats restating 6 here, which is
    # how a gate and the capture that satisfies it drift apart.
    from app.config_first_capture.outcomes import (
        REQUIREMENT_RANK, REQUIREMENT_TIER,
    )
    from app.config_versioning import DatabaseConfigVersionProvider

    provider = DatabaseConfigVersionProvider(db)
    version = provider.version_as_of(organization_id=organization_id,
                                     as_of=as_of)
    if version is None:
        return GateResult(
            GateId.REQUIREMENT_FALLBACK, GateStatus.EVIDENCE_MISSING,
            f"no configuration version is in force for {as_of.isoformat()}",
            missing_evidence=("config_version",))

    evidence: Dict[str, Any] = {"config_version": getattr(
        version, "version_key", str(version))}
    candidates = provider.requirement_candidates(
        organization_id=organization_id, user_id=0, as_of=as_of,
        version=version)
    fallback = [c for c in candidates
                if c.rank == REQUIREMENT_RANK
                and c.tier == REQUIREMENT_TIER]
    evidence["rank"] = REQUIREMENT_RANK
    evidence["fallback_candidates"] = len(fallback)

    if not fallback:
        return GateResult(
            GateId.REQUIREMENT_FALLBACK, GateStatus.UNSATISFIED,
            f"the version in force carries no rank-{REQUIREMENT_RANK} Tenant "
            f"fallback requirement", evidence=evidence)
    minutes = [c.minutes for c in fallback]
    evidence["fallback_minutes"] = minutes
    if any(m is None or m <= 0 for m in minutes):
        return GateResult(
            GateId.REQUIREMENT_FALLBACK, GateStatus.EVIDENCE_INVALID,
            "the rank-6 fallback is recorded with a non-positive value, which "
            "requirement resolution refuses rather than resolving to",
            evidence=evidence)
    return GateResult(
        GateId.REQUIREMENT_FALLBACK, GateStatus.SATISFIED,
        f"a rank-{REQUIREMENT_RANK} Tenant fallback requirement is in force",
        evidence=evidence)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Netting decision
# ═════════════════════════════════════════════════════════════════════════════
def _assess_netting(db: Session, organization_id: int,
                    as_of: date) -> GateResult:
    """§44.3: "Netting decision made explicitly — Not accepted as a default".

    A LIMITATION THIS GATE REPORTS RATHER THAN HIDES
    ------------------------------------------------
    ``PolicyVersion.netting_mode`` is NOT NULL, so every captured policy
    carries a mode and "never decided" is unrepresentable. The data therefore
    cannot distinguish a mode an administrator CHOSE from one an onboarding
    flow pre-selected — which is precisely §45's OD-02, still unruled.

    So this gate verifies what is verifiable: a policy version is in force and
    carries a mode from the closed §15 vocabulary. It does NOT claim to have
    verified explicitness, and says so in its reason rather than implying a
    check it cannot perform.
    """
    from app.policy_version import UNRESOLVED_OUTCOMES, resolve_policy_as_of
    from app.policy_version.vocabulary import NettingMode

    resolved = resolve_policy_as_of(db, organization_id=organization_id,
                                    as_of=as_of)
    if resolved.outcome in UNRESOLVED_OUTCOMES:
        return GateResult(
            GateId.NETTING_DECISION, GateStatus.EVIDENCE_MISSING,
            f"no usable policy version is in force for {as_of.isoformat()} "
            f"({resolved.outcome.value}), so no netting decision has been "
            f"recorded", evidence={"outcome": resolved.outcome.value},
            missing_evidence=("policy_version",))

    mode = resolved.netting_mode
    evidence = {"policy_version": resolved.policy_version,
                "netting_mode": mode}
    known = {m.value for m in NettingMode}
    if mode not in known:
        return GateResult(
            GateId.NETTING_DECISION, GateStatus.EVIDENCE_INVALID,
            f"the recorded netting mode {mode!r} is outside §15's closed "
            f"vocabulary {sorted(known)}", evidence=evidence)
    return GateResult(
        GateId.NETTING_DECISION, GateStatus.SATISFIED,
        f"a policy version in force records netting mode {mode}. NOTE: the "
        f"data cannot distinguish an explicit choice from an accepted default "
        f"— §45 OD-02 is unruled, so §44.3's 'not accepted as a default' is "
        f"not independently verifiable here", evidence=evidence)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Break treatment
# ═════════════════════════════════════════════════════════════════════════════
def _assess_break_treatment(db: Session, organization_id: int,
                            as_of: date) -> GateResult:
    """§44.3: "BRK_M and BRK_R treatment set explicitly".

    The §13.2 artifact is ``BreakTreatmentProfile``, whose ``meal_qualifies``
    and ``rest_qualifies`` are both NOT NULL — so a profile existing IS both
    treatments being set. Nothing is synthesised here and no profile is
    created; absence is reported as absence.
    """
    import sqlalchemy as sa
    from app.break_treatment.models import BreakTreatmentProfile as Profile

    row = db.execute(
        sa.select(Profile)
        .where(Profile.organization_id == organization_id,
               Profile.effective_from <= as_of)
        .order_by(Profile.effective_from.desc())
        .limit(1)).scalars().first()
    if row is None:
        return GateResult(
            GateId.BREAK_TREATMENT, GateStatus.EVIDENCE_MISSING,
            f"no break-treatment profile is in force for {as_of.isoformat()}",
            missing_evidence=("break_treatment_profile",))
    return GateResult(
        GateId.BREAK_TREATMENT, GateStatus.SATISFIED,
        "a break-treatment profile in force sets both BRK_M and BRK_R",
        evidence={"profile_ref": row.profile_ref,
                  "effective_from": row.effective_from.isoformat(),
                  "meal_qualifies": bool(row.meal_qualifies),
                  "rest_qualifies": bool(row.rest_qualifies),
                  "rest_qualifying_cap_minutes":
                      row.rest_qualifying_cap_minutes})


# ═════════════════════════════════════════════════════════════════════════════
# 5. Administrative recipient
# ═════════════════════════════════════════════════════════════════════════════
def _assess_recipient(db: Session, organization_id: int) -> GateResult:
    """§44.3 / §30.1: "at least one administrative recipient".

    PRIMARY ADMIN is authoritative and there is NO substitution (owner ruling
    D-1): an unresolved recipient is RECORDED, never replaced by a manager, an
    HR admin or the first administrator found. So this gate counts PRIMARY
    ADMIN designations and nothing else — counting any other audience would be
    the substitution the ruling forbids, performed by the readiness check.

    Nothing is designated here. The checker reads the model; it never writes it.
    """
    import sqlalchemy as sa
    from app.recipient_authority import EcoRecipientDesignation

    designated = db.execute(
        sa.select(EcoRecipientDesignation.user_id)
        .where(EcoRecipientDesignation.organization_id == organization_id,
               EcoRecipientDesignation.audience == "PRIMARY_ADMIN")).scalars(
                   ).all()
    evidence = {"audience": "PRIMARY_ADMIN",
                "designation_count": len(designated)}
    if not designated:
        return GateResult(
            GateId.ADMINISTRATIVE_RECIPIENT, GateStatus.EVIDENCE_MISSING,
            "no PRIMARY ADMIN is designated for this Tenant, and no other "
            "audience may be substituted (D-1)", evidence=evidence,
            missing_evidence=("eco_recipient_designation[PRIMARY_ADMIN]",))
    return GateResult(
        GateId.ADMINISTRATIVE_RECIPIENT, GateStatus.SATISFIED,
        f"{len(designated)} PRIMARY ADMIN recipient(s) designated",
        evidence=evidence)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Shadow period
# ═════════════════════════════════════════════════════════════════════════════
def _assess_shadow_period(shadow: Optional[ShadowObservation],
                          now: datetime) -> GateResult:
    """§44.3 / §44.2: "Shadow period completed — two complete workweeks".

    OPERATIONAL EVIDENCE ONLY
    -------------------------
    The ONLY source read here is the durable rollout control: a real
    ``shadow_entered_at`` on a real row, and real elapsed time measured against
    it. The replay harness's fourteen-day fixture is NOT evidence for this
    gate and cannot be — it observes nothing, its dates are fixture dates, and
    it never creates a rollout row. A harness that could satisfy this gate
    would make the observation period skippable, which is the one thing §44.2
    exists to prevent.

    "Two complete workweeks" is read through the observation's own
    ``min_observation_days``, which encodes it as 14 calendar days and is the
    same number ``rollout.transition`` enforces. Reading it from the supplied
    observation rather than restating 14 here is what stops the gate and the
    transition rule from drifting apart.

    SUPPLIED, NOT FETCHED (owner ruling D6)
    ---------------------------------------
    The observation arrives as a ``ShadowObservation`` from whoever holds it.
    This module no longer imports ``app.rollout``, because ``app.rollout`` now
    composes this checker to gate its own promotion, and the dependency has to
    run one way. A caller that supplies nothing gets EVIDENCE_MISSING: the gate
    reports that it was not given the fact, and never assumes a shadow period
    it was not shown.
    """
    if shadow is None:
        return GateResult(
            GateId.SHADOW_PERIOD, GateStatus.EVIDENCE_MISSING,
            "no shadow observation was supplied to this assessment, so the "
            "observation period cannot be measured",
            missing_evidence=("shadow_observation",))
    status = shadow
    if not status.exists:
        return GateResult(
            GateId.SHADOW_PERIOD, GateStatus.EVIDENCE_MISSING,
            "no rollout control exists for this Tenant, so no shadow period "
            "has been observed", missing_evidence=("rollout_control",))

    evidence: Dict[str, Any] = {
        "rollout_state": status.state,
        "min_observation_days": status.min_observation_days,
        "shadow_entered_at": (status.shadow_entered_at.isoformat()
                              if status.shadow_entered_at else None),
    }

    if status.state == "OFF":
        return GateResult(
            GateId.SHADOW_PERIOD, GateStatus.UNSATISFIED,
            "the control is OFF: the shadow period has not begun",
            evidence=evidence)

    if status.shadow_entered_at is None:
        return GateResult(
            GateId.SHADOW_PERIOD, GateStatus.EVIDENCE_INVALID,
            f"the control is {status.state} but carries no "
            f"shadow_entered_at, so the observation period cannot be measured",
            evidence=evidence)

    elapsed = status.days_elapsed(now=now)
    evidence["observation_days_elapsed"] = (
        None if elapsed is None else round(float(elapsed), 2))
    if elapsed is None or elapsed < status.min_observation_days:
        return GateResult(
            GateId.SHADOW_PERIOD, GateStatus.UNSATISFIED,
            f"observed for {evidence['observation_days_elapsed']} of the "
            f"required {status.min_observation_days} days",
            evidence=evidence)
    return GateResult(
        GateId.SHADOW_PERIOD, GateStatus.SATISFIED,
        f"observed for {evidence['observation_days_elapsed']} days, meeting "
        f"the required {status.min_observation_days}", evidence=evidence)


# ═════════════════════════════════════════════════════════════════════════════
# 7-9. Compliance evidence
# ═════════════════════════════════════════════════════════════════════════════
def _jurisdiction(db: Session, organization_id: int, as_of: date):
    """The Tenant's governed jurisdiction resolution for a date.

    Wrapped so all three compliance gates ask the question the same way, and so
    the ONE place that decides what "unassigned" means lives here rather than
    being re-derived per gate.
    """
    from app.jurisdiction import resolve

    return resolve(db, organization_id=organization_id, as_of=as_of)


def _unresolved(gate: GateId, juris, evidence) -> GateResult:
    """An assigned country the platform has no classification for.

    Distinct from the declared-empty set: that is a Tenant STATING it operates
    in no mapped jurisdiction, and it reaches NOT_APPLICABLE. This is the
    platform not having said what a country is, and the missing answer might
    have been "EU". Reporting NOT_APPLICABLE here would waive the gate on the
    platform's own incomplete reference data.
    """
    missing = tuple(f"jurisdiction_classification[{c}]"
                    for c in juris.unclassified)
    return GateResult(
        gate, GateStatus.EVIDENCE_MISSING,
        f"the governed mapping has no classification in force for "
        f"{', '.join(juris.unclassified)} on this date, so whether this gate "
        f"applies cannot be established. An unclassified country is an "
        f"unanswered question, not an answer of 'no'.",
        evidence=evidence, missing_evidence=missing)


_UNASSIGNED = (
    "no jurisdiction has ever been assigned to this Tenant, so whether this "
    "gate applies cannot be established. An unassigned Tenant is not an "
    "out-of-scope Tenant: absence of an assignment is absence of information, "
    "and reporting NOT_APPLICABLE from it would silently waive the gate."
)


def _window(row, as_of: date, attr: str):
    """(has_lapsed, end_date) for a record that may carry an end date."""
    end = getattr(row, attr, None)
    return (end is not None and end < as_of), end


def _assess_dpia(db: Session, organization_id: int, as_of: date) -> GateResult:
    """§44.3: "Required for EU/UK Tenants". §40.2 states the substantive
    requirement for EU Tenants.

    Owner ruling D1: C — BOTH apply, and they are not the same set. The GATE
    fires on EU or UK; §40.2's substantive obligation is EU only. The record
    carries both answers (ruling C: B); this gate reads the gate one.
    """
    from app.compliance_evidence import dpia_in_force

    juris = _jurisdiction(db, organization_id, as_of)
    if not juris.is_assigned:
        return GateResult(GateId.DPIA, GateStatus.EVIDENCE_MISSING, _UNASSIGNED,
                          missing_evidence=("tenant_jurisdiction_assignment",))

    evidence: Dict[str, Any] = {
        "jurisdictions": list(juris.countries),
        "is_eu": juris.is_eu, "is_uk": juris.is_uk,
        "assignment_id": juris.assignment_id,
        "assignment_source": juris.source,
        "declared_empty": juris.is_declared_empty,
        "unclassified": list(juris.unclassified),
    }

    if not juris.applicability_resolved:
        return _unresolved(GateId.DPIA, juris, evidence)

    if not juris.dpia_gate_applies:
        return GateResult(
            GateId.DPIA, GateStatus.NOT_APPLICABLE,
            "the governed mapping places this Tenant in no EU or UK "
            "jurisdiction on this date, so §44.3's DPIA gate does not apply",
            evidence=evidence)

    row = dpia_in_force(db, organization_id=organization_id, as_of=as_of)
    if row is None:
        return GateResult(
            GateId.DPIA, GateStatus.EVIDENCE_MISSING,
            "this Tenant is EU/UK on this date and no DPIA evidence is in "
            "force", evidence=evidence,
            missing_evidence=("eco_dpia_evidence",))

    evidence.update(dpia_evidence_id=row.id, status=row.status,
                    effective_from=row.effective_from.isoformat())
    lapsed, valid_until = _window(row, as_of, "valid_until")
    if valid_until is not None:
        evidence["valid_until"] = valid_until.isoformat()

    if row.status != EvidenceStatus.COMPLETED.value:
        return GateResult(
            GateId.DPIA, GateStatus.UNSATISFIED,
            f"the DPIA record in force says {row.status}, but the governed "
            f"mapping places this Tenant in an EU/UK jurisdiction. The mapping "
            f"is platform-governed, and a Tenant does not overrule it by "
            f"recording an exemption for itself.", evidence=evidence)
    if lapsed:
        return GateResult(
            GateId.DPIA, GateStatus.UNSATISFIED,
            f"the DPIA in force lapsed on {valid_until.isoformat()}",
            evidence=evidence)
    return GateResult(GateId.DPIA, GateStatus.SATISFIED,
                      "a completed DPIA is in force", evidence=evidence)


def _assess_consultation(db: Session, organization_id: int,
                         as_of: date) -> GateResult:
    """§40.4 / §44.3: the employee-representative consultation attestation.

    Temveris RECORDS the attestation and does not assess its adequacy. This
    gate checks that one exists, is in force and has not lapsed, and forms no
    view whatsoever on whether the consultation itself was sufficient.
    """
    from app.compliance_evidence import consultation_in_force

    juris = _jurisdiction(db, organization_id, as_of)
    if not juris.is_assigned:
        return GateResult(
            GateId.CONSULTATION_ATTESTATION, GateStatus.EVIDENCE_MISSING,
            _UNASSIGNED,
            missing_evidence=("tenant_jurisdiction_assignment",))

    evidence: Dict[str, Any] = {
        "jurisdictions": list(juris.countries),
        "requires_consultation": juris.requires_consultation,
        "assignment_id": juris.assignment_id,
        "declared_empty": juris.is_declared_empty,
        "unclassified": list(juris.unclassified),
    }

    if not juris.applicability_resolved:
        return _unresolved(GateId.CONSULTATION_ATTESTATION, juris, evidence)

    if not juris.requires_consultation:
        return GateResult(
            GateId.CONSULTATION_ATTESTATION, GateStatus.NOT_APPLICABLE,
            "no jurisdiction assigned to this Tenant on this date is marked "
            "as requiring employee-representative consultation",
            evidence=evidence)

    row = consultation_in_force(db, organization_id=organization_id,
                                as_of=as_of)
    if row is None:
        return GateResult(
            GateId.CONSULTATION_ATTESTATION, GateStatus.EVIDENCE_MISSING,
            "a jurisdiction assigned to this Tenant requires consultation and "
            "no attestation is in force", evidence=evidence,
            missing_evidence=("eco_consultation_attestation",))

    evidence.update(attestation_id=row.id, status=row.status,
                    effective_from=row.effective_from.isoformat())
    lapsed, expires_on = _window(row, as_of, "expires_on")
    if expires_on is not None:
        evidence["expires_on"] = expires_on.isoformat()

    if row.status != EvidenceStatus.COMPLETED.value:
        return GateResult(
            GateId.CONSULTATION_ATTESTATION, GateStatus.UNSATISFIED,
            f"the attestation in force says {row.status}, but a jurisdiction "
            f"assigned to this Tenant is marked as requiring consultation",
            evidence=evidence)
    if lapsed:
        return GateResult(
            GateId.CONSULTATION_ATTESTATION, GateStatus.UNSATISFIED,
            f"the attestation in force expired on {expires_on.isoformat()}",
            evidence=evidence)
    return GateResult(GateId.CONSULTATION_ATTESTATION, GateStatus.SATISFIED,
                      "a consultation attestation is in force",
                      evidence=evidence)


def _assess_worker_notice(db: Session, organization_id: int,
                          as_of: date) -> GateResult:
    """§44.3: "Tenant confirms Workers have been informed the control is
    active."

    UNCONDITIONAL, AND TENANT-LEVEL (rulings D4, E4)
    ------------------------------------------------
    No jurisdiction is consulted: §44.3 attaches no condition to this row, so
    this gate can never be NOT_APPLICABLE. It also never reads a roster or a
    per-Worker record, because D4 forbids Worker-level notice and
    acknowledgement records and E4 keeps the gate Tenant-level.

    Rulings E1/E2/E3: a confirmation is permanent from its effective date until
    a later one supersedes it. New joiners are covered, and a policy change
    does not invalidate it — so there is nothing here to compare against a
    roster or against a PolicyVersion.
    """
    from app.compliance_evidence import worker_notice_in_force

    row = worker_notice_in_force(db, organization_id=organization_id,
                                 as_of=as_of)
    if row is None:
        return GateResult(
            GateId.WORKER_NOTICE, GateStatus.EVIDENCE_MISSING,
            "no Tenant confirmation that Workers have been informed is in "
            "force", missing_evidence=("eco_worker_notice_confirmation",))

    evidence: Dict[str, Any] = {
        "confirmation_id": row.id, "status": row.status,
        "effective_from": row.effective_from.isoformat(),
    }
    if row.status != EvidenceStatus.COMPLETED.value:
        return GateResult(
            GateId.WORKER_NOTICE, GateStatus.UNSATISFIED,
            f"the confirmation in force says {row.status}, but §44.3 attaches "
            f"no condition to this gate: informing Workers is required of "
            f"every Tenant", evidence=evidence)
    return GateResult(GateId.WORKER_NOTICE, GateStatus.SATISFIED,
                      "the Tenant has confirmed Workers were informed",
                      evidence=evidence)


# ═════════════════════════════════════════════════════════════════════════════
# The assessment
# ═════════════════════════════════════════════════════════════════════════════
def assess_enablement_gates(
    db: Session, *, organization_id: int,
    as_of: Optional[date] = None,
    now: Optional[datetime] = None,
    shadow: Optional[ShadowObservation] = None,
) -> EnablementReadiness:
    """Assess §44.3's nine gates for ONE Tenant. Reads only; writes nothing.

    ``as_of`` is the date the "in force" questions are asked about, and ``now``
    the instant the observation window is measured from. Both are parameters so
    an assessment is reproducible rather than dependent on when it ran.

    ``shadow`` is the rollout observation, supplied by the caller that holds it
    (see ``ports``). Omitting it does not skip the gate — the gate reports
    EVIDENCE_MISSING, because "I was not told" is not "it happened".
    """
    if organization_id is None:
        raise ValueError("organization_id is required: a §44.3 assessment is "
                         "per Tenant and has no global form")
    organization_id = int(organization_id)
    now = now or _utcnow()
    as_of = as_of or now.date()

    results = [
        _assess_workweek(db, organization_id),
        _assess_requirement_fallback(db, organization_id, as_of),
        _assess_netting(db, organization_id, as_of),
        _assess_break_treatment(db, organization_id, as_of),
        _assess_recipient(db, organization_id),
        _assess_shadow_period(shadow, now),
        _assess_dpia(db, organization_id, as_of),
        _assess_consultation(db, organization_id, as_of),
        _assess_worker_notice(db, organization_id, as_of),
    ]
    by_gate = {r.gate: r for r in results}
    ordered = tuple(by_gate[gate] for gate in GATE_ORDER)

    ready = all(r.passes for r in ordered if r.blocking)
    return EnablementReadiness(
        organization_id=organization_id, evaluated_at=now,
        verdict="ENABLEMENT_READY" if ready else "ENABLEMENT_NOT_READY",
        gates=ordered)


__all__ = ["assess_enablement_gates"]
