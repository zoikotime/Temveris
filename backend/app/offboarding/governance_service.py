"""Former-worker lifecycle governance orchestrator (§4-§8 — governance spec).

A façade on top of the existing lifecycle services. It owns three concerns the
spec asks for that did not previously have a single entry point:

  • The seven §8 governance endpoints (former-access grant/revoke, legal-hold
    apply/release, retention assign, certificate fetch, evidence-package fetch).
  • The §4 **deletion eligibility review** with the mandatory dual approval
    (Legal + Compliance) before any deletion / anonymisation may proceed.
  • Issuance of the **legal evidence** and **audit evidence** package types
    (the lifecycle service shipped payroll/billing/completion/access-closure
    only).

The orchestrator is intentionally thin: every state mutation routes through the
existing `OffboardingService`, `LegalHoldService`, `EvidencePackageService`,
and the new `retention_engine`. Authorization is RBAC + ABAC via
`rbac.require`; tenant isolation is the repository's responsibility.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from . import rbac, retention_engine
from .constants import (
    DELETION_REQUIRED_APPROVERS, AlertType, CaseState, DeletionReviewStatus,
    EvidencePackageType, IncidentSeverity, RetentionPolicyCode,
)
from .incidents import open_incident
from .models import (
    CompletionCertificate, DeletionEligibilityReview, EvidencePackage,
    FormerWorkerAccess, LegalHold, OffboardingCase, RetentionPolicy,
)
from .rbac import ResourceAttrs
from .repository import Repository
from .security_service import CrossTenantOrMissing
from .services import OffboardingContext, OffboardingService, ServiceError
from . import ids

_log = logging.getLogger("zoiko.offboarding.governance")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resource(case: OffboardingCase) -> ResourceAttrs:
    return ResourceAttrs(
        tenant_id=case.tenant_id, department=case.department, jurisdiction=case.jurisdiction,
        worker_type=case.worker_type, exit_type=case.exit_type, risk_level=case.risk_level,
        legal_hold_status=case.legal_hold_status, case_status=case.status,
        data_classification=case.data_classification,
    )


class DualApprovalRequired(ServiceError):
    http_status = 409

    def __init__(self, missing: List[str]):
        super().__init__(f"dual approval not complete: missing {missing}",
                         "dual_approval_required")
        self.missing = missing


class DeletionBlocked(ServiceError):
    http_status = 409

    def __init__(self, blockers: List[dict]):
        super().__init__("deletion blocked by gating conditions", "deletion_blocked")
        self.blockers = blockers


class GovernanceService:
    def __init__(self, ctx: OffboardingContext):
        self.ctx = ctx
        self.svc = OffboardingService(ctx)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _repo(self) -> Repository:
        return self.ctx.repo()

    def _get_case(self, case_id: str) -> OffboardingCase:
        case = self._repo().get_case(case_id)
        if case is None:
            raise CrossTenantOrMissing()
        return case

    # ── §8 POST /former-access ───────────────────────────────────────────────
    def grant_former_access(self, *, case_id: str, scopes: Optional[List[str]] = None,
                            expires_in_days: int = 365) -> dict:
        case = self._get_case(case_id)
        access = self.svc.setup_former_access(case_id, scopes=scopes,
                                              expires_in_days=expires_in_days)
        raw_token = getattr(access, "_raw_portal_token", None)
        return {
            "id": access.id, "case_id": access.case_id, "worker_id": access.worker_id,
            "status": access.status, "scopes": access.scopes,
            "granted_at": access.granted_at, "expires_at": access.expires_at,
            # Surfaced ONCE — the raw bearer is never persisted.
            "portal_token": raw_token,
        }

    # ── §8 DELETE /former-access ─────────────────────────────────────────────
    def revoke_former_access(self, *, case_id: str) -> dict:
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "former_access.revoke", _resource(case))
        access = self.svc.lifecycle.revoke_former_access(case)
        if access is None:
            raise ServiceError("no former-worker access on this case", "not_found")
        return {"id": access.id, "status": access.status,
                "revoked_at": access.revoked_at}

    # ── §8 POST /legal-hold ──────────────────────────────────────────────────
    def apply_legal_hold(self, *, case_id: str, matter_ref: Optional[str] = None,
                         reason: Optional[str] = None, notes: Optional[str] = None,
                         review_due_at: Optional[datetime] = None) -> dict:
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "legal_hold.apply", _resource(case))
        hold = self.svc.legal.apply(case, matter_ref=matter_ref, reason=reason,
                                    notes=notes, review_due_at=review_due_at)
        return {"id": hold.id, "case_id": case.id, "status": hold.status,
                "matter_ref": hold.matter_ref, "applied_at": hold.applied_at}

    # ── §8 DELETE /legal-hold ────────────────────────────────────────────────
    def release_legal_hold(self, *, hold_id: str, reason: Optional[str] = None) -> dict:
        hold = self._repo().get_hold(hold_id)
        if hold is None:
            raise CrossTenantOrMissing()
        case = self._get_case(hold.case_id) if hold.case_id else None
        if case is not None:
            rbac.require(self.ctx.authz, "legal_hold.release", _resource(case))
        else:
            rbac.require(self.ctx.authz, "legal_hold.release")
        if hold.status != "active":
            raise ServiceError("legal hold is not active", "not_active")
        if case is None:
            hold.status = "released"
            hold.released_by_id = self.ctx.actor_id
            hold.released_at = _now()
            self.ctx.db.flush()
        else:
            self.svc.legal.release(case, hold)
        return {"id": hold.id, "status": hold.status, "released_at": hold.released_at,
                "reason": reason}

    # ── §8 POST /retention ───────────────────────────────────────────────────
    def assign_retention(self, *, case_id: str, policy_code: Optional[str] = None
                         ) -> dict:
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "retention.assign", _resource(case))
        assignment = retention_engine.assign_to_case(
            self.ctx.db, case, policy_code=policy_code,
            actor_id=self.ctx.actor_id, correlation_id=self.ctx.correlation_id,
        )
        return {
            "id": assignment.id, "case_id": case.id, "policy_code": assignment.policy_code,
            "jurisdiction": assignment.jurisdiction,
            "retention_until": assignment.retention_until,
            "deletion_eligibility": assignment.deletion_eligibility,
        }

    # Versioning surface — supersede a policy with a new retention window.
    def publish_policy_version(self, *, code: str, retention_days: int,
                               description: Optional[str] = None,
                               jurisdiction: Optional[str] = None) -> dict:
        rbac.require(self.ctx.authz, "retention.assign")
        pol = retention_engine.supersede_policy(
            self.ctx.db, tenant_id=self.ctx.tenant_id, code=code,
            new_days=retention_days, description=description, jurisdiction=jurisdiction,
            created_by_id=self.ctx.actor_id,
        )
        return retention_engine.detail(pol)

    def list_policies(self) -> List[dict]:
        rows = (self.ctx.db.query(RetentionPolicy)
                .filter(RetentionPolicy.tenant_id == self.ctx.tenant_id)
                .order_by(RetentionPolicy.code.asc(), RetentionPolicy.version.desc())
                .all())
        return [retention_engine.detail(p) for p in rows]

    # ── §8 GET /certificate ──────────────────────────────────────────────────
    def get_certificate(self, *, case_id: str) -> dict:
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "case.view", _resource(case))
        cert = self._repo().certificate_for_case(case_id)
        if cert is None:
            raise ServiceError("no completion certificate has been generated", "not_found")
        return {
            "id": cert.id, "certificate_number": cert.certificate_number,
            "content_hash": cert.content_hash, "issued_at": cert.issued_at,
            "evidence_package_id": cert.evidence_package_id,
            "payload": cert.payload,
        }

    # ── §8 GET /evidence-package ─────────────────────────────────────────────
    def get_evidence_packages(self, *, case_id: str,
                              package_type: Optional[str] = None) -> List[dict]:
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "case.view", _resource(case))
        q = (self.ctx.db.query(EvidencePackage)
             .filter(EvidencePackage.tenant_id == self.ctx.tenant_id,
                     EvidencePackage.case_id == case_id))
        if package_type:
            q = q.filter(EvidencePackage.package_type == package_type)
        rows = q.order_by(EvidencePackage.created_at.asc()).all()
        return [{
            "id": p.id, "case_id": p.case_id, "package_type": p.package_type,
            "content_hash": p.content_hash, "storage_ref": p.storage_ref,
            "manifest": p.manifest, "created_at": p.created_at,
        } for p in rows]

    def build_legal_evidence(self, *, case_id: str, manifest_extra: Optional[dict] = None
                             ) -> dict:
        """Generate the §7 LEGAL evidence package: all legal holds + retention
        assignment + every governance audit row, hashed together."""
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "legal_hold.apply", _resource(case))
        manifest = self._legal_manifest(case)
        if manifest_extra:
            manifest = {**manifest, "extra": manifest_extra}
        pkg = self.svc.evidence.build(case, package_type=EvidencePackageType.LEGAL.value,
                                      manifest=manifest)
        return {"id": pkg.id, "content_hash": pkg.content_hash, "manifest": pkg.manifest}

    def build_audit_evidence(self, *, case_id: str) -> dict:
        """Generate the §7 AUDIT evidence package: the chain-head + every audit
        row id + the verifier expectation. Hashes the canonical JSON so an
        external auditor can re-derive the hash from the exported chain."""
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "audit.view", _resource(case))
        from .models import AuditEvent
        rows = (self.ctx.db.query(AuditEvent)
                .filter(AuditEvent.tenant_id == case.tenant_id,
                        AuditEvent.case_id == case.id)
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                .all())
        manifest = {
            "case_id": case.id,
            "audit_row_count": len(rows),
            "chain_head_hash": rows[-1].current_hash if rows else None,
            "row_hashes": [r.current_hash for r in rows],
        }
        pkg = self.svc.evidence.build(case, package_type=EvidencePackageType.AUDIT.value,
                                      manifest=manifest)
        return {"id": pkg.id, "content_hash": pkg.content_hash, "manifest": pkg.manifest}

    def _legal_manifest(self, case: OffboardingCase) -> dict:
        holds = (self.ctx.db.query(LegalHold)
                 .filter(LegalHold.tenant_id == case.tenant_id,
                         LegalHold.case_id == case.id).all())
        retention = self._repo().retention_for_case(case.id)
        return {
            "case_id": case.id,
            "legal_hold_status": case.legal_hold_status,
            "holds": [
                {"id": h.id, "matter_ref": h.matter_ref, "status": h.status,
                 "applied_at": h.applied_at.isoformat() if h.applied_at else None,
                 "released_at": h.released_at.isoformat() if h.released_at else None}
                for h in holds
            ],
            "retention": {
                "policy_code": retention.policy_code if retention else None,
                "retention_until": retention.retention_until.isoformat()
                if retention and retention.retention_until else None,
                "deletion_eligibility": retention.deletion_eligibility if retention else None,
            },
        }

    # ── §4 DELETION ELIGIBILITY REVIEW (dual approval) ────────────────────────
    def open_deletion_review(self, *, case_id: str, scope: str = "full_record",
                             notes: Optional[str] = None) -> dict:
        case = self._get_case(case_id)
        rbac.require(self.ctx.authz, "retention.assign", _resource(case))
        eligibility = retention_engine.compute_eligibility(self.ctx.db, case)
        existing = (self.ctx.db.query(DeletionEligibilityReview)
                    .filter(DeletionEligibilityReview.tenant_id == case.tenant_id,
                            DeletionEligibilityReview.case_id == case.id,
                            DeletionEligibilityReview.status ==
                            DeletionReviewStatus.PENDING.value)
                    .first())
        if existing is not None:
            existing.blockers_snapshot = eligibility["blockers"]
            existing.request_notes = notes or existing.request_notes
            self.ctx.db.flush()
            return self._review_dict(existing, eligibility)
        review = DeletionEligibilityReview(
            id=ids.deletion_review_id(), tenant_id=case.tenant_id, case_id=case.id,
            worker_id=case.worker_id, scope=scope,
            requested_by_id=self.ctx.actor_id, request_notes=notes,
            blockers_snapshot=eligibility["blockers"],
            correlation_id=self.ctx.correlation_id,
        )
        self.ctx.db.add(review)
        self.ctx.db.flush()
        self.svc.audit.record(action="DeletionReviewOpened", entity="DeletionEligibilityReview",
                              case=case, payload={"review_id": review.id, "scope": scope,
                                                  "blockers": eligibility["blockers"]})
        return self._review_dict(review, eligibility)

    def approve_deletion(self, *, review_id: str, role: str,
                         note: Optional[str] = None) -> dict:
        """Record an approval from one of the required roles ('legal' or
        'compliance'). Both must be present, AND the eligibility check must
        still pass at decision time, before status flips to APPROVED."""
        if role not in DELETION_REQUIRED_APPROVERS:
            raise ServiceError(f"unknown approver role {role!r}", "invalid_role")
        review = self._get_review(review_id)
        case = self._get_case(review.case_id)
        rbac.require(self.ctx.authz,
                     "legal_hold.release" if role == "legal" else "retention.assign",
                     _resource(case))
        if review.status != DeletionReviewStatus.PENDING.value:
            raise ServiceError(f"review is {review.status}, cannot approve",
                               "wrong_status")

        now = _now()
        if role == "legal":
            review.legal_approver_id = self.ctx.actor_id
            review.legal_approved_at = now
            review.legal_notes = note
        else:
            review.compliance_approver_id = self.ctx.actor_id
            review.compliance_approved_at = now
            review.compliance_notes = note
        self.ctx.db.flush()

        eligibility = retention_engine.compute_eligibility(self.ctx.db, case)
        missing = self._missing_approvers(review)
        if missing:
            self.svc.audit.record(action="DeletionReviewSigned",
                                  entity="DeletionEligibilityReview", case=case,
                                  payload={"review_id": review.id, "role": role,
                                           "still_missing": missing})
            return self._review_dict(review, eligibility)

        # Both approvals present — gate on live eligibility.
        if not eligibility["eligible"]:
            # Re-snapshot the blockers so the audit trail records why the
            # dual-approved decision was *still* blocked.
            review.blockers_snapshot = eligibility["blockers"]
            self.ctx.db.flush()
            self.svc.audit.record(action="DeletionReviewBlockedDespiteApproval",
                                  entity="DeletionEligibilityReview", case=case,
                                  payload={"review_id": review.id,
                                           "blockers": eligibility["blockers"]})
            raise DeletionBlocked(eligibility["blockers"])

        review.status = DeletionReviewStatus.APPROVED.value
        review.decided_at = now
        self.ctx.db.flush()
        self.svc.audit.record(action="DeletionReviewApproved",
                              entity="DeletionEligibilityReview", case=case,
                              payload={"review_id": review.id})
        return self._review_dict(review, eligibility)

    def deny_deletion(self, *, review_id: str, reason: str) -> dict:
        review = self._get_review(review_id)
        case = self._get_case(review.case_id)
        rbac.require(self.ctx.authz, "legal_hold.release", _resource(case))
        if review.status != DeletionReviewStatus.PENDING.value:
            raise ServiceError(f"review is {review.status}, cannot deny",
                               "wrong_status")
        review.status = DeletionReviewStatus.DENIED.value
        review.denied_by_id = self.ctx.actor_id
        review.denied_at = _now()
        review.denial_reason = reason
        review.decided_at = _now()
        self.ctx.db.flush()
        self.svc.audit.record(action="DeletionReviewDenied",
                              entity="DeletionEligibilityReview", case=case,
                              payload={"review_id": review.id, "reason": reason})
        return self._review_dict(review, retention_engine.compute_eligibility(
            self.ctx.db, case))

    def _missing_approvers(self, review: DeletionEligibilityReview) -> List[str]:
        missing: List[str] = []
        if review.legal_approved_at is None:
            missing.append("legal")
        if review.compliance_approved_at is None:
            missing.append("compliance")
        return missing

    def _get_review(self, review_id: str) -> DeletionEligibilityReview:
        review = (self.ctx.db.query(DeletionEligibilityReview)
                  .filter(DeletionEligibilityReview.id == review_id,
                          DeletionEligibilityReview.tenant_id == self.ctx.tenant_id)
                  .first())
        if review is None:
            raise CrossTenantOrMissing()
        return review

    def _review_dict(self, review: DeletionEligibilityReview,
                     eligibility: Optional[dict] = None) -> dict:
        return {
            "id": review.id, "case_id": review.case_id, "status": review.status,
            "scope": review.scope, "missing_approvers": self._missing_approvers(review),
            "legal_approved_at": review.legal_approved_at,
            "compliance_approved_at": review.compliance_approved_at,
            "denied_at": review.denied_at, "denial_reason": review.denial_reason,
            "blockers": review.blockers_snapshot or [],
            "current_eligibility": eligibility,
        }
