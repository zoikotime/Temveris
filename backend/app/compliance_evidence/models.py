"""
ECO compliance evidence — persisted (D2, D3, D4, D7).
=====================================================

Three dedicated tables. D7 is explicit that these must NOT reuse
``commercial_consents`` (a signup-time consent keyed to a user, with
``ip_address`` and ``user_agent_hash`` — wrong subject, wrong lifecycle),
``audit_logs`` (``ondelete="SET NULL"`` on both organization and actor, so
evidence could lose its Tenant) or ``ConfigVersion`` (configuration history,
not compliance).

ONE SHARED SHAPE, THREE DIFFERENT FACTS
---------------------------------------
Every record carries: the Tenant, a status, the actor and the role they held,
a timestamp, an effective date, an optional external reference, a supersession
pointer and immutable creation metadata. That is the intersection of D2, D3 and
D4, and it is deliberately not factored into a shared base class — three tables
with an honest shape each beats one polymorphic table with a discriminator,
because the differences (two DPIA applicability flags, jurisdictions on the
attestation, population scope on the notice) are the substance.

APPEND-ONLY
-----------
All three carry database triggers. The ORM sees none of a Core UPDATE, so the
database has to be the one that refuses it (Ruling M / D18-F1).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index,
    Integer, String, Text, event, text,
)

from database.database import Base

from .append_only_ddl import (
    CONSULTATION_TABLE, DPIA_TABLE, WORKER_NOTICE_TABLE,
    append_only_statements,
)
from .outcomes import EvidenceStatus

_STATUS_SQL = ", ".join(f"'{m.value}'" for m in EvidenceStatus)

#: Owner ruling D: ONE authorization vocabulary for ECO governance acts.
_ROLE_SQL = "'admin', 'hr'"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EcoDpiaEvidence(Base):
    """A Tenant's DPIA evidence (D2).

    TWO APPLICABILITY FLAGS, NOT ONE (owner ruling C: B)
    ----------------------------------------------------
    D1 ruled that BOTH scopes apply: §40.2's substantive requirement is EU,
    §44.3's rollout evidence gate is EU/UK. A single scope enum would force
    those into one value and make "EU-substantive, UK-gate-only" unsayable, so
    the record carries the two answers it was assessed against, frozen at
    recording time. They are evidence of what the Tenant was told applied to
    it, not a cache of today's mapping.
    """

    __tablename__ = DPIA_TABLE

    id = Column(Integer, primary_key=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    status = Column(String(16), nullable=False)

    #: The jurisdiction scope this evidence was recorded against, frozen.
    #: Human-readable ISO codes, comma-joined, because this is provenance for a
    #: reader rather than a key anything joins on.
    jurisdiction_scope = Column(String(255), nullable=False, default="")

    #: Owner ruling C: B. Frozen at recording time.
    eu_substantive_applies = Column(Boolean, nullable=False, default=False)
    eu_uk_gate_applies = Column(Boolean, nullable=False, default=False)

    effective_from = Column(Date, nullable=False, index=True)
    #: Optional. §40.2 describes a DPIA produced from a template and reviewed
    #: on a real-world cycle; a Tenant may record when its assessment lapses.
    valid_until = Column(Date, nullable=True)

    recorded_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    actor_role = Column(String(32), nullable=False)
    actor_label = Column(String(64), nullable=False, default="SYSTEM")

    #: A pointer to the externally held assessment. §40.2 calls the DPIA a
    #: document produced from a template — Temveris never holds the assessment
    #: itself, only the fact that one exists.
    external_reference = Column(Text, nullable=True)

    supersedes_id = Column(
        Integer, ForeignKey(f"{DPIA_TABLE}.id", ondelete="RESTRICT"),
        nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (%s)" % _STATUS_SQL,
                        name="ck_eco_dpia_evidence_status"),
        CheckConstraint("actor_role IN (%s)" % _ROLE_SQL,
                        name="ck_eco_dpia_evidence_actor_role"),
        CheckConstraint("valid_until IS NULL OR valid_until >= effective_from",
                        name="ck_eco_dpia_evidence_validity_window"),
        Index("ix_eco_dpia_evidence_lookup", "organization_id",
              "effective_from"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<EcoDpiaEvidence org={self.organization_id} "
                f"{self.status} from={self.effective_from}>")


class EcoConsultationAttestation(Base):
    """A Tenant's employee-representative consultation attestation (D3).

    §44.3: "Temveris records the attestation; it does not assess its
    adequacy." There is deliberately no reviewed, approved or verified column —
    a field for a judgement is an invitation to make one.
    """

    __tablename__ = CONSULTATION_TABLE

    id = Column(Integer, primary_key=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    status = Column(String(16), nullable=False)

    #: The jurisdictions attested for, frozen at attestation time. §40.4 names
    #: Germany, the Netherlands, Austria and France; the governed mapping says
    #: which of a Tenant's jurisdictions required consultation on the day.
    jurisdictions = Column(String(255), nullable=False, default="")
    applicability_scope = Column(String(32), nullable=False,
                                 default="ORGANIZATION")

    attested_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)
    effective_from = Column(Date, nullable=False, index=True)
    #: Nullable. A works-council agreement may or may not lapse; D3 leaves it
    #: optional rather than asserting a renewal cycle the source never states.
    expires_on = Column(Date, nullable=True)

    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    actor_role = Column(String(32), nullable=False)
    actor_label = Column(String(64), nullable=False, default="SYSTEM")

    external_reference = Column(Text, nullable=True)

    recorded_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)
    supersedes_id = Column(
        Integer, ForeignKey(f"{CONSULTATION_TABLE}.id", ondelete="RESTRICT"),
        nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (%s)" % _STATUS_SQL,
                        name="ck_eco_consultation_attestation_status"),
        CheckConstraint("actor_role IN (%s)" % _ROLE_SQL,
                        name="ck_eco_consultation_attestation_actor_role"),
        CheckConstraint("expires_on IS NULL OR expires_on >= effective_from",
                        name="ck_eco_consultation_attestation_window"),
        Index("ix_eco_consultation_attestation_lookup", "organization_id",
              "effective_from"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<EcoConsultationAttestation org={self.organization_id} "
                f"{self.status} from={self.effective_from}>")


class EcoWorkerNoticeConfirmation(Base):
    """A Tenant's confirmation that Workers were informed (D4).

    TENANT-LEVEL ONLY
    -----------------
    §44.3 says "Tenant confirms Workers have been informed the control is
    active". D4 is explicit that this establishes NO requirement for individual
    Worker acknowledgement, and forbids Worker-level records. There is no
    ``user_id`` here beyond the CONFIRMING actor, and the gate never reads a
    roster (E4) — which is also the answer §36 and §33.4's minimisation
    reasoning would give.

    E1-E3, encoded
    --------------
    ``effective_from`` and supersession are the whole validity model: a
    confirmation is permanent from its effective date until a later one
    supersedes it (E1). New Workers are covered automatically, so there is no
    population snapshot to go stale (E2). A policy change does not invalidate
    it, so there is no policy reference to compare against (E3). The two
    descriptive columns below record what the Tenant SAID about scope; they are
    not inputs to the gate.
    """

    __tablename__ = WORKER_NOTICE_TABLE

    id = Column(Integer, primary_key=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    status = Column(String(16), nullable=False)

    confirmed_at_utc = Column(DateTime(timezone=True), nullable=False,
                              default=_utcnow)
    effective_from = Column(Date, nullable=False, index=True)

    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    actor_role = Column(String(32), nullable=False)
    actor_label = Column(String(64), nullable=False, default="SYSTEM")

    #: DESCRIPTIVE, not evaluative. What the Tenant stated about the population
    #: it informed, and about repetition. The gate does not read either (E2,
    #: E3, E4) — they exist so the record says what was confirmed, not so the
    #: checker can second-guess it.
    population_scope = Column(String(64), nullable=False,
                              default="ALL_WORKERS")
    new_joiners_covered = Column(Boolean, nullable=False, default=True)
    repeat_on_policy_change = Column(Boolean, nullable=False, default=False)

    external_reference = Column(Text, nullable=True)

    recorded_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)
    supersedes_id = Column(
        Integer, ForeignKey(f"{WORKER_NOTICE_TABLE}.id", ondelete="RESTRICT"),
        nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (%s)" % _STATUS_SQL,
                        name="ck_eco_worker_notice_status"),
        CheckConstraint("actor_role IN (%s)" % _ROLE_SQL,
                        name="ck_eco_worker_notice_actor_role"),
        Index("ix_eco_worker_notice_lookup", "organization_id",
              "effective_from"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<EcoWorkerNoticeConfirmation org={self.organization_id} "
                f"{self.status} from={self.effective_from}>")


# ═════════════════════════════════════════════════════════════════════════════
# Database-level append-only enforcement (D2, D3, D4)
# ═════════════════════════════════════════════════════════════════════════════
def _install(table_name: str):
    def _listener(target, connection, **kw):
        for statement in append_only_statements(connection.dialect.name,
                                                table_name):
            connection.execute(text(statement))
    return _listener


event.listens_for(EcoDpiaEvidence.__table__, "after_create")(
    _install(DPIA_TABLE))
event.listens_for(EcoConsultationAttestation.__table__, "after_create")(
    _install(CONSULTATION_TABLE))
event.listens_for(EcoWorkerNoticeConfirmation.__table__, "after_create")(
    _install(WORKER_NOTICE_TABLE))


__all__ = [
    "EcoConsultationAttestation",
    "EcoDpiaEvidence",
    "EcoWorkerNoticeConfirmation",
]
