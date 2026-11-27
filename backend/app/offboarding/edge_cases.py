"""Edge-case engine (§4 — production governance).

The lifecycle's happy path is well covered; this module is the explicit home
for the awkward real-world cases an enterprise offboarding system must not fall
over on. Several are already handled elsewhere and are re-exposed here as
named, testable operations so QA can assert each one:

  • rehire                 → controlled reversal + rehire-conflict detection
  • transfer               → mid-flight department move (audited)
  • manager reassignment   → reassign a task's owner (audited)
  • duplicate identity     → reconciliation conflict (multiple_identities)
  • integration failure    → reconciliation dead-letter + blocker
  • desktop offline        → late telemetry quarantined by the gate
  • payroll unavailable    → dependency blocker (completion stays gated)
  • billing unavailable    → dependency blocker
  • deleted tenant         → hard guard: refuse to operate
  • multiple entities      → duplicate-identity detection
  • final access in past   → enforcement gate denies immediately

Each handler reuses the existing services (no duplicate revocation/audit
logic); the value here is a single, named, audited entry point per scenario.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from . import incidents, rbac, reconciliation, workflow
from .constants import AlertType, CaseState, IncidentSeverity
from .models import OffboardingCase
from .rbac import ResourceAttrs
from .repository import Repository
from .services import OffboardingContext, OffboardingService, ServiceError

_log = logging.getLogger("zoiko.offboarding.edge_cases")


# Dependencies whose unavailability blocks a closure step rather than failing it.
_DEPENDENCIES = frozenset({"payroll", "billing", "integration", "scim", "sso"})


class TenantDeleted(ServiceError):
    http_status = 409

    def __init__(self, tenant_id: str):
        super().__init__(f"tenant {tenant_id} is deleted/suspended — operations refused",
                         "tenant_deleted")


def is_tenant_deleted(db, tenant_id: str) -> bool:
    """Best-effort check against the commercial org registry. A tenant whose
    commercial organization is in a terminal state (deleted/cancelled/churned)
    must not accept new offboarding mutations. Fail-OPEN (returns False) if the
    registry isn't present — the repository's tenant scoping is still the
    primary isolation guarantee; this is defence in depth, not the only line."""
    try:
        from app.commercial.models import CommercialOrganization
        org = (db.query(CommercialOrganization)
               .filter(CommercialOrganization.id == tenant_id).first())
        if org is None:
            return False
        status = (getattr(org, "status", None) or getattr(org, "state", None) or "").lower()
        return status in {"deleted", "cancelled", "canceled", "churned", "suspended", "terminated"}
    except Exception:
        return False


def _resource(case: OffboardingCase) -> ResourceAttrs:
    return ResourceAttrs(
        tenant_id=case.tenant_id, department=case.department, jurisdiction=case.jurisdiction,
        worker_type=case.worker_type, exit_type=case.exit_type, risk_level=case.risk_level,
        legal_hold_status=case.legal_hold_status, case_status=case.status,
        data_classification=case.data_classification,
    )


class EdgeCaseService:
    def __init__(self, ctx: OffboardingContext):
        self.ctx = ctx
        self.svc = OffboardingService(ctx)

    def _repo(self) -> Repository:
        return self.ctx.repo()

    def _get(self, case_id: str) -> OffboardingCase:
        case = self._repo().get_case(case_id)
        if case is None:
            from .security_service import CrossTenantOrMissing
            raise CrossTenantOrMissing()
        return case

    def _guard_tenant(self, case: OffboardingCase) -> None:
        if is_tenant_deleted(self.ctx.db, case.tenant_id):
            raise TenantDeleted(case.tenant_id)

    # ── REHIRE ───────────────────────────────────────────────────────────
    def rehire(self, case_id: str, *, reason: str) -> dict:
        """Reverse the exit (restores login, revokes former-worker access) and
        run conflict detection so a stale SSO/SCIM state surfaces rather than
        silently leaving the worker half-in/half-out."""
        case = self._get(case_id)
        self._guard_tenant(case)
        case = self.svc.reverse(case_id, reason=reason)
        conflicts = reconciliation.detect_conflicts_for_case(self.ctx.db, case,
                                                            correlation_id=self.ctx.correlation_id)
        return {"case_id": case.id, "status": case.status, "conflicts": conflicts}

    # ── TRANSFER (mid-flight department move) ────────────────────────────
    def transfer(self, case_id: str, *, new_department: str,
                 new_team: Optional[str] = None) -> dict:
        case = self._get(case_id)
        self._guard_tenant(case)
        rbac.require(self.ctx.authz, "case.update", _resource(case))
        old = case.department
        case.department = new_department
        if new_team is not None:
            case.meta = {**(case.meta or {}), "team": new_team}
        self.ctx.db.flush()
        self.svc.audit.record(action="WorkerTransferred", entity="OffboardingCase",
                              case=case, payload={"from_department": old,
                                                  "to_department": new_department,
                                                  "team": new_team})
        return {"case_id": case.id, "department": case.department}

    # ── MANAGER REASSIGNMENT ─────────────────────────────────────────────
    def reassign_task(self, case_id: str, *, task_type: str,
                      new_assignee_id: int, new_role: Optional[str] = None) -> dict:
        case = self._get(case_id)
        self._guard_tenant(case)
        rbac.require(self.ctx.authz, "task.complete", _resource(case))
        repo = self._repo()
        task = next((t for t in repo.tasks_for_case(case.id) if t.task_type == task_type), None)
        if task is None:
            raise ServiceError(f"no task of type {task_type!r} on this case", "task_not_found")
        old = task.assignee_id
        task.assignee_id = new_assignee_id
        if new_role:
            task.assignee_role = new_role
        self.ctx.db.flush()
        self.svc.audit.record(action="TaskReassigned", entity="OffboardingTask", case=case,
                              payload={"task_type": task_type, "from_assignee": old,
                                       "to_assignee": new_assignee_id})
        return {"case_id": case.id, "task_type": task_type, "assignee_id": new_assignee_id}

    # ── DEPENDENCY UNAVAILABLE (payroll / billing / integration) ─────────
    def mark_dependency_unavailable(self, case_id: str, *, dependency: str,
                                    detail: Optional[str] = None) -> dict:
        """A downstream system (payroll, billing, an integration) is unreachable.
        We BLOCK rather than fail: a blocker keeps the case out of COMPLETED
        until the dependency recovers, and a MEDIUM incident gives ops a signal
        without paging. The retry job + reconciliation engine drive recovery."""
        if dependency not in _DEPENDENCIES:
            raise ServiceError(f"unknown dependency {dependency!r}", "unknown_dependency")
        case = self._get(case_id)
        self._guard_tenant(case)
        rbac.require(self.ctx.authz, "blocker.raise", _resource(case))
        repo = self._repo()
        blocker_type = f"{dependency}_unavailable"
        if not any(b.blocker_type == blocker_type and b.status == "open"
                   for b in repo.blockers_for_case(case.id, open_only=True)):
            workflow.raise_blocker(repo, case, blocker_type=blocker_type,
                                   reason=detail or f"{dependency} unavailable",
                                   severity="medium", actor_id=self.ctx.actor_id)
        incidents.open_incident(
            self.ctx.db, tenant_id=case.tenant_id,
            incident_type=AlertType.RECONCILIATION_CONFLICT.value,
            title=f"{dependency} unavailable for case {case.id}",
            severity=IncidentSeverity.MEDIUM.value,
            worker_id=case.worker_id, case_id=case.id,
            diagnostics={"dependency": dependency, "detail": detail},
            correlation_id=self.ctx.correlation_id,
        )
        return {"case_id": case.id, "blocker": blocker_type, "status": "blocked_pending_recovery"}

    # ── CLASSIFY (detect every edge condition that currently applies) ────
    def classify(self, case_id: str) -> dict:
        """Read-only: enumerate the edge conditions that currently apply to a
        case (rehire conflict, duplicate identity, stale SSO, final-access
        elapsed, dependency blockers). Lets the UI badge a case before an
        operator acts on it."""
        from . import enforcement
        case = self._get(case_id)
        conditions: List[str] = []

        # Reconciliation-detectable conflicts (rehire, duplicate identity, SSO/SCIM).
        if case.status in (
            CaseState.ACCESS_CLOSURE_PENDING.value, CaseState.FINAL_REVIEW_PENDING.value,
            CaseState.PAYROLL_CLOSURE_PENDING.value, CaseState.BILLING_CLOSURE_PENDING.value,
            CaseState.LEGAL_HOLD_REVIEW_PENDING.value, CaseState.FORMER_ACCESS_PENDING.value,
            CaseState.COMPLETED.value,
        ):
            # Read-only peek: we don't open incidents here, just report.
            from .models import SecurityIncident
            open_conflicts = (self.ctx.db.query(SecurityIncident.incident_type)
                              .filter(SecurityIncident.tenant_id == case.tenant_id,
                                      SecurityIncident.case_id == case.id,
                                      SecurityIncident.status.in_(["open", "escalated"]))
                              .all())
            conditions.extend(sorted({t for (t,) in open_conflicts}))

        # Final-access elapsed while still pre-cutover.
        if case.worker_id is not None:
            decision = enforcement.evaluate_worker(self.ctx.db, case.worker_id,
                                                  tenant_id=case.tenant_id)
            if not decision.allowed and decision.reason == "past_final_access_at":
                conditions.append("final_access_in_past")

        # Open dependency blockers.
        for b in self._repo().blockers_for_case(case.id, open_only=True):
            if b.blocker_type.endswith("_unavailable"):
                conditions.append(b.blocker_type)

        return {"case_id": case.id, "status": case.status,
                "edge_conditions": sorted(set(conditions))}
