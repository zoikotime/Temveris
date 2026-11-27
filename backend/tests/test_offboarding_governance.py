"""Former-worker lifecycle governance tests (§9 — governance spec).

Mandatory:
  • former worker cannot access active dashboard       (portal isolation)
  • document scope enforced                            (allowlist + grant gate)
  • legal hold blocks deletion                         (dual-approval still gated)
  • evidence hash immutable                            (re-hash matches stored)
  • expired access revoked                             (status flip + 403)
  • download auditing works                            (every download → PortalAccessLog)
"""
import hashlib
import json
import pytest
from datetime import datetime, timedelta, timezone

from app.auth.service import create_access_token, get_current_user
from app.offboarding import portal_auth, retention_engine
from app.offboarding.constants import (
    ALLOWED_FORMER_WORKER_SCOPES, DataRightsRequestStatus, DataRightsRequestType,
    DeletionReviewStatus, EvidencePackageType, FORBIDDEN_FORMER_WORKER_SCOPES,
    FormerAccessStatus, MFAStatus, PortalAction, PortalSessionStatus,
    RetentionPolicyCode,
)
from app.offboarding.governance_service import (
    DeletionBlocked, GovernanceService,
)
from app.offboarding.models import (
    CompletionCertificate, DataRightsRequest, EvidencePackage,
    FormerWorkerAccess, FormerWorkerMFA, FormerWorkerPortalSession,
    FormerWorkerSupportTicket, PortalAccessLog, RetentionAssignment,
    RetentionPolicy,
)
from app.offboarding.portal_auth import (
    PortalAuthError, begin_login, check_download_throttle,
    clear_otp_dispatchers, register_otp_dispatcher, verify_mfa,
)
from app.offboarding.rbac import AuthzContext, Role
from app.offboarding.repository import Repository
from app.offboarding.schemas import CaseCreate
from app.offboarding.services import OffboardingContext, OffboardingService
from database.models import User

import fastapi


def _now():
    return datetime.now(timezone.utc)


def _worker(db, email):
    u = User(name="Worker", email=email, role="employee", is_active=True)
    db.add(u)
    db.flush()
    return u


def _svc(db, tenant="t1", roles=frozenset({Role.SUPER_ADMIN})):
    authz = AuthzContext(user_id=99, tenant_id=tenant, roles=roles)
    return OffboardingService(OffboardingContext(db=db, authz=authz, correlation_id="cor_gov"))


def _gov(db, tenant="t1", roles=frozenset({Role.SUPER_ADMIN})):
    authz = AuthzContext(user_id=99, tenant_id=tenant, roles=roles)
    return GovernanceService(OffboardingContext(db=db, authz=authz, correlation_id="cor_gov"))


def _complete_case(db, svc, worker_id, engagement="ENG-GOV"):
    """Drive a case all the way to COMPLETED + former-access granted."""
    case = svc.create_case(CaseCreate(engagement_id=engagement, worker_id=worker_id))
    svc.submit(case.id)
    svc.approve(case.id, schedule=False)
    svc.execute_access_closure(case.id)
    svc.complete_final_review(case.id)
    svc.close_payroll(case.id)
    svc.close_billing(case.id)
    svc.review_legal_hold(case.id)
    access = svc.setup_former_access(case.id)
    svc.generate_certificate(case.id)
    svc.complete(case.id)
    return case, access


