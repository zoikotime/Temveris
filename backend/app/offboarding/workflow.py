"""Workflow engine (§5).

Owns the task graph for a case: which tasks exist, who they route to, their SLA
clocks and escalation, blockers + waivers, and — the keystone — the completion
readiness calculation that decides whether a case is allowed to finish. The
readiness gate is what enforces "completion is BLOCKED if any critical control
fails" from the module doctrine: no green light unless access is provably
revoked, tracking provably stopped, blockers cleared, required tasks done (or
waived), retention assigned, and no legal hold is standing in the way.

These functions operate on a tenant-bound ``Repository`` and may create task /
blocker rows, but they do NOT emit domain events or commit — the service layer
wraps them so the audit + event + state write stay in one transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from . import ids
from .constants import (
    CaseState, RevocationStatus, TaskStatus, TaskType,
)
from .models import OffboardingCase, OffboardingTask, WorkflowBlocker, WorkflowWaiver
from .repository import Repository
from .schemas import CompletionReadiness


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Ordered task template: (type, required, default assignee role, SLA hours).
# Sequence is the index — it drives the timeline ordering and which closure
# step a task belongs to.
TASK_TEMPLATE: List[tuple] = [
    (TaskType.ACCESS_REVOCATION,      True,  "it_security_admin", 4),
    (TaskType.TRACKING_STOP_VERIFY,   True,  "it_security_admin", 4),
    (TaskType.FINAL_WORK_REVIEW,      True,  "manager",           48),
    (TaskType.PAYROLL_CLOSURE,        True,  "payroll_admin",     72),
    (TaskType.BILLING_CLOSURE,        True,  "finance_admin",     72),
    (TaskType.RETENTION_ASSIGNMENT,   True,  "compliance_admin",  72),
    (TaskType.LEGAL_HOLD_REVIEW,      True,  "legal_admin",       72),
    (TaskType.FORMER_WORKER_SETUP,    False, "hr_admin",          168),
    (TaskType.CERTIFICATE_GENERATION, True,  "hr_lead",           24),
]


def seed_tasks(repo: Repository, case: OffboardingCase) -> List[OffboardingTask]:
    """Create the standard task set for a case (idempotent — skips types that
    already exist). Called when a case is approved."""
    existing = {t.task_type for t in repo.tasks_for_case(case.id)}
    created: List[OffboardingTask] = []
    now = _now()
    for seq, (ttype, required, role, sla_hours) in enumerate(TASK_TEMPLATE):
        if ttype.value in existing:
            continue
        task = OffboardingTask(
            id=ids.task_id(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            task_type=ttype.value,
            status=TaskStatus.PENDING.value,
            required=required,
            sequence=seq,
            assignee_role=role,
            sla_due_at=now + timedelta(hours=sla_hours),
        )
        repo.add(task)
        created.append(task)
    repo.flush()
    return created


def complete_task(repo: Repository, task: OffboardingTask, *, actor_id: Optional[int],
                  result: Optional[dict] = None, note: Optional[str] = None) -> OffboardingTask:
    task.status = TaskStatus.COMPLETED.value
    task.completed_at = _now()
    task.completed_by_id = actor_id
    if result is not None:
        task.result = result
    if note:
        task.notes = note
    repo.flush()
    return task


def raise_blocker(repo: Repository, case: OffboardingCase, *, blocker_type: str,
                  reason: Optional[str], severity: str = "high",
                  task_id: Optional[str] = None, actor_id: Optional[int] = None) -> WorkflowBlocker:
    blk = WorkflowBlocker(
        id=ids.blocker_id(),
        tenant_id=case.tenant_id,
        case_id=case.id,
        task_id=task_id,
        blocker_type=blocker_type,
        severity=severity,
        reason=reason,
        status="open",
        raised_by_id=actor_id,
    )
    repo.add(blk)
    if task_id:
        task = repo.get_task(task_id)
        if task is not None:
            task.status = TaskStatus.BLOCKED.value
    repo.flush()
    return blk


def resolve_blocker(repo: Repository, blocker: WorkflowBlocker, *, actor_id: Optional[int],
                    waiver_id: Optional[str] = None) -> WorkflowBlocker:
    blocker.status = "waived" if waiver_id else "resolved"
    blocker.resolved_by_id = actor_id
    blocker.resolved_at = _now()
    blocker.waiver_id = waiver_id
    repo.flush()
    return blocker


def grant_waiver(repo: Repository, case: OffboardingCase, *, reason: str,
                 justification: Optional[str], actor_id: Optional[int],
                 task_id: Optional[str] = None, blocker_id: Optional[str] = None) -> WorkflowWaiver:
    wvr = WorkflowWaiver(
        id=ids.waiver_id(),
        tenant_id=case.tenant_id,
        case_id=case.id,
        task_id=task_id,
        blocker_id=blocker_id,
        reason=reason,
        justification=justification,
        status="active",
        waived_by_id=actor_id,
    )
    repo.add(wvr)
    # Waiving a task marks it WAIVED so it no longer blocks readiness.
    if task_id:
        task = repo.get_task(task_id)
        if task is not None:
            task.status = TaskStatus.WAIVED.value
            task.waiver_id = wvr.id
    # Waiving a blocker resolves it.
    if blocker_id:
        blk = repo.get_blocker(blocker_id)
        if blk is not None:
            resolve_blocker(repo, blk, actor_id=actor_id, waiver_id=wvr.id)
    repo.flush()
    return wvr


def sweep_slas(repo: Repository, case_id: str, *, escalate: bool = True) -> List[OffboardingTask]:
    """Mark overdue open tasks as SLA-breached and (optionally) escalated.
    Returns the tasks whose state changed. Idempotent."""
    now = _now()
    changed: List[OffboardingTask] = []
    for task in repo.tasks_for_case(case_id):
        if task.status in (TaskStatus.COMPLETED.value, TaskStatus.WAIVED.value):
            continue
        if task.sla_due_at and now > _as_utc(task.sla_due_at):
            dirty = False
            if not task.sla_breached:
                task.sla_breached = True
                dirty = True
            if escalate and not task.escalated:
                task.escalated = True
                task.escalated_at = now
                dirty = True
            if dirty:
                changed.append(task)
    repo.flush()
    return changed


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ════════════════════════════════════════════════════════════════════════════
# COMPLETION READINESS  — the gate that allows / blocks COMPLETED
# ════════════════════════════════════════════════════════════════════════════
def compute_readiness(repo: Repository, case: OffboardingCase) -> CompletionReadiness:
    tasks = repo.tasks_for_case(case.id)
    revocations = repo.revocations_for_case(case.id)
    open_blockers = repo.blockers_for_case(case.id, open_only=True)
    retention = repo.retention_for_case(case.id)
    certificate = repo.certificate_for_case(case.id)

    by_type = {t.task_type: t for t in tasks}

    def _done(ttype: TaskType) -> bool:
        t = by_type.get(ttype.value)
        return bool(t and t.status in (TaskStatus.COMPLETED.value, TaskStatus.WAIVED.value))

    # Access fully revoked: at least one record, and every record is in a
    # terminal-good state (revoked / not_applicable / reversed-not-counted).
    terminal_ok = {RevocationStatus.REVOKED.value, RevocationStatus.NOT_APPLICABLE.value}
    access_fully_revoked = bool(revocations) and all(r.status in terminal_ok for r in revocations)

    tracking_stopped = _done(TaskType.TRACKING_STOP_VERIFY)
    payroll_closed = _done(TaskType.PAYROLL_CLOSURE)
    billing_closed = _done(TaskType.BILLING_CLOSURE)
    retention_assigned = retention is not None
    certificate_issued = certificate is not None
    legal_hold_active = case.legal_hold_status == "active"

    required = [t for t in tasks if t.required]
    required_done = [t for t in required
                     if t.status in (TaskStatus.COMPLETED.value, TaskStatus.WAIVED.value)]

    reasons: List[str] = []
    if not access_fully_revoked:
        reasons.append("access_not_fully_revoked")
    if not tracking_stopped:
        reasons.append("tracking_not_stopped")
    if open_blockers:
        reasons.append(f"{len(open_blockers)}_open_blockers")
    if len(required_done) < len(required):
        reasons.append("required_tasks_incomplete")
    if not payroll_closed:
        reasons.append("payroll_not_closed")
    if not billing_closed:
        reasons.append("billing_not_closed")
    if not retention_assigned:
        reasons.append("retention_not_assigned")
    if legal_hold_active:
        reasons.append("legal_hold_active")

    ready = (
        len(reasons) == 0
        and case.status not in (
            CaseState.COMPLETED.value, CaseState.CANCELLED.value,
            CaseState.REVERSED.value, CaseState.DRAFT.value,
        )
    )

    return CompletionReadiness(
        ready=ready,
        case_status=case.status,
        access_fully_revoked=access_fully_revoked,
        tracking_stopped=tracking_stopped,
        open_blockers=len(open_blockers),
        required_tasks_total=len(required),
        required_tasks_done=len(required_done),
        legal_hold_active=legal_hold_active,
        payroll_closed=payroll_closed,
        billing_closed=billing_closed,
        retention_assigned=retention_assigned,
        certificate_issued=certificate_issued,
        blocking_reasons=reasons,
    )
