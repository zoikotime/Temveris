"""Security API orchestrator (§7).

Backs the four security endpoints with the same authorization, audit and
tenant-isolation guarantees as the rest of the module:

    POST /revoke-access      → immediate revocation (by case, engagement, or worker)
    POST /retry-revocation   → re-drive a failed revocation + its reconciliation
    GET  /reconciliation     → reconciliation queue + identity conflicts
    GET  /security-events     → security incidents + quarantine summary

Everything routes through the existing services (``IdentityService``,
``TrackingService``, the reconciliation engine, the incident store) so there is
exactly one implementation of each control — the API is a thin, RBAC-gated,
audited façade over them.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from . import rbac, reconciliation
from .constants import (
    CaseState, IdentityConflictType, IncidentStatus, RevocationStatus,
)
from .incidents import IncidentService
from .models import (
    IntegrationReconciliationItem, OffboardingCase, SecurityIncident,
    TelemetryQuarantineRecord,
)
from .rbac import ResourceAttrs
from .repository import Repository
from .services import OffboardingContext, OffboardingService, ServiceError

_log = logging.getLogger("zoiko.offboarding.security")


_CONFLICT_TYPES = frozenset(c.value for c in IdentityConflictType)


def _resource(case: OffboardingCase) -> ResourceAttrs:
    return ResourceAttrs(
        tenant_id=case.tenant_id, department=case.department, jurisdiction=case.jurisdiction,
        worker_type=case.worker_type, exit_type=case.exit_type, risk_level=case.risk_level,
        legal_hold_status=case.legal_hold_status, case_status=case.status,
        data_classification=case.data_classification,
    )


class CrossTenantOrMissing(ServiceError):
    """A case/worker that doesn't exist in the caller's tenant — surfaced as 404
    so it never confirms the existence of another tenant's row."""
    http_status = 404

    def __init__(self):
        super().__init__("not found", "not_found")