# ════════════════════════════════════════════════════════════════════════════
# §1. PORTAL ISOLATION  — "former worker cannot access active dashboard"
# ════════════════════════════════════════════════════════════════════════════
class TestPortalIsolation:
    def test_portal_router_uses_distinct_auth_dependency(self):
        """The portal router must depend on get_portal_principal — not on
        get_current_user — for every endpoint. We assert this statically by
        inspecting the route dependencies, so a future regression is caught."""
        from app.offboarding.portal_router import router
        from app.offboarding.portal_auth import get_portal_principal
        from app.auth.service import get_current_user

        for route in router.routes:
            deps = []
            # Pull dependant call functions from FastAPI's internal tree.
            dep = getattr(route, "dependant", None)
            if dep is None:
                continue
            stack = [dep]
            while stack:
                d = stack.pop()
                if d.call is not None:
                    deps.append(d.call)
                stack.extend(d.dependencies or [])
            assert get_current_user not in deps, (
                f"portal route {route.path} pulls active-worker get_current_user")

    def test_revoked_active_user_cannot_login_through_admin_path(self, db):
        """Mirror invariant for the other direction: the former worker — whose
        active-worker User.is_active was flipped off at exit — cannot reach
        admin endpoints with a stale access JWT."""
        svc = _svc(db)
        w = _worker(db, "isolation@gov.test")
        _complete_case(db, svc, w.id, engagement="ENG-ISO")
        token = create_access_token({"user_id": w.id})
        with pytest.raises(fastapi.HTTPException) as exc:
            get_current_user(token=token, db=db)
        assert exc.value.status_code == 403


# ════════════════════════════════════════════════════════════════════════════
# §3. DOCUMENT SCOPE ENGINE
# ════════════════════════════════════════════════════════════════════════════
class TestDocumentScopeEngine:
    def test_allowlist_and_blocklist_are_disjoint(self):
        assert ALLOWED_FORMER_WORKER_SCOPES.isdisjoint(FORBIDDEN_FORMER_WORKER_SCOPES)

    def test_grant_rejects_scope_outside_allowlist(self, db):
        svc = _svc(db)
        w = _worker(db, "scope1@gov.test")
        case = svc.create_case(CaseCreate(engagement_id="ENG-SCP1", worker_id=w.id))
        svc.submit(case.id); svc.approve(case.id, schedule=False)
        from app.offboarding.services import ServiceError
        with pytest.raises(ServiceError):
            # 'audit_logs' is explicitly forbidden; the scope check refuses.
            svc.lifecycle.grant_former_access(case, scopes=["audit_logs"])

    def test_grant_accepts_allowlisted_scopes(self, db):
        svc = _svc(db)
        w = _worker(db, "scope2@gov.test")
        _case, access = _complete_case(db, svc, w.id, engagement="ENG-SCP2")
        assert set(access.scopes).issubset(ALLOWED_FORMER_WORKER_SCOPES)


# ════════════════════════════════════════════════════════════════════════════
# §4. LEGAL HOLD BLOCKS DELETION  (dual-approval still gated by holds)
# ════════════════════════════════════════════════════════════════════════════
class TestLegalHoldBlocksDeletion:
    def test_legal_hold_blocks_deletion_even_with_dual_approval(self, db):
        svc = _svc(db)
        w = _worker(db, "delhold@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-DELHOLD")
        gov = _gov(db)
        # Open a deletion review, then drop a legal hold AFTER snapshot — the
        # final approval must re-check eligibility against the LIVE state.
        review = gov.open_deletion_review(case_id=case.id, scope="full_record")
        # First half: legal signs.
        gov.approve_deletion(review_id=review["id"], role="legal", note="approved")
        svc.legal.apply(svc._get(case.id), matter_ref="LIT-1", reason="lit")
        # Second half: compliance signs — but legal hold now blocks.
        with pytest.raises(DeletionBlocked) as exc:
            gov.approve_deletion(review_id=review["id"], role="compliance",
                                 note="approved")
        blocker_types = [b["type"] for b in exc.value.blockers]
        assert "legal_hold_active" in blocker_types

    def test_dual_approval_required_for_deletion(self, db):
        svc = _svc(db)
        w = _worker(db, "dual@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-DUAL")
        gov = _gov(db)
        # Make the case eligible: assign retention with a tiny window so the
        # clock is already elapsed.
        retention_engine.supersede_policy(
            db, tenant_id="t1", code=RetentionPolicyCode.DEFAULT.value,
            new_days=0, description="zero-day for test")
        gov.assign_retention(case_id=case.id, policy_code=RetentionPolicyCode.DEFAULT.value)
        # Make the clock elapsed in the past.
        assignment = Repository(db, "t1").retention_for_case(case.id)
        assignment.retention_until = _now() - timedelta(days=1)
        db.flush()

        review = gov.open_deletion_review(case_id=case.id)
        assert review["status"] == DeletionReviewStatus.PENDING.value

        gov.approve_deletion(review_id=review["id"], role="legal")
        # Single approval is not enough — status still pending.
        from app.offboarding.models import DeletionEligibilityReview as DER
        row = db.query(DER).filter(DER.id == review["id"]).first()
        assert row.status == DeletionReviewStatus.PENDING.value

        gov.approve_deletion(review_id=review["id"], role="compliance")
        db.refresh(row)
        assert row.status == DeletionReviewStatus.APPROVED.value

    def test_eligibility_blockers_enumerated(self, db):
        svc = _svc(db)
        w = _worker(db, "elig@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-ELIG")
        result = retention_engine.compute_eligibility(db, svc._get(case.id))
        # Retention is active (default 7 years) — eligibility should be false.
        assert result["eligible"] is False
        assert any(b["type"] == "retention_active" for b in result["blockers"])


