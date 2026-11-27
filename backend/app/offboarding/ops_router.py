"""Operations & QA-hardening API (§3 monitoring, §4 edge cases, §6 audit checker).

Mounted under ``/api/offboarding``. Read endpoints require ``audit.view``;
edge-case mutations authorize inside ``EdgeCaseService`` per action. All reuse
the lifecycle router's context / commit / error machinery and stay tenant-
scoped.

    GET  /metrics                       — JSON gauges (blocked/SLA/revocation/…)
    GET  /metrics/prometheus            — same gauges, Prometheus text format
    GET  /audit-consistency             — audit completeness + chain + hashes
    GET  /flags                         — live kill-switch state (observability)

    POST /edge/rehire                   — controlled reversal + conflict scan
    POST /edge/transfer                 — mid-flight department move
    POST /edge/reassign-task            — manager/owner reassignment
    POST /edge/dependency-unavailable   — payroll/billing/integration blocker
    GET  /edge/classify                 — detect edge conditions on a case
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import audit_checker, flags, metrics, rbac
from .edge_cases import EdgeCaseService, TenantDeleted
from .rbac import AuthorizationError
from .router import _execute, get_context, rate_limit
from .security_service import CrossTenantOrMissing
from .services import OffboardingContext, ServiceError

router = APIRouter(prefix="/api/offboarding", tags=["Offboarding Ops"])


def _require_audit_view(ctx: OffboardingContext):
    if not rbac.can(ctx.authz, "audit.view"):
        raise HTTPException(status_code=403, detail="missing permission audit.view")


# ════════════════════════════════════════════════════════════════════════════
# MONITORING
# ════════════════════════════════════════════════════════════════════════════
@router.get("/metrics")
def get_metrics(ctx: OffboardingContext = Depends(get_context)):
    _require_audit_view(ctx)
    snap = metrics.compute_metrics(ctx.db, tenant_id=ctx.tenant_id)
    snap["flags"] = flags.snapshot()
    return snap


@router.get("/metrics/prometheus", response_class=PlainTextResponse)
def get_metrics_prometheus(ctx: OffboardingContext = Depends(get_context)):
    _require_audit_view(ctx)
    return metrics.prometheus_text(ctx.db, tenant_id=ctx.tenant_id)


@router.get("/flags")
def get_flags(ctx: OffboardingContext = Depends(get_context)):
    _require_audit_view(ctx)
    return flags.snapshot()


# ════════════════════════════════════════════════════════════════════════════
# AUDIT CONSISTENCY CHECKER (§6)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/audit-consistency")
def audit_consistency(ctx: OffboardingContext = Depends(get_context),
                      case_id: Optional[str] = Query(default=None, max_length=40),
                      only_completed: bool = Query(default=False)):
    _require_audit_view(ctx)
    if case_id:
        case = ctx.repo().get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        return audit_checker.check_case(ctx.db, case).as_dict()
    return audit_checker.check_tenant(ctx.db, tenant_id=ctx.tenant_id,
                                      only_completed=only_completed)


# ════════════════════════════════════════════════════════════════════════════
# EDGE CASES (§4)
# ════════════════════════════════════════════════════════════════════════════
class RehireBody(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=2000)


class TransferBody(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    new_department: str = Field(min_length=1, max_length=100)
    new_team: Optional[str] = Field(default=None, max_length=100)


class ReassignBody(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    task_type: str = Field(min_length=1, max_length=40)
    new_assignee_id: int
    new_role: Optional[str] = Field(default=None, max_length=40)


class DependencyBody(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    dependency: str = Field(min_length=1, max_length=40)
    detail: Optional[str] = Field(default=None, max_length=2000)


def _edge(ctx: OffboardingContext = Depends(get_context)) -> EdgeCaseService:
    return EdgeCaseService(ctx)


def _run_edge(ctx, fn):
    """Commit/translate wrapper that also maps the edge-specific errors."""
    def _inner():
        try:
            return fn()
        except TenantDeleted as e:
            raise HTTPException(status_code=409, detail={"code": e.code, "message": e.message})
        except CrossTenantOrMissing:
            raise HTTPException(status_code=404, detail="case not found")
    return _execute(ctx.db, _inner)


@router.post("/edge/rehire")
def edge_rehire(payload: RehireBody, ctx: OffboardingContext = Depends(get_context),
                edge: EdgeCaseService = Depends(_edge), _rl: None = Depends(rate_limit)):
    return _run_edge(ctx, lambda: edge.rehire(payload.case_id, reason=payload.reason))


@router.post("/edge/transfer")
def edge_transfer(payload: TransferBody, ctx: OffboardingContext = Depends(get_context),
                  edge: EdgeCaseService = Depends(_edge), _rl: None = Depends(rate_limit)):
    return _run_edge(ctx, lambda: edge.transfer(
        payload.case_id, new_department=payload.new_department, new_team=payload.new_team))


@router.post("/edge/reassign-task")
def edge_reassign(payload: ReassignBody, ctx: OffboardingContext = Depends(get_context),
                  edge: EdgeCaseService = Depends(_edge), _rl: None = Depends(rate_limit)):
    return _run_edge(ctx, lambda: edge.reassign_task(
        payload.case_id, task_type=payload.task_type,
        new_assignee_id=payload.new_assignee_id, new_role=payload.new_role))


@router.post("/edge/dependency-unavailable")
def edge_dependency(payload: DependencyBody, ctx: OffboardingContext = Depends(get_context),
                    edge: EdgeCaseService = Depends(_edge), _rl: None = Depends(rate_limit)):
    return _run_edge(ctx, lambda: edge.mark_dependency_unavailable(
        payload.case_id, dependency=payload.dependency, detail=payload.detail))


@router.get("/edge/classify")
def edge_classify(case_id: str = Query(..., max_length=40),
                  ctx: OffboardingContext = Depends(get_context),
                  edge: EdgeCaseService = Depends(_edge)):
    try:
        return edge.classify(case_id)
    except CrossTenantOrMissing:
        raise HTTPException(status_code=404, detail="case not found")
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=e.decision.reason)
    except ServiceError as e:
        raise HTTPException(status_code=e.http_status, detail=e.message)
