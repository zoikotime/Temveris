"""
Recording and resolving ECO compliance evidence (D2, D3, D4).
=============================================================

THREE RECORDERS, THREE RESOLVERS
--------------------------------
Each recorder takes an authorized actor, refuses anyone else, and writes a new
row that SUPERSEDES the previous one. Each resolver answers "what is in force
on this date", so a gate assessed for a past date gets the past answer.

AUTHORIZATION IS ONE VOCABULARY
-------------------------------
Owner ruling D fixed ``{admin, hr}`` for ECO governance acts. Imported from
``eco_threshold_capture`` rather than restated, so a second copy cannot drift.

NO ADEQUACY JUDGEMENT
---------------------
Nothing here evaluates whether a DPIA was good, whether a consultation was
sufficient, or whether Workers were adequately informed. §44.3 says Temveris
records the attestation and does not assess its adequacy; the same posture
governs all three.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.eco_threshold_capture.outcomes import AUTHORIZED_ROLES

from .models import (
    EcoConsultationAttestation, EcoDpiaEvidence, EcoWorkerNoticeConfirmation,
)
from .outcomes import EvidenceStatus, UnauthorizedEvidenceActor


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _authorize(actor, organization_id: int):
    """One authorization check, shared by all three recorders (ruling D)."""
    role = getattr(actor, "role", None)
    user_id = getattr(actor, "user_id", None)
    organization = getattr(actor, "organization_id", None)

    if role not in AUTHORIZED_ROLES:
        raise UnauthorizedEvidenceActor(
            f"role {role!r} may not record ECO compliance evidence; owner "
            f"ruling D authorises {sorted(AUTHORIZED_ROLES)}")
    if user_id is None:
        raise UnauthorizedEvidenceActor(
            "compliance evidence must be attributable to a user; the actor "
            "carries no user id")
    if organization is not None and int(organization) != int(organization_id):
        raise UnauthorizedEvidenceActor(
            "an actor may only record evidence for their own Tenant")
    return int(user_id), str(role)[:32], str(getattr(actor, "label", role))[:64]


def _prior(db: Session, model, organization_id: int, effective_from: date):
    return db.execute(
        sa.select(model)
        .where(model.organization_id == organization_id,
               model.effective_from < effective_from)
        .order_by(model.effective_from.desc()).limit(1)).scalars().first()


def _in_force(db: Session, model, organization_id: int, as_of: date):
    """The record in force on a date: latest effective_from <= as_of.

    Later records supersede earlier ones, and a record whose effective date is
    in the FUTURE is not yet in force — which is what makes a post-dated
    confirmation unable to satisfy a gate today.
    """
    return db.execute(
        sa.select(model)
        .where(model.organization_id == organization_id,
               model.effective_from <= as_of)
        .order_by(model.effective_from.desc(), model.id.desc())
        .limit(1)).scalars().first()


# ═════════════════════════════════════════════════════════════════════════════
# DPIA (D2)
# ═════════════════════════════════════════════════════════════════════════════
def record_dpia_evidence(
    db: Session, *, organization_id: int, actor, effective_from,
    status: EvidenceStatus = EvidenceStatus.COMPLETED,
    eu_substantive_applies: bool = False, eu_uk_gate_applies: bool = False,
    jurisdiction_scope: Iterable[str] = (), valid_until=None,
    external_reference: Optional[str] = None,
) -> EcoDpiaEvidence:
    """Record DPIA evidence. Append-only; a later record supersedes."""
    user_id, role, label = _authorize(actor, organization_id)
    effective = _as_date(effective_from)
    prior = _prior(db, EcoDpiaEvidence, organization_id, effective)

    row = EcoDpiaEvidence(
        organization_id=int(organization_id), status=EvidenceStatus(status).value,
        jurisdiction_scope=",".join(sorted(str(c).upper()
                                           for c in jurisdiction_scope))[:255],
        eu_substantive_applies=bool(eu_substantive_applies),
        eu_uk_gate_applies=bool(eu_uk_gate_applies),
        effective_from=effective,
        valid_until=(_as_date(valid_until) if valid_until else None),
        actor_user_id=user_id, actor_role=role, actor_label=label,
        external_reference=external_reference,
        supersedes_id=(prior.id if prior is not None else None))
    db.add(row)
    db.flush()
    return row


def dpia_in_force(db: Session, *, organization_id: int,
                  as_of) -> Optional[EcoDpiaEvidence]:
    return _in_force(db, EcoDpiaEvidence, int(organization_id), _as_date(as_of))


# ═════════════════════════════════════════════════════════════════════════════
# Consultation (D3)
# ═════════════════════════════════════════════════════════════════════════════
def record_consultation_attestation(
    db: Session, *, organization_id: int, actor, effective_from,
    status: EvidenceStatus = EvidenceStatus.COMPLETED,
    jurisdictions: Iterable[str] = (), expires_on=None,
    external_reference: Optional[str] = None,
    applicability_scope: str = "ORGANIZATION",
) -> EcoConsultationAttestation:
    """Record the attestation. Temveris does NOT assess its adequacy."""
    user_id, role, label = _authorize(actor, organization_id)
    effective = _as_date(effective_from)
    prior = _prior(db, EcoConsultationAttestation, organization_id, effective)

    row = EcoConsultationAttestation(
        organization_id=int(organization_id),
        status=EvidenceStatus(status).value,
        jurisdictions=",".join(sorted(str(c).upper()
                                      for c in jurisdictions))[:255],
        applicability_scope=applicability_scope[:32],
        effective_from=effective,
        expires_on=(_as_date(expires_on) if expires_on else None),
        actor_user_id=user_id, actor_role=role, actor_label=label,
        external_reference=external_reference,
        supersedes_id=(prior.id if prior is not None else None))
    db.add(row)
    db.flush()
    return row


def consultation_in_force(db: Session, *, organization_id: int,
                          as_of) -> Optional[EcoConsultationAttestation]:
    return _in_force(db, EcoConsultationAttestation, int(organization_id),
                     _as_date(as_of))


# ═════════════════════════════════════════════════════════════════════════════
# Worker notice (D4)
# ═════════════════════════════════════════════════════════════════════════════
def record_worker_notice(
    db: Session, *, organization_id: int, actor, effective_from,
    status: EvidenceStatus = EvidenceStatus.COMPLETED,
    population_scope: str = "ALL_WORKERS", new_joiners_covered: bool = True,
    repeat_on_policy_change: bool = False,
    external_reference: Optional[str] = None,
) -> EcoWorkerNoticeConfirmation:
    """Record the Tenant-level confirmation. NO Worker-level record is written.

    D4 forbids Worker acknowledgement records, and nothing here reads or
    writes a Worker row beyond attributing the CONFIRMING actor.
    """
    user_id, role, label = _authorize(actor, organization_id)
    effective = _as_date(effective_from)
    prior = _prior(db, EcoWorkerNoticeConfirmation, organization_id, effective)

    row = EcoWorkerNoticeConfirmation(
        organization_id=int(organization_id),
        status=EvidenceStatus(status).value, effective_from=effective,
        actor_user_id=user_id, actor_role=role, actor_label=label,
        population_scope=population_scope[:64],
        new_joiners_covered=bool(new_joiners_covered),
        repeat_on_policy_change=bool(repeat_on_policy_change),
        external_reference=external_reference,
        supersedes_id=(prior.id if prior is not None else None))
    db.add(row)
    db.flush()
    return row


def worker_notice_in_force(db: Session, *, organization_id: int,
                           as_of) -> Optional[EcoWorkerNoticeConfirmation]:
    return _in_force(db, EcoWorkerNoticeConfirmation, int(organization_id),
                     _as_date(as_of))


__all__ = [
    "consultation_in_force",
    "dpia_in_force",
    "record_consultation_attestation",
    "record_dpia_evidence",
    "record_worker_notice",
    "worker_notice_in_force",
]
