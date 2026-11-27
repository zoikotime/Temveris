"""Security API surface (§7) — mounted under ``/api/offboarding``.

    POST /api/offboarding/revoke-access
    POST /api/offboarding/retry-revocation
    GET  /api/offboarding/reconciliation
    GET  /api/offboarding/security-events

Reuses the main router's context/commit/error machinery so these routes inherit
the same correlation-id propagation, RBAC+ABAC, tenant isolation (cross-tenant
ids → 404), rate limiting and append-only audit as the lifecycle routes.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from . import rbac
from .rbac import AuthorizationError
from .router import _execute, get_context, rate_limit
from .schemas import RetryRevocationRequest, RevokeAccessRequest
from .security_service import SecurityService
from .services import OffboardingContext, ServiceError

router = APIRouter(prefix="/api/offboarding", tags=["Offboarding Security"])


def get_security_service(ctx: OffboardingContext = Depends(get_context)) -> SecurityService:
    return SecurityService(ctx)


@router.post("/revoke-access")
def revoke_access(payload: RevokeAccessRequest,
                  ctx: OffboardingContext = Depends(get_context),
                  sec: SecurityService = Depends(get_security_service),
                  _rl: None = Depends(rate_limit)):
    if payload.worker_id is None and not payload.case_id and not payload.engagement_id:
        raise HTTPException(status_code=422,
                            detail="one of worker_id, case_id, engagement_id is required")
    return _execute(ctx.db, lambda: sec.revoke_access(
        worker_id=payload.worker_id, case_id=payload.case_id,
        engagement_id=payload.engagement_id, reason=payload.reason))


@router.post("/retry-revocation")
def retry_revocation(payload: RetryRevocationRequest,
                     ctx: OffboardingContext = Depends(get_context),
                     sec: SecurityService = Depends(get_security_service),
                     _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: sec.retry_revocation(case_id=payload.case_id))


@router.get("/reconciliation")
def get_reconciliation(ctx: OffboardingContext = Depends(get_context),
                       sec: SecurityService = Depends(get_security_service),
                       status: Optional[str] = Query(default=None),
                       integration: Optional[str] = Query(default=None),
                       limit: int = Query(default=100, ge=1, le=500),
                       offset: int = Query(default=0, ge=0)):
    try:
        return sec.reconciliation_view(status=status, integration=integration,
                                       limit=limit, offset=offset)
    except ServiceError as e:
        raise HTTPException(status_code=403 if e.code == "forbidden" else e.http_status,
                            detail=e.message)
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=e.decision.reason)


@router.get("/security-events")
def get_security_events(ctx: OffboardingContext = Depends(get_context),
                        sec: SecurityService = Depends(get_security_service),
                        severity: Optional[str] = Query(default=None),
                        status: Optional[str] = Query(default=None),
                        incident_type: Optional[str] = Query(default=None),
                        worker_id: Optional[int] = Query(default=None),
                        limit: int = Query(default=100, ge=1, le=500),
                        offset: int = Query(default=0, ge=0)):
    try:
        return sec.security_events(severity=severity, status=status,
                                   incident_type=incident_type, worker_id=worker_id,
                                   limit=limit, offset=offset)
    except ServiceError as e:
        raise HTTPException(status_code=403 if e.code == "forbidden" else e.http_status,
                            detail=e.message)
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=e.decision.reason)
