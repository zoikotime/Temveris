"""Tenant-scoped data access (§1 tenant isolation).

Every read goes through a ``Repository`` bound to exactly one ``tenant_id``, and
every query filters on it. Nothing in the service or router layer issues a raw
cross-tenant query — so a caller can never read or mutate another tenant's case
even if they guess a valid opaque id (the lookup simply returns ``None`` →
the router surfaces a 404, never a 403 that would confirm the id exists).

The repository is deliberately thin: it holds no business rules, just isolation
+ convenience lookups. Services own the rules.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from .constants import CaseState
from .models import (
    AccessRevocationRecord, AuditEvent, CompletionCertificate, DomainEvent,
    EvidencePackage, FormerWorkerAccess, IntegrationReconciliationItem,
    LegalHold, OffboardingCase, OffboardingTask, RetentionAssignment,
    WorkflowBlocker, WorkflowWaiver,
)

# Statuses that release the "one active case per engagement" lock.
CLOSED_CASE_STATES = frozenset({
    CaseState.COMPLETED.value, CaseState.CANCELLED.value, CaseState.REVERSED.value,
})


class Repository:
    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id

    # ── generic ──────────────────────────────────────────────────────────────
    def add(self, obj):
        self.db.add(obj)
        return obj

    def flush(self):
        self.db.flush()

    # ── cases ──────────────────────────────────────────────────────────────--
    def get_case(self, case_id: str) -> Optional[OffboardingCase]:
        return (
            self.db.query(OffboardingCase)
            .filter(
                OffboardingCase.id == case_id,
                OffboardingCase.tenant_id == self.tenant_id,
            )
            .first()
        )

    def active_case_for_engagement(self, engagement_id: str) -> Optional[OffboardingCase]:
        """The single open case for an engagement, if any. Used to enforce the
        unique-active-case rule before insert (the DB unique key is the backstop)."""
        return (
            self.db.query(OffboardingCase)
            .filter(
                OffboardingCase.tenant_id == self.tenant_id,
                OffboardingCase.active_engagement_key == engagement_id,
            )
            .first()
        )

    def list_cases(
        self,
        *,
        status: Optional[str] = None,
        worker_id: Optional[int] = None,
        engagement_id: Optional[str] = None,
        legal_hold_status: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[OffboardingCase]:
        q = self.db.query(OffboardingCase).filter(OffboardingCase.tenant_id == self.tenant_id)
        if status:
            q = q.filter(OffboardingCase.status == status)
        if worker_id is not None:
            q = q.filter(OffboardingCase.worker_id == worker_id)
        if engagement_id:
            q = q.filter(OffboardingCase.engagement_id == engagement_id)
        if legal_hold_status:
            q = q.filter(OffboardingCase.legal_hold_status == legal_hold_status)
        if department:
            q = q.filter(OffboardingCase.department == department)
        return (
            q.order_by(OffboardingCase.created_at.desc())
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
            .all()
        )

    def count_cases(self, *, status: Optional[str] = None) -> int:
        q = self.db.query(OffboardingCase).filter(OffboardingCase.tenant_id == self.tenant_id)
        if status:
            q = q.filter(OffboardingCase.status == status)
        return q.count()

    # ── tasks ──────────────────────────────────────────────────────────────--
    def tasks_for_case(self, case_id: str) -> List[OffboardingTask]:
        return (
            self.db.query(OffboardingTask)
            .filter(
                OffboardingTask.tenant_id == self.tenant_id,
                OffboardingTask.case_id == case_id,
            )
            .order_by(OffboardingTask.sequence.asc())
            .all()
        )

    def get_task(self, task_id: str) -> Optional[OffboardingTask]:
        return (
            self.db.query(OffboardingTask)
            .filter(
                OffboardingTask.id == task_id,
                OffboardingTask.tenant_id == self.tenant_id,
            )
            .first()
        )

    # ── revocations ────────────────────────────────────────────────────────--
    def revocations_for_case(self, case_id: str) -> List[AccessRevocationRecord]:
        return (
            self.db.query(AccessRevocationRecord)
            .filter(
                AccessRevocationRecord.tenant_id == self.tenant_id,
                AccessRevocationRecord.case_id == case_id,
            )
            .all()
        )

    # ── blockers / waivers ────────────────────────────────────────────────--
    def blockers_for_case(self, case_id: str, *, open_only: bool = False) -> List[WorkflowBlocker]:
        q = self.db.query(WorkflowBlocker).filter(
            WorkflowBlocker.tenant_id == self.tenant_id,
            WorkflowBlocker.case_id == case_id,
        )
        if open_only:
            q = q.filter(WorkflowBlocker.status == "open")
        return q.order_by(WorkflowBlocker.created_at.asc()).all()

    def get_blocker(self, blocker_id: str) -> Optional[WorkflowBlocker]:
        return (
            self.db.query(WorkflowBlocker)
            .filter(
                WorkflowBlocker.id == blocker_id,
                WorkflowBlocker.tenant_id == self.tenant_id,
            )
            .first()
        )

    def waivers_for_case(self, case_id: str) -> List[WorkflowWaiver]:
        return (
            self.db.query(WorkflowWaiver)
            .filter(
                WorkflowWaiver.tenant_id == self.tenant_id,
                WorkflowWaiver.case_id == case_id,
            )
            .all()
        )

    # ── legal holds ────────────────────────────────────────────────────────--
    def active_holds_for_case(self, case_id: str) -> List[LegalHold]:
        return (
            self.db.query(LegalHold)
            .filter(
                LegalHold.tenant_id == self.tenant_id,
                LegalHold.case_id == case_id,
                LegalHold.status == "active",
            )
            .all()
        )

    def get_hold(self, hold_id: str) -> Optional[LegalHold]:
        return (
            self.db.query(LegalHold)
            .filter(
                LegalHold.id == hold_id,
                LegalHold.tenant_id == self.tenant_id,
            )
            .first()
        )

    # ── retention ──────────────────────────────────────────────────────────--
    def retention_for_case(self, case_id: str) -> Optional[RetentionAssignment]:
        return (
            self.db.query(RetentionAssignment)
            .filter(
                RetentionAssignment.tenant_id == self.tenant_id,
                RetentionAssignment.case_id == case_id,
            )
            .first()
        )

    # ── former-worker access ─────────────────────────────────────────────--
    def former_access_for_case(self, case_id: str) -> Optional[FormerWorkerAccess]:
        return (
            self.db.query(FormerWorkerAccess)
            .filter(
                FormerWorkerAccess.tenant_id == self.tenant_id,
                FormerWorkerAccess.case_id == case_id,
            )
            .first()
        )

    # ── certificate / evidence ───────────────────────────────────────────--
    def certificate_for_case(self, case_id: str) -> Optional[CompletionCertificate]:
        return (
            self.db.query(CompletionCertificate)
            .filter(
                CompletionCertificate.tenant_id == self.tenant_id,
                CompletionCertificate.case_id == case_id,
            )
            .first()
        )

    def evidence_for_case(self, case_id: str) -> List[EvidencePackage]:
        return (
            self.db.query(EvidencePackage)
            .filter(
                EvidencePackage.tenant_id == self.tenant_id,
                EvidencePackage.case_id == case_id,
            )
            .order_by(EvidencePackage.created_at.asc())
            .all()
        )

    # ── audit / events ───────────────────────────────────────────────────--
    def audit_for_case(self, case_id: str, limit: int = 200) -> List[AuditEvent]:
        return (
            self.db.query(AuditEvent)
            .filter(
                AuditEvent.tenant_id == self.tenant_id,
                AuditEvent.case_id == case_id,
            )
            .order_by(AuditEvent.created_at.asc())
            .limit(limit)
            .all()
        )

    def events_for_case(self, case_id: str, limit: int = 200) -> List[DomainEvent]:
        return (
            self.db.query(DomainEvent)
            .filter(
                DomainEvent.tenant_id == self.tenant_id,
                DomainEvent.case_id == case_id,
            )
            .order_by(DomainEvent.occurred_at.asc())
            .limit(limit)
            .all()
        )

    # ── reconciliation queue ─────────────────────────────────────────────--
    def reconciliation_for_case(self, case_id: str) -> List[IntegrationReconciliationItem]:
        return (
            self.db.query(IntegrationReconciliationItem)
            .filter(
                IntegrationReconciliationItem.tenant_id == self.tenant_id,
                IntegrationReconciliationItem.case_id == case_id,
            )
            .all()
        )
