"""FastAPI surface for the offboarding lifecycle (§7).

Design choices that satisfy the spec:
  • opaque IDs        — every path id is an opaque ``case_<hex>`` (see ids.py).
  • correlation IDs   — each request carries one (from the X-Correlation-ID
                        header or freshly minted) through every audit + event
                        row and back out in the response header.
  • validation        — Pydantic request models reject malformed input at the
                        edge before any service runs.
  • rate limits       — a lightweight in-process fixed-window limiter guards the
                        mutating routes.
  • secure errors     — service/authorization/transition exceptions map to clean
                        4xx codes; a cross-tenant id is a 404 (never a 403 that
                        would confirm the id exists).
  • audit logging     — handled inside the services; the router just commits.

Routes are mounted under ``/api/offboarding`` by bootstrap.install_router().
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.auth.service import get_current_user
from database.database import get_db
from database.models import User

from . import ids, rbac
from .constants import CaseState
from .models import DEFAULT_TENANT
from .rbac import AuthorizationError, AuthzContext
from .repository import Repository
from .schemas import (
    ApproveRequest, BlockerCreate, CancelRequest, CaseCreate, CaseDetail,
    CaseSummary, CaseUpdate, LegalHoldApply, RegisterResponse, ReverseRequest,
    SubmitRequest, TaskComplete, WaiverCreate,
)
from .services import (
    CompletionBlocked, OffboardingContext, OffboardingService, ServiceError,
)
from .state_machine import InvalidTransition, allowed_next

router = APIRouter(prefix="/api/offboarding", tags=["Offboarding"])


# ════════════════════════════════════════════════════════════════════════════
# RATE LIMITING  (in-process fixed window — single source of truth per worker)
# ════════════════════════════════════════════════════════════════════════════
class _FixedWindowLimiter:
    def __init__(self, limit: int, window_s: int):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_s
        bucket = [t for t in self._hits[key] if t > cutoff]
        if len(bucket) >= self.limit:
            raise HTTPException(status_code=429, detail="rate limit exceeded; slow down")
        bucket.append(now)
        self._hits[key] = bucket


_write_limiter = _FixedWindowLimiter(limit=120, window_s=60)


# ════════════════════════════════════════════════════════════════════════════
# TENANT RESOLUTION + CONTEXT
# ════════════════════════════════════════════════════════════════════════════
def resolve_tenant_id(user: User) -> str:
    """Resolve the actor's tenant. The legacy User model has no tenant column,
    so the current single-tenant deployment resolves to 'default'; real orgs
    carry an ``organization_id`` we honor when present."""
    org_id = getattr(user, "organization_id", None)
    return str(org_id) if org_id is not None else DEFAULT_TENANT


def _client_ip(request: Request) -> Optional[str]:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd
    return request.client.host if request.client else None


def get_context(
    request: Request,
    response: Response,
    x_correlation_id: Optional[str] = Header(default=None, alias="X-Correlation-ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OffboardingContext:
    tenant_id = resolve_tenant_id(current_user)
    roles = rbac.roles_for_user(current_user)
    authz = AuthzContext(
        user_id=current_user.id,
        tenant_id=tenant_id,
        roles=roles,
        department=getattr(current_user, "department", None),
        jurisdiction=None,
    )
    correlation_id = x_correlation_id or ids.correlation_id()
    # Echo the correlation id back so the client can trace the whole fan-out.
    response.headers["X-Correlation-ID"] = correlation_id
    return OffboardingContext(db=db, authz=authz, correlation_id=correlation_id,
                              ip=_client_ip(request))


def get_service(ctx: OffboardingContext = Depends(get_context)) -> OffboardingService:
    return OffboardingService(ctx)


def rate_limit(ctx: OffboardingContext = Depends(get_context)) -> None:
    _write_limiter.check(f"{ctx.tenant_id}:{ctx.actor_id}")


# ════════════════════════════════════════════════════════════════════════════
# ERROR TRANSLATION + COMMIT
# ════════════════════════════════════════════════════════════════════════════
def _execute(db: Session, fn: Callable):
    """Run a service call, commit on success, and translate domain exceptions
    into clean HTTP errors (rolling back first)."""
    try:
        result = fn()
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except AuthorizationError as e:
        db.rollback()
        raise HTTPException(status_code=403, detail=e.decision.reason or "forbidden")
    except InvalidTransition as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except CompletionBlocked as e:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail={"code": e.code, "reasons": e.reasons})
    except ServiceError as e:
        db.rollback()
        raise HTTPException(status_code=e.http_status,
                            detail={"code": e.code, "message": e.message})
    except Exception:
        db.rollback()
        raise


# ════════════════════════════════════════════════════════════════════════════
# SERIALIZATION
# ════════════════════════════════════════════════════════════════════════════
def _summary(case) -> CaseSummary:
    return CaseSummary.model_validate(case)


def _detail(svc: OffboardingService, ctx: OffboardingContext, case) -> CaseDetail:
    from .schemas import BlockerOut, RevocationOut, TaskOut
    from .workflow import compute_readiness

    repo = ctx.repo()
    base = {
        "id": case.id, "tenant_id": case.tenant_id, "engagement_id": case.engagement_id,
        "worker_id": case.worker_id, "status": case.status,
        "blocked_from_state": case.blocked_from_state, "exit_type": case.exit_type,
        "jurisdiction": case.jurisdiction, "worker_type": case.worker_type,
        "risk_level": case.risk_level, "department": case.department,
        "data_classification": case.data_classification,
        "legal_hold_status": case.legal_hold_status, "revocation_mode": case.revocation_mode,
        "final_access_at": case.final_access_at, "termination_reason": case.termination_reason,
        "notes": case.notes, "correlation_id": case.correlation_id,
        "created_at": case.created_at, "updated_at": case.updated_at,
    }
    # Field-level protection: redact sensitive columns the actor can't read.
    base = rbac.redact_case(base, ctx.authz)

    return CaseDetail(
        **base,
        tasks=[TaskOut.model_validate(t) for t in repo.tasks_for_case(case.id)],
        revocations=[RevocationOut.model_validate(r) for r in repo.revocations_for_case(case.id)],
        blockers=[BlockerOut.model_validate(b) for b in repo.blockers_for_case(case.id)],
        readiness=compute_readiness(repo, case),
        allowed_actions=sorted(s.value for s in allowed_next(case.status)),
    )


# ════════════════════════════════════════════════════════════════════════════
# CASE CRUD
# ════════════════════════════════════════════════════════════════════════════
@router.post("/cases", status_code=201)
def create_case(
    payload: CaseCreate,
    ctx: OffboardingContext = Depends(get_context),
    svc: OffboardingService = Depends(get_service),
    _rl: None = Depends(rate_limit),
):
    case = _execute(ctx.db, lambda: svc.create_case(payload))
    return _summary(case)


@router.get("/cases", response_model=RegisterResponse)
def list_cases(
    ctx: OffboardingContext = Depends(get_context),
    status: Optional[str] = Query(default=None),
    worker_id: Optional[int] = Query(default=None),
    engagement_id: Optional[str] = Query(default=None),
    legal_hold_status: Optional[str] = Query(default=None),
    department: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    # Read authorization: the actor needs case.view.
    if not rbac.can(ctx.authz, "case.view"):
        raise HTTPException(status_code=403, detail="missing permission case.view")
    repo = Repository(ctx.db, ctx.tenant_id)
    rows = repo.list_cases(status=status, worker_id=worker_id, engagement_id=engagement_id,
                           legal_hold_status=legal_hold_status, department=department,
                           limit=limit, offset=offset)
    return RegisterResponse(
        items=[_summary(c) for c in rows],
        total=repo.count_cases(status=status),
        limit=limit, offset=offset,
    )


@router.get("/cases/{case_id}", response_model=CaseDetail)
def get_case(case_id: str, ctx: OffboardingContext = Depends(get_context),
             svc: OffboardingService = Depends(get_service)):
    def _run():
        case = ctx.repo().get_case(case_id)
        if case is None:
            raise ServiceError("case not found", "not_found")  # → 404 below via override
        rbac.require(ctx.authz, "case.view", _resource_of(case))
        return _detail(svc, ctx, case)
    try:
        return _run()
    except ServiceError:
        raise HTTPException(status_code=404, detail="case not found")
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=e.decision.reason)


@router.patch("/cases/{case_id}", response_model=CaseDetail)
def update_case(case_id: str, payload: CaseUpdate,
                ctx: OffboardingContext = Depends(get_context),
                svc: OffboardingService = Depends(get_service),
                _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.update_case(case_id, payload))
    return _detail(svc, ctx, case)


# ════════════════════════════════════════════════════════════════════════════
# LIFECYCLE ACTIONS
# ════════════════════════════════════════════════════════════════════════════
@router.post("/cases/{case_id}/submit", response_model=CaseDetail)
def submit_case(case_id: str, payload: SubmitRequest = SubmitRequest(),
                ctx: OffboardingContext = Depends(get_context),
                svc: OffboardingService = Depends(get_service),
                _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.submit(case_id, note=payload.note))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/approve", response_model=CaseDetail)
def approve_case(case_id: str, payload: ApproveRequest = ApproveRequest(),
                 ctx: OffboardingContext = Depends(get_context),
                 svc: OffboardingService = Depends(get_service),
                 _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.approve(
        case_id, schedule=payload.schedule, final_access_at=payload.final_access_at,
        note=payload.note))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/cancel", response_model=CaseDetail)
def cancel_case(case_id: str, payload: CancelRequest,
                ctx: OffboardingContext = Depends(get_context),
                svc: OffboardingService = Depends(get_service),
                _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.cancel(case_id, reason=payload.reason))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/reverse", response_model=CaseDetail)
def reverse_case(case_id: str, payload: ReverseRequest,
                 ctx: OffboardingContext = Depends(get_context),
                 svc: OffboardingService = Depends(get_service),
                 _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.reverse(case_id, reason=payload.reason))
    return _detail(svc, ctx, case)


# ── closure workflow steps ───────────────────────────────────────────────────
@router.post("/cases/{case_id}/begin", response_model=CaseDetail)
def begin_offboarding(case_id: str, ctx: OffboardingContext = Depends(get_context),
                      svc: OffboardingService = Depends(get_service),
                      _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.begin_offboarding(case_id))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/close-access", response_model=CaseDetail)
def close_access(case_id: str, ctx: OffboardingContext = Depends(get_context),
                 svc: OffboardingService = Depends(get_service),
                 _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.execute_access_closure(case_id))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/retry-access", response_model=CaseDetail)
def retry_access(case_id: str, ctx: OffboardingContext = Depends(get_context),
                 svc: OffboardingService = Depends(get_service),
                 _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.retry_access_closure(case_id))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/final-review", response_model=CaseDetail)
def final_review(case_id: str, ctx: OffboardingContext = Depends(get_context),
                 svc: OffboardingService = Depends(get_service),
                 _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.complete_final_review(case_id))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/close-payroll", response_model=CaseDetail)
def close_payroll(case_id: str, payload: TaskComplete = TaskComplete(),
                  ctx: OffboardingContext = Depends(get_context),
                  svc: OffboardingService = Depends(get_service),
                  _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.close_payroll(case_id, note=payload.note))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/close-billing", response_model=CaseDetail)
def close_billing(case_id: str, payload: TaskComplete = TaskComplete(),
                  ctx: OffboardingContext = Depends(get_context),
                  svc: OffboardingService = Depends(get_service),
                  _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.close_billing(case_id, note=payload.note))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/review-legal-hold", response_model=CaseDetail)
def review_legal_hold(case_id: str, ctx: OffboardingContext = Depends(get_context),
                      svc: OffboardingService = Depends(get_service),
                      _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.review_legal_hold(case_id))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/setup-former-access")
def setup_former_access(case_id: str, ctx: OffboardingContext = Depends(get_context),
                        svc: OffboardingService = Depends(get_service),
                        _rl: None = Depends(rate_limit)):
    access = _execute(ctx.db, lambda: svc.setup_former_access(case_id))
    return {"id": access.id, "status": access.status, "scopes": access.scopes,
            "expires_at": access.expires_at}


@router.post("/cases/{case_id}/generate-certificate")
def generate_certificate(case_id: str, ctx: OffboardingContext = Depends(get_context),
                         svc: OffboardingService = Depends(get_service),
                         _rl: None = Depends(rate_limit)):
    cert = _execute(ctx.db, lambda: svc.generate_certificate(case_id))
    return {"id": cert.id, "certificate_number": cert.certificate_number,
            "content_hash": cert.content_hash, "issued_at": cert.issued_at}


@router.post("/cases/{case_id}/complete", response_model=CaseDetail)
def complete_case(case_id: str, ctx: OffboardingContext = Depends(get_context),
                  svc: OffboardingService = Depends(get_service),
                  _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.complete(case_id))
    return _detail(svc, ctx, case)


# ── legal hold ────────────────────────────────────────────────────────────────
@router.post("/cases/{case_id}/legal-hold")
def apply_legal_hold(case_id: str, payload: LegalHoldApply,
                     ctx: OffboardingContext = Depends(get_context),
                     svc: OffboardingService = Depends(get_service),
                     _rl: None = Depends(rate_limit)):
    def _run():
        case = svc._get(case_id)
        rbac.require(ctx.authz, "legal_hold.apply", _resource_of(case))
        return svc.legal.apply(case, matter_ref=payload.matter_ref, reason=payload.reason,
                               notes=payload.notes, review_due_at=payload.review_due_at)
    hold = _execute(ctx.db, _run)
    return {"id": hold.id, "status": hold.status, "matter_ref": hold.matter_ref}


# ── blockers / waivers ────────────────────────────────────────────────────────
@router.post("/cases/{case_id}/blockers")
def add_blocker(case_id: str, payload: BlockerCreate,
                ctx: OffboardingContext = Depends(get_context),
                svc: OffboardingService = Depends(get_service),
                _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.block(
        case_id, blocker_type=payload.blocker_type, reason=payload.reason,
        severity=payload.severity))
    return _detail(svc, ctx, case)


@router.post("/cases/{case_id}/waivers")
def add_waiver(case_id: str, payload: WaiverCreate,
               ctx: OffboardingContext = Depends(get_context),
               svc: OffboardingService = Depends(get_service),
               _rl: None = Depends(rate_limit)):
    def _run():
        case = svc._get(case_id)
        rbac.require(ctx.authz, "waiver.grant", _resource_of(case))
        from .workflow import grant_waiver
        return grant_waiver(ctx.repo(), case, reason=payload.reason,
                            justification=payload.justification, actor_id=ctx.actor_id,
                            task_id=payload.task_id, blocker_id=payload.blocker_id)
    wvr = _execute(ctx.db, _run)
    return {"id": wvr.id, "status": wvr.status}


@router.post("/cases/{case_id}/unblock", response_model=CaseDetail)
def unblock_case(case_id: str, ctx: OffboardingContext = Depends(get_context),
                 svc: OffboardingService = Depends(get_service),
                 _rl: None = Depends(rate_limit)):
    case = _execute(ctx.db, lambda: svc.unblock(case_id))
    return _detail(svc, ctx, case)


# ════════════════════════════════════════════════════════════════════════════
# READ-ONLY CONTRACT VIEWS  (§8)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/cases/{case_id}/readiness")
def get_readiness(case_id: str, ctx: OffboardingContext = Depends(get_context),
                  svc: OffboardingService = Depends(get_service)):
    try:
        return svc.readiness(case_id).model_dump()
    except ServiceError:
        raise HTTPException(status_code=404, detail="case not found")
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=e.decision.reason)


@router.get("/cases/{case_id}/timeline")
def get_timeline(case_id: str, ctx: OffboardingContext = Depends(get_context)):
    repo = ctx.repo()
    case = repo.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    if not rbac.can(ctx.authz, "case.view", _resource_of(case)):
        raise HTTPException(status_code=403, detail="forbidden")
    return [
        {"event_type": e.event_type, "occurred_at": e.occurred_at, "state_from": e.state_from,
         "state_to": e.state_to, "correlation_id": e.correlation_id, "payload": e.payload}
        for e in repo.events_for_case(case_id)
    ]


@router.get("/cases/{case_id}/audit")
def get_audit_trail(case_id: str, ctx: OffboardingContext = Depends(get_context)):
    repo = ctx.repo()
    case = repo.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    if not rbac.can(ctx.authz, "audit.view", _resource_of(case)):
        raise HTTPException(status_code=403, detail="missing permission audit.view")
    return [
        {"id": a.id, "action": a.action, "entity": a.entity, "actor_id": a.actor_id,
         "actor_role": a.actor_role, "created_at": a.created_at, "current_hash": a.current_hash,
         "payload": a.payload}
        for a in repo.audit_for_case(case_id)
    ]


@router.get("/cases/{case_id}/blockers")
def get_blockers(case_id: str, ctx: OffboardingContext = Depends(get_context)):
    repo = ctx.repo()
    case = repo.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    if not rbac.can(ctx.authz, "case.view", _resource_of(case)):
        raise HTTPException(status_code=403, detail="forbidden")
    return [
        {"id": b.id, "blocker_type": b.blocker_type, "severity": b.severity, "reason": b.reason,
         "status": b.status, "task_id": b.task_id, "resolved_at": b.resolved_at,
         "created_at": b.created_at}
        for b in repo.blockers_for_case(case_id)
    ]


# Small helper to build ABAC resource attrs from a case row (router-local).
def _resource_of(case):
    from .rbac import ResourceAttrs
    return ResourceAttrs(
        tenant_id=case.tenant_id, department=case.department, jurisdiction=case.jurisdiction,
        worker_type=case.worker_type, exit_type=case.exit_type, risk_level=case.risk_level,
        legal_hold_status=case.legal_hold_status, case_status=case.status,
        data_classification=case.data_classification,
    )