class SecurityService:
    def __init__(self, ctx: OffboardingContext):
        self.ctx = ctx
        self.svc = OffboardingService(ctx)
        self.incidents = IncidentService(ctx)

    def _repo(self) -> Repository:
        return self.ctx.repo()

    # ── POST /revoke-access ─────────────────────────────────────────────────
    def revoke_access(self, *, worker_id: Optional[int] = None, case_id: Optional[str] = None,
                      engagement_id: Optional[str] = None, reason: str = "security_revoke") -> dict:
        """Immediately revoke access. Resolves a target case from case_id /
        engagement_id / worker_id; if a case is found, runs the full revocation
        sweep + tracking stop against it. If only a worker_id is given with no
        open case, performs a break-glass direct revocation of that worker's
        local surfaces. Idempotent and retry-safe."""
        repo = self._repo()
        case = self._resolve_case(repo, worker_id=worker_id, case_id=case_id,
                                  engagement_id=engagement_id)

        if case is not None:
            rbac.require(self.ctx.authz, "access.revoke", _resource(case))
            records = self.svc.identity.revoke_all(case)
            tracking = self.svc.tracking.stop_tracking(case)
            failed = [r.component for r in records if r.status == RevocationStatus.FAILED.value]
            return {
                "mode": "case",
                "case_id": case.id,
                "worker_id": case.worker_id,
                "components": {r.component: r.status for r in records},
                "failed": failed,
                "tracking": tracking,
            }

        if worker_id is None:
            raise ServiceError("provide case_id, engagement_id, or worker_id", "no_target")

        # Break-glass: no case row, revoke the worker's local surfaces directly.
        rbac.require(self.ctx.authz, "access.revoke")
        evidence = self.svc.identity.revoke_worker_directly(worker_id, reason=reason)
        return {"mode": "direct_worker", "worker_id": worker_id, "evidence": evidence}

    def _resolve_case(self, repo: Repository, *, worker_id: Optional[int],
                      case_id: Optional[str], engagement_id: Optional[str]
                      ) -> Optional[OffboardingCase]:
        if case_id:
            case = repo.get_case(case_id)
            if case is None:
                raise CrossTenantOrMissing()    # cross-tenant / unknown → 404
            return case
        if engagement_id:
            return repo.active_case_for_engagement(engagement_id)
        if worker_id is not None:
            # Most-recent non-inert case for the worker, if any.
            inert = {CaseState.DRAFT.value, CaseState.PENDING_APPROVAL.value,
                     CaseState.CANCELLED.value, CaseState.REVERSED.value}
            for c in repo.list_cases(worker_id=worker_id, limit=20):
                if c.status not in inert:
                    return c
        return None

    # ── POST /retry-revocation ──────────────────────────────────────────────
    def retry_revocation(self, *, case_id: str) -> dict:
        """Re-drive a stuck revocation: if the case is in ACCESS_REVOCATION_FAILED
        re-run the lifecycle retry; otherwise re-attempt any non-terminal
        component and process the case's reconciliation queue."""
        repo = self._repo()
        case = repo.get_case(case_id)
        if case is None:
            raise CrossTenantOrMissing()
        rbac.require(self.ctx.authz, "access.revoke", _resource(case))

        if case.status == CaseState.ACCESS_REVOCATION_FAILED.value:
            case = self.svc.retry_access_closure(case_id)
            outcome = "lifecycle_retry"
        else:
            self.svc.identity.revoke_all(case)
            outcome = "component_resweep"

        recon = self._retry_case_reconciliation(repo, case)
        records = repo.revocations_for_case(case.id)
        failed = [r.component for r in records if r.status == RevocationStatus.FAILED.value]
        return {
            "case_id": case.id,
            "status": case.status,
            "outcome": outcome,
            "failed": failed,
            "reconciliation": recon,
        }

    def _retry_case_reconciliation(self, repo: Repository, case: OffboardingCase) -> dict:
        counts = {"processed": 0, "succeeded": 0, "dead_letter": 0, "failed": 0}
        for item in repo.reconciliation_for_case(case.id):
            status = reconciliation.process_item(self.ctx.db, item,
                                                 correlation_id=self.ctx.correlation_id)
            counts["processed"] += 1
            counts[status] = counts.get(status, 0) + 1
        return counts

    # ── GET /reconciliation ─────────────────────────────────────────────────
    def reconciliation_view(self, *, status: Optional[str] = None,
                            integration: Optional[str] = None,
                            limit: int = 100, offset: int = 0) -> dict:
        if not rbac.can(self.ctx.authz, "audit.view"):
            raise ServiceError("missing permission audit.view", "forbidden")
        db = self.ctx.db
        q = (db.query(IntegrationReconciliationItem)
             .filter(IntegrationReconciliationItem.tenant_id == self.ctx.tenant_id))
        if status:
            q = q.filter(IntegrationReconciliationItem.status == status)
        if integration:
            q = q.filter(IntegrationReconciliationItem.integration == integration)
        items = (q.order_by(IntegrationReconciliationItem.created_at.desc())
                 .limit(max(1, min(limit, 500))).offset(max(0, offset)).all())

        conflicts = (db.query(SecurityIncident)
                     .filter(SecurityIncident.tenant_id == self.ctx.tenant_id,
                             SecurityIncident.incident_type.in_(list(_CONFLICT_TYPES)),
                             SecurityIncident.status.in_([IncidentStatus.OPEN.value,
                                                          IncidentStatus.ESCALATED.value]))
                     .order_by(SecurityIncident.created_at.desc()).limit(200).all())
        return {
            "queue": [self._recon_dict(i) for i in items],
            "conflicts": [self._incident_dict(c) for c in conflicts],
            "queue_total": q.count(),
        }

    # ── GET /security-events ────────────────────────────────────────────────
    def security_events(self, *, severity: Optional[str] = None, status: Optional[str] = None,
                        incident_type: Optional[str] = None, worker_id: Optional[int] = None,
                        limit: int = 100, offset: int = 0) -> dict:
        if not rbac.can(self.ctx.authz, "audit.view"):
            raise ServiceError("missing permission audit.view", "forbidden")
        incidents = self.incidents.list(status=status, severity=severity,
                                        worker_id=worker_id, limit=limit, offset=offset)
        if incident_type:
            incidents = [i for i in incidents if i.incident_type == incident_type]
        quarantines = (self.ctx.db.query(TelemetryQuarantineRecord)
                       .filter(TelemetryQuarantineRecord.tenant_id == self.ctx.tenant_id)
                       .order_by(TelemetryQuarantineRecord.created_at.desc())
                       .limit(50).all())
        return {
            "incidents": [self._incident_dict(i) for i in incidents],
            "open_count": self._open_incident_count(),
            "recent_quarantine": [self._quarantine_dict(q) for q in quarantines],
        }

    def _open_incident_count(self) -> int:
        return (self.ctx.db.query(SecurityIncident)
                .filter(SecurityIncident.tenant_id == self.ctx.tenant_id,
                        SecurityIncident.status.in_([IncidentStatus.OPEN.value,
                                                     IncidentStatus.ESCALATED.value]))
                .count())

    # ── serialization ────────────────────────────────────────────────────────
    @staticmethod
    def _incident_dict(i: SecurityIncident) -> dict:
        return {
            "id": i.id, "reference": i.reference, "incident_type": i.incident_type,
            "severity": i.severity, "status": i.status, "title": i.title,
            "worker_id": i.worker_id, "case_id": i.case_id, "occurrences": i.occurrences,
            "diagnostics": i.diagnostics, "created_at": i.created_at,
            "escalated_at": i.escalated_at, "resolved_at": i.resolved_at,
        }

    @staticmethod
    def _recon_dict(i: IntegrationReconciliationItem) -> dict:
        return {
            "id": i.id, "case_id": i.case_id, "worker_id": i.worker_id,
            "integration": i.integration, "component": i.component, "operation": i.operation,
            "status": i.status, "attempts": i.attempts, "max_attempts": i.max_attempts,
            "last_error": i.last_error, "next_attempt_at": i.next_attempt_at,
            "created_at": i.created_at,
        }

    @staticmethod
    def _quarantine_dict(q: TelemetryQuarantineRecord) -> dict:
        return {
            "id": q.id, "worker_id": q.worker_id, "case_id": q.case_id,
            "telemetry_kind": q.telemetry_kind, "reason": q.reason, "status": q.status,
            "incident_id": q.incident_id, "created_at": q.created_at,
        }