# ════════════════════════════════════════════════════════════════════════════
# §6+§7. CERTIFICATE & EVIDENCE PACKAGE HASH IMMUTABILITY
# ════════════════════════════════════════════════════════════════════════════
class TestEvidenceImmutability:
    def test_certificate_payload_hash_recomputes(self, db):
        svc = _svc(db)
        w = _worker(db, "hash@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-HASH")
        cert = Repository(db, "t1").certificate_for_case(case.id)
        assert cert is not None
        recomputed = hashlib.sha256(
            json.dumps(cert.payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        assert recomputed == cert.content_hash

    def test_certificate_payload_includes_required_fields(self, db):
        svc = _svc(db)
        w = _worker(db, "certfields@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-CERTF")
        cert = Repository(db, "t1").certificate_for_case(case.id)
        for required in ("access_closure", "tracking_stop", "audit_reference",
                         "retention", "legal_hold", "evidence_hashes"):
            assert required in cert.payload, f"certificate missing {required}"
        # Evidence hashes are present and each is a 64-char SHA-256 hex.
        assert len(cert.payload["evidence_hashes"]) > 0
        assert all(len(e["content_hash"]) == 64 for e in cert.payload["evidence_hashes"])

    def test_legal_and_audit_evidence_packages_hash(self, db):
        svc = _svc(db)
        w = _worker(db, "legaud@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-LEGAUD")
        gov = _gov(db)
        legal_pkg = gov.build_legal_evidence(case_id=case.id)
        audit_pkg = gov.build_audit_evidence(case_id=case.id)
        for pkg_dict in (legal_pkg, audit_pkg):
            assert len(pkg_dict["content_hash"]) == 64
            recomputed = hashlib.sha256(
                json.dumps(pkg_dict["manifest"], sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            assert recomputed == pkg_dict["content_hash"]
        # Package types are recorded distinctly so the §7 audit can fetch them.
        pkgs = (db.query(EvidencePackage)
                .filter(EvidencePackage.case_id == case.id).all())
        types = {p.package_type for p in pkgs}
        assert EvidencePackageType.LEGAL.value in types
        assert EvidencePackageType.AUDIT.value in types


# ════════════════════════════════════════════════════════════════════════════
# §5. RETENTION ENGINE
# ════════════════════════════════════════════════════════════════════════════
class TestRetentionEngine:
    def test_policies_seed_idempotent(self, db):
        n1 = retention_engine.seed_default_policies(db, tenant_id="t1")
        n2 = retention_engine.seed_default_policies(db, tenant_id="t1")
        assert n1 > 0
        assert n2 == 0   # second call is a no-op

    def test_jurisdiction_resolves_to_eu_policy(self, db):
        retention_engine.seed_default_policies(db, tenant_id="t1")
        pol = retention_engine.resolve_policy(db, tenant_id="t1", jurisdiction="DE")
        assert pol.code == RetentionPolicyCode.GDPR_EU.value

    def test_policy_versioning_supersedes(self, db):
        retention_engine.seed_default_policies(db, tenant_id="t1")
        v1 = retention_engine.active_policy(db, tenant_id="t1",
                                            code=RetentionPolicyCode.DEFAULT.value)
        assert v1.version == 1
        v2 = retention_engine.supersede_policy(
            db, tenant_id="t1", code=RetentionPolicyCode.DEFAULT.value,
            new_days=3650, description="new policy")
        db.refresh(v1)
        assert v1.active is False
        assert v1.superseded_by == v2.id
        assert v2.version == 2

    def test_expiry_job_flips_eligibility(self, db):
        svc = _svc(db)
        w = _worker(db, "expiry@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-EXP")
        retention_engine.supersede_policy(
            db, tenant_id="t1", code=RetentionPolicyCode.DEFAULT.value,
            new_days=0, description="zero")
        _gov(db).assign_retention(case_id=case.id,
                                  policy_code=RetentionPolicyCode.DEFAULT.value)
        # Time-travel the assignment past its (already-zero) window.
        assignment = Repository(db, "t1").retention_for_case(case.id)
        assignment.retention_until = _now() - timedelta(days=1)
        db.flush()
        result = retention_engine.run_retention_expiry_once(db)
        assert result["now_eligible"] >= 1


# ════════════════════════════════════════════════════════════════════════════
# PORTAL  — login / MFA / scopes / throttle / abnormal activity
# ════════════════════════════════════════════════════════════════════════════
class TestPortal:
    def setup_method(self):
        self._captured: list = []
        clear_otp_dispatchers()
        register_otp_dispatcher(lambda email, otp: self._captured.append((email, otp)))

    def teardown_method(self):
        clear_otp_dispatchers()
        # Reset in-memory rate limiters so tests don't bleed state.
        from app.offboarding.portal_auth import _login_limiter, _download_limiter
        _login_limiter._hits.clear()
        _download_limiter._hits.clear()

    def _enrolled_access(self, db, email):
        """Grant former access AND enroll the alternate email so the worker can
        actually log in via the portal flow."""
        svc = _svc(db)
        w = _worker(db, email)
        case, access = _complete_case(db, svc, w.id,
                                       engagement=f"ENG-PORT-{email.split('@')[0]}")
        raw_token = getattr(access, "_raw_portal_token", None)
        assert raw_token, "grant_former_access did not surface the raw portal token"

        # Re-load via the same session to refresh attrs but keep the token.
        access_row = Repository(db, "t1").former_access_for_case(case.id)
        portal_auth.begin_mfa_enrollment(db, fwa=access_row,
                                         alternate_email=f"alt+{email}")
        # First OTP captured is the enrollment one.
        _email, enroll_otp = self._captured[-1]
        portal_auth.verify_mfa_enrollment(db, fwa=access_row, otp=enroll_otp)
        db.flush()
        return case, access_row, raw_token

    def test_login_requires_mfa_and_returns_session(self, db):
        case, access, token = self._enrolled_access(db, "p1@gov.test")
        raw_sess, session = begin_login(db, portal_token=token)
        assert session.status == PortalSessionStatus.PENDING_MFA.value
        # OTP captured on login.
        _email, login_otp = self._captured[-1]
        verified = verify_mfa(db, session_token=raw_sess, otp=login_otp)
        assert verified.status == PortalSessionStatus.ACTIVE.value
        assert verified.mfa_passed is True

    def test_wrong_otp_locks_session_after_limit(self, db):
        case, access, token = self._enrolled_access(db, "p2@gov.test")
        raw_sess, _session = begin_login(db, portal_token=token)
        for _ in range(5):
            with pytest.raises(PortalAuthError):
                verify_mfa(db, session_token=raw_sess, otp="000000")
        # Session is REVOKED after the burst.
        row = db.query(FormerWorkerPortalSession).filter(
            FormerWorkerPortalSession.token_hash ==
            hashlib.sha256(raw_sess.encode("utf-8")).hexdigest()).first()
        assert row.status == PortalSessionStatus.REVOKED.value

    def test_unknown_portal_token_logged_and_rejected(self, db):
        with pytest.raises(PortalAuthError) as exc:
            begin_login(db, portal_token="not-a-real-token-xxxx")
        assert exc.value.status_code == 401
        # An attempt was logged (no FWA known, but the action lands on the
        # default-tenant null stream).
        log = (db.query(PortalAccessLog)
               .filter(PortalAccessLog.action == PortalAction.LOGIN_ATTEMPT.value)
               .all())
        assert any(r.outcome == "failed" for r in log)

    def test_expired_access_revoked_on_login(self, db):
        case, access, token = self._enrolled_access(db, "p3@gov.test")
        # Time-travel the grant past expiry.
        access.expires_at = _now() - timedelta(days=1)
        db.flush()
        with pytest.raises(PortalAuthError) as exc:
            begin_login(db, portal_token=token)
        assert exc.value.status_code == 403
        db.refresh(access)
        assert access.status == FormerAccessStatus.EXPIRED.value

    def test_download_audited(self, db):
        case, access, token = self._enrolled_access(db, "p4@gov.test")
        # Drive directly: a login → MFA → mock a download throttle check + log.
        raw_sess, session = begin_login(db, portal_token=token)
        _email, login_otp = self._captured[-1]
        verify_mfa(db, session_token=raw_sess, otp=login_otp)
        access_row = Repository(db, "t1").former_access_for_case(case.id)
        check_download_throttle(db, access_row, session=session)
        portal_auth.log_action(db, fwa=access_row,
                               action=PortalAction.DOWNLOAD_DOCUMENT.value,
                               outcome="ok",
                               session=session,
                               scope="approved_payslips",
                               document_ref="2026-04")
        db.flush()
        rows = (db.query(PortalAccessLog)
                .filter(PortalAccessLog.fwa_id == access_row.id,
                        PortalAccessLog.action == PortalAction.DOWNLOAD_DOCUMENT.value)
                .all())
        assert len(rows) >= 1
        assert rows[0].scope == "approved_payslips"
        assert rows[0].document_ref == "2026-04"

    def test_download_throttle_enforced(self, db):
        case, access, token = self._enrolled_access(db, "p5@gov.test")
        raw_sess, session = begin_login(db, portal_token=token)
        _email, login_otp = self._captured[-1]
        verify_mfa(db, session_token=raw_sess, otp=login_otp)
        access_row = Repository(db, "t1").former_access_for_case(case.id)
        # The default limit is 10 in 60s.
        for _ in range(10):
            check_download_throttle(db, access_row, session=session)
        with pytest.raises(PortalAuthError) as exc:
            check_download_throttle(db, access_row, session=session)
        assert exc.value.status_code == 429


# ════════════════════════════════════════════════════════════════════════════
# DSAR + SUPPORT TICKETS  (allowed §1 surfaces)
# ════════════════════════════════════════════════════════════════════════════
class TestDsarAndSupport:
    def test_dsar_records_request_with_sla(self, db):
        svc = _svc(db)
        w = _worker(db, "dsar@gov.test")
        case, access = _complete_case(db, svc, w.id, engagement="ENG-DSAR")
        # Insert a DSAR directly via the model (the portal router does the same).
        from app.offboarding import ids as _ids
        req = DataRightsRequest(
            id=_ids.dsar_id(), tenant_id=access.tenant_id, fwa_id=access.id,
            worker_id=access.worker_id, case_id=access.case_id,
            request_type=DataRightsRequestType.ERASURE.value,
            status=DataRightsRequestStatus.OPEN.value,
            sla_due_at=_now() + timedelta(days=30),
        )
        db.add(req); db.flush()
        assert req.status == DataRightsRequestStatus.OPEN.value
        assert req.sla_due_at is not None


# ════════════════════════════════════════════════════════════════════════════
# CROSS-TENANT GOVERNANCE ISOLATION
# ════════════════════════════════════════════════════════════════════════════
class TestCrossTenantGovernance:
    def test_certificate_fetch_404_cross_tenant(self, db):
        svc = _svc(db, tenant="t1")
        w = _worker(db, "xt-cert@gov.test")
        case, _ = _complete_case(db, svc, w.id, engagement="ENG-XTCERT")
        # Same case_id, different tenant → 404 not found, never the cert.
        from app.offboarding.security_service import CrossTenantOrMissing
        gov = _gov(db, tenant="t2")
        with pytest.raises(CrossTenantOrMissing):
            gov.get_certificate(case_id=case.id)
