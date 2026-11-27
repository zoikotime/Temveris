"""Governance HTTP surface (§8 — governance spec).

    POST   /api/offboarding/governance/former-access
    DELETE /api/offboarding/governance/former-access
    POST   /api/offboarding/governance/legal-hold
    DELETE /api/offboarding/governance/legal-hold
    POST   /api/offboarding/governance/retention
    GET    /api/offboarding/governance/certificate
    GET    /api/offboarding/governance/evidence-package

Plus the §4 deletion-review surface:
    POST /governance/deletion-review                — open
    POST /governance/deletion-review/{id}/approve   — legal | compliance signs
    POST /governance/deletion-review/{id}/deny
    GET  /governance/deletion-review                — list pending

Reuses the lifecycle router's context / RBAC / commit / error machinery — every
governance route is rate-limited, tenant-isolated, and audited via the same
correlation-id propagation as `/api/offboarding`.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .governance_service import (
    DeletionBlocked, DualApprovalRequired, GovernanceService,
)
from .router import _execute, get_context, rate_limit
from .security_service import CrossTenantOrMissing
from .services import OffboardingContext, ServiceError

router = APIRouter(prefix="/api/offboarding/governance", tags=["Offboarding Governance"])


def get_governance(ctx: OffboardingContext = Depends(get_context)) -> GovernanceService:
    return GovernanceService(ctx)


# ════════════════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ════════════════════════════════════════════════════════════════════════════
class FormerAccessGrant(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    scopes: Optional[list[str]] = None
    expires_in_days: int = Field(default=365, ge=1, le=3650)


class LegalHoldApply(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    matter_ref: Optional[str] = Field(default=None, max_length=120)
    reason: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)


class LegalHoldRelease(BaseModel):
    hold_id: str = Field(min_length=1, max_length=40)
    reason: Optional[str] = Field(default=None, max_length=4000)


class RetentionAssign(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    policy_code: Optional[str] = Field(default=None, max_length=60)


class PolicyVersionPublish(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    retention_days: int = Field(ge=1, le=99 * 365)
    description: Optional[str] = Field(default=None, max_length=2000)
    jurisdiction: Optional[str] = Field(default=None, max_length=40)


class DeletionReviewOpen(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    scope: str = Field(default="full_record", max_length=60)
    notes: Optional[str] = Field(default=None, max_length=4000)


class DeletionReviewApprove(BaseModel):
    role: str = Field(pattern=r"^(legal|compliance)$")
    note: Optional[str] = Field(default=None, max_length=2000)


class DeletionReviewDeny(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


class LegalEvidenceBuild(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    manifest_extra: Optional[dict] = None


class AuditEvidenceBuild(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)


# ════════════════════════════════════════════════════════════════════════════
# FORMER-WORKER ACCESS
# ════════════════════════════════════════════════════════════════════════════
@router.post("/former-access")
def grant_former_access(payload: FormerAccessGrant,
                        ctx: OffboardingContext = Depends(get_context),
                        gov: GovernanceService = Depends(get_governance),
                        _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.grant_former_access(
        case_id=payload.case_id, scopes=payload.scopes,
        expires_in_days=payload.expires_in_days))


@router.delete("/former-access")
def revoke_former_access(case_id: str = Query(..., max_length=40),
                         ctx: OffboardingContext = Depends(get_context),
                         gov: GovernanceService = Depends(get_governance),
                         _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.revoke_former_access(case_id=case_id))


# ════════════════════════════════════════════════════════════════════════════
# LEGAL HOLD
# ════════════════════════════════════════════════════════════════════════════
@router.post("/legal-hold")
def apply_legal_hold(payload: LegalHoldApply,
                     ctx: OffboardingContext = Depends(get_context),
                     gov: GovernanceService = Depends(get_governance),
                     _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.apply_legal_hold(
        case_id=payload.case_id, matter_ref=payload.matter_ref,
        reason=payload.reason, notes=payload.notes))


@router.delete("/legal-hold")
def release_legal_hold(payload: LegalHoldRelease = Body(...),
                       ctx: OffboardingContext = Depends(get_context),
                       gov: GovernanceService = Depends(get_governance),
                       _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.release_legal_hold(
        hold_id=payload.hold_id, reason=payload.reason))


# ════════════════════════════════════════════════════════════════════════════
# RETENTION
# ════════════════════════════════════════════════════════════════════════════
@router.post("/retention")
def assign_retention(payload: RetentionAssign,
                     ctx: OffboardingContext = Depends(get_context),
                     gov: GovernanceService = Depends(get_governance),
                     _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.assign_retention(
        case_id=payload.case_id, policy_code=payload.policy_code))


@router.get("/retention/policies")
def list_policies(ctx: OffboardingContext = Depends(get_context),
                  gov: GovernanceService = Depends(get_governance)):
    try:
        return gov.list_policies()
    except ServiceError as e:
        raise HTTPException(status_code=e.http_status, detail=e.message)


@router.post("/retention/policies/version")
def publish_policy_version(payload: PolicyVersionPublish,
                           ctx: OffboardingContext = Depends(get_context),
                           gov: GovernanceService = Depends(get_governance),
                           _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.publish_policy_version(
        code=payload.code, retention_days=payload.retention_days,
        description=payload.description, jurisdiction=payload.jurisdiction))


# ════════════════════════════════════════════════════════════════════════════
# CERTIFICATE + EVIDENCE PACKAGES
# ════════════════════════════════════════════════════════════════════════════
@router.get("/certificate")
def get_certificate(case_id: str = Query(..., max_length=40),
                    ctx: OffboardingContext = Depends(get_context),
                    gov: GovernanceService = Depends(get_governance)):
    try:
        return gov.get_certificate(case_id=case_id)
    except CrossTenantOrMissing:
        raise HTTPException(status_code=404, detail="case not found")
    except ServiceError as e:
        raise HTTPException(status_code=e.http_status, detail=e.message)


@router.get("/evidence-package")
def get_evidence_package(case_id: str = Query(..., max_length=40),
                         package_type: Optional[str] = Query(default=None, max_length=40),
                         ctx: OffboardingContext = Depends(get_context),
                         gov: GovernanceService = Depends(get_governance)):
    try:
        return gov.get_evidence_packages(case_id=case_id, package_type=package_type)
    except CrossTenantOrMissing:
        raise HTTPException(status_code=404, detail="case not found")
    except ServiceError as e:
        raise HTTPException(status_code=e.http_status, detail=e.message)


@router.post("/evidence-package/legal")
def build_legal_evidence(payload: LegalEvidenceBuild,
                         ctx: OffboardingContext = Depends(get_context),
                         gov: GovernanceService = Depends(get_governance),
                         _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.build_legal_evidence(
        case_id=payload.case_id, manifest_extra=payload.manifest_extra))


@router.post("/evidence-package/audit")
def build_audit_evidence(payload: AuditEvidenceBuild,
                         ctx: OffboardingContext = Depends(get_context),
                         gov: GovernanceService = Depends(get_governance),
                         _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.build_audit_evidence(case_id=payload.case_id))


# ════════════════════════════════════════════════════════════════════════════
# DELETION ELIGIBILITY REVIEW (§4 — dual approval before any deletion)
# ════════════════════════════════════════════════════════════════════════════
@router.post("/deletion-review")
def open_deletion_review(payload: DeletionReviewOpen,
                         ctx: OffboardingContext = Depends(get_context),
                         gov: GovernanceService = Depends(get_governance),
                         _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.open_deletion_review(
        case_id=payload.case_id, scope=payload.scope, notes=payload.notes))


@router.post("/deletion-review/{review_id}/approve")
def approve_deletion(review_id: str, payload: DeletionReviewApprove,
                     ctx: OffboardingContext = Depends(get_context),
                     gov: GovernanceService = Depends(get_governance),
                     _rl: None = Depends(rate_limit)):
    def _run():
        try:
            return gov.approve_deletion(review_id=review_id, role=payload.role,
                                        note=payload.note)
        except DeletionBlocked as e:
            raise HTTPException(status_code=409,
                                detail={"code": e.code, "blockers": e.blockers})
        except DualApprovalRequired as e:
            raise HTTPException(status_code=409,
                                detail={"code": e.code, "missing": e.missing})
    return _execute(ctx.db, _run)


@router.post("/deletion-review/{review_id}/deny")
def deny_deletion(review_id: str, payload: DeletionReviewDeny,
                  ctx: OffboardingContext = Depends(get_context),
                  gov: GovernanceService = Depends(get_governance),
                  _rl: None = Depends(rate_limit)):
    return _execute(ctx.db, lambda: gov.deny_deletion(
        review_id=review_id, reason=payload.reason))


@router.get("/deletion-review")
def list_deletion_reviews(case_id: Optional[str] = Query(default=None, max_length=40),
                          status: Optional[str] = Query(default=None, max_length=20),
                          ctx: OffboardingContext = Depends(get_context),
                          gov: GovernanceService = Depends(get_governance)):
    from .models import DeletionEligibilityReview
    q = (ctx.db.query(DeletionEligibilityReview)
         .filter(DeletionEligibilityReview.tenant_id == ctx.tenant_id))
    if case_id:
        q = q.filter(DeletionEligibilityReview.case_id == case_id)
    if status:
        q = q.filter(DeletionEligibilityReview.status == status)
    rows = q.order_by(DeletionEligibilityReview.created_at.desc()).limit(200).all()
    return [gov._review_dict(r) for r in rows]
