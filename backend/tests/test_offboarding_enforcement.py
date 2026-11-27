"""Security-critical enforcement tests (§8).

These are the mandatory controls and the P0 blockers for the offboarding
enforcement layer. They assert the Tier-0 invariants directly: once a worker's
access is closed, they cannot log in, hold a valid API token, or push telemetry
— enforced from server-side lifecycle state, never from client logout.

P0 blockers (each is the *negation* of a release-blocking bug):
  • a revoked user can still log in            → MUST fail
  • tracking continues after exit              → MUST be rejected
  • an API token stays valid after revocation  → MUST be denied
  • cross-tenant leakage                        → MUST 404 / be invisible
"""
import pytest

from app.auth.service import create_access_token, get_current_user
from app.offboarding import enforcement, incidents, reconciliation
from app.offboarding.constants import (
    CaseState, IdentityConflictType, IncidentSeverity, QuarantineReason,
    ReconciliationStatus, TelemetryKind,
)
from app.offboarding.models import (
    OffboardingCase, SecurityIncident, TelemetryQuarantineRecord,
    WorkflowBlocker,
)
from app.offboarding.rbac import AuthzContext, Role
from app.offboarding.repository import Repository
from app.offboarding.schemas import CaseCreate
from app.offboarding.security_service import CrossTenantOrMissing, SecurityService
from app.offboarding.services import OffboardingContext, OffboardingService
from database.models import ClockSession, RefreshToken, User, UserSession

from datetime import datetime, timedelta, timezone

import fastapi


def _now():
    return datetime.now(timezone.utc)


def _worker(db, email, *, active=True):
    u = User(name="Worker", email=email, role="employee", is_active=active)
    db.add(u)
    db.flush()
    return u


def _seed_devices(db, worker_id):
    """Give the worker something to revoke: a live session + refresh token +
    an open clock session (active tracking)."""
    db.add(UserSession(user_id=worker_id, token_hash=f"sess{worker_id}", is_revoked=False))
    db.add(RefreshToken(user_id=worker_id, token_hash=f"rt{worker_id}", family_id=f"fam{worker_id}",
                        remember=True, is_revoked=False, expires_at=_now() + timedelta(days=30)))
    db.add(ClockSession(user_id=worker_id, clock_in_time=_now(), is_active=True))
    db.flush()


def _svc(db, tenant="t1", roles=frozenset({Role.SUPER_ADMIN}), user_id=99):
    authz = AuthzContext(user_id=user_id, tenant_id=tenant, roles=roles)
    return OffboardingService(OffboardingContext(db=db, authz=authz, correlation_id="cor_enf"))


def _close_access(db, svc, worker_id, engagement="ENG-SEC"):
    """Drive a case to FINAL_REVIEW_PENDING — an access-closed state."""
    case = svc.create_case(CaseCreate(engagement_id=engagement, worker_id=worker_id))
    svc.submit(case.id)
    svc.approve(case.id, schedule=False)        # → IN_PROGRESS
    svc.execute_access_closure(case.id)         # → FINAL_REVIEW_PENDING (access closed)
    return case


# ════════════════════════════════════════════════════════════════════════════
# 1. ACCESS REVOCATION  — login / tokens / sessions
# ════════════════════════════════════════════════════════════════════════════
class TestAccessRevocation:
    def test_revoked_user_cannot_login(self, db):
        svc = _svc(db)
        w = _worker(db, "login@sec.test")
        _seed_devices(db, w.id)
        _close_access(db, svc, w.id)
        # Login gate is closed both ways: the lifecycle flipped is_active off,
        # and the enforcement gate denies on lifecycle state.
        assert db.query(User).get(w.id).is_active is False
        decision = enforcement.is_login_allowed(db, w.id)
        assert decision.allowed is False
        assert decision.state == CaseState.FINAL_REVIEW_PENDING.value

    def test_tokens_invalidated(self, db):
        svc = _svc(db)
        w = _worker(db, "tokens@sec.test")
        _seed_devices(db, w.id)
        _close_access(db, svc, w.id)
        # Every refresh token revoked, every session revoked.
        assert all(rt.is_revoked for rt in
                   db.query(RefreshToken).filter(RefreshToken.user_id == w.id).all())
        assert all(s.is_revoked for s in
                   db.query(UserSession).filter(UserSession.user_id == w.id).all())

    def test_api_access_denied_for_revoked_user(self, db):
        svc = _svc(db)
        w = _worker(db, "api@sec.test")
        _seed_devices(db, w.id)
        _close_access(db, svc, w.id)
        # A still-unexpired access token must be refused by get_current_user.
        token = create_access_token({"user_id": w.id})
        with pytest.raises(fastapi.HTTPException) as exc:
            get_current_user(token=token, db=db)
        assert exc.value.status_code == 403

    def test_revocation_is_idempotent(self, db):
        svc = _svc(db)
        w = _worker(db, "idem@sec.test")
        _seed_devices(db, w.id)
        case = _close_access(db, svc, w.id)
        # Re-running the sweep changes nothing materially and never errors.
        before = {r.component: r.status for r in Repository(db, "t1").revocations_for_case(case.id)}
        svc.identity.revoke_all(case)
        after = {r.component: r.status for r in Repository(db, "t1").revocations_for_case(case.id)}
        assert before == after

    def test_mobile_and_desktop_surfaces_recorded(self, db):
        svc = _svc(db)
        w = _worker(db, "devices@sec.test")
        _seed_devices(db, w.id)
        case = _close_access(db, svc, w.id)
        comps = {r.component for r in Repository(db, "t1").revocations_for_case(case.id)}
        assert "mobile_authorization" in comps
        assert "desktop_authorization" in comps


# ════════════════════════════════════════════════════════════════════════════
# 2. TRACKING STOP ENFORCEMENT  — telemetry rejection + quarantine
# ════════════════════════════════════════════════════════════════════════════
class TestTrackingStop:
    def test_telemetry_blocked_after_access_closure(self, db):
        svc = _svc(db)
        w = _worker(db, "track@sec.test")
        _close_access(db, svc, w.id)
        decision = enforcement.is_telemetry_allowed(db, w.id)
        assert decision.allowed is False
        assert decision.reason == QuarantineReason.POST_EXIT_ACCESS_CLOSED.value

    def test_telemetry_blocked_past_final_access_even_before_closure_runs(self, db):
        svc = _svc(db)
        w = _worker(db, "cutover@sec.test")
        case = svc.create_case(CaseCreate(engagement_id="ENG-CUT", worker_id=w.id,
                                          final_access_at=_now() - timedelta(minutes=5)))
        svc.submit(case.id)
        svc.approve(case.id, schedule=True)     # SCHEDULED, cutover already elapsed
        decision = enforcement.is_telemetry_allowed(db, w.id)
        assert decision.allowed is False
        assert decision.reason == QuarantineReason.PAST_FINAL_ACCESS.value

    def test_active_worker_telemetry_allowed(self, db):
        w = _worker(db, "active@sec.test")
        decision = enforcement.is_telemetry_allowed(db, w.id)
        assert decision.allowed is True

    def test_post_exit_upload_is_quarantined(self, db):
        svc = _svc(db)
        w = _worker(db, "quarantine@sec.test")
        _close_access(db, svc, w.id)
        decision = enforcement.evaluate_worker(db, w.id)
        rec = enforcement.record_quarantine(
            db, worker_id=w.id, telemetry_kind=TelemetryKind.SCREENSHOT.value,
            decision=decision, payload_meta={"app_name": "Chrome", "mouse_clicks": 5},
            source_ip="203.0.113.7")
        db.flush()
        assert rec.status == "quarantined"
        assert rec.incident_id is not None
        # A CRITICAL incident was opened for the post-exit tracking attempt.
        inc = db.query(SecurityIncident).filter(SecurityIncident.id == rec.incident_id).first()
        assert inc.severity == IncidentSeverity.CRITICAL.value

    def test_offline_backfill_reason(self, db):
        svc = _svc(db)
        w = _worker(db, "offline@sec.test")
        _close_access(db, svc, w.id)
        decision = enforcement.evaluate_worker(db, w.id)
        rec = enforcement.record_quarantine(
            db, worker_id=w.id, telemetry_kind=TelemetryKind.OFFLINE_UPLOAD.value,
            decision=decision)
        assert rec.reason == QuarantineReason.OFFLINE_BACKFILL.value

    def test_quarantine_dedupes_incident(self, db):
        svc = _svc(db)
        w = _worker(db, "dedupe@sec.test")
        _close_access(db, svc, w.id)
        for _ in range(3):
            decision = enforcement.evaluate_worker(db, w.id)
            enforcement.record_quarantine(db, worker_id=w.id,
                                          telemetry_kind=TelemetryKind.SCREENSHOT.value,
                                          decision=decision)
        # Three rejected uploads → three quarantine rows but ONE incident.
        assert db.query(TelemetryQuarantineRecord).filter(
            TelemetryQuarantineRecord.worker_id == w.id).count() == 3
        incs = db.query(SecurityIncident).filter(SecurityIncident.worker_id == w.id).all()
        assert len(incs) == 1
        assert incs[0].occurrences == 3


# ════════════════════════════════════════════════════════════════════════════
# 4. SSO / SCIM RECONCILIATION
# ════════════════════════════════════════════════════════════════════════════
class TestReconciliation:
    def teardown_method(self):
        reconciliation.clear_adapters()

    def test_stale_sso_state_blocks_completion(self, db):
        svc = _svc(db)
        w = _worker(db, "sso@sec.test")
        case = _close_access(db, svc, w.id)
        # An SSO teardown is still pending after access closure → stale SSO.
        reconciliation.enqueue(db, case=case, integration="sso", component="sso_sessions")
        found = reconciliation.detect_conflicts_for_case(db, case)
        assert IdentityConflictType.STALE_SSO_STATE.value in found
        # A blocker was raised, so the case can't complete clean.
        open_blockers = Repository(db, "t1").blockers_for_case(case.id, open_only=True)
        assert any(b.blocker_type.startswith("identity_conflict") for b in open_blockers)

    def test_scim_failure_dead_letters_and_opens_incident(self, db):
        svc = _svc(db)
        w = _worker(db, "scim@sec.test")
        case = _close_access(db, svc, w.id)

        def _always_fail(item):
            raise RuntimeError("SCIM endpoint 503")
        reconciliation.register_adapter("scim", _always_fail)

        item = reconciliation.enqueue(db, case=case, integration="scim", component="scim_state")
        # Burn through the retry budget.
        for _ in range(item.max_attempts):
            reconciliation.process_item(db, item)
        assert item.status == ReconciliationStatus.DEAD_LETTER.value
        # Dead-letter raised a reconciliation-conflict incident + a case blocker.
        assert db.query(SecurityIncident).filter(
            SecurityIncident.case_id == case.id).count() >= 1
        assert db.query(WorkflowBlocker).filter(
            WorkflowBlocker.case_id == case.id,
            WorkflowBlocker.blocker_type.like("reconciliation_dead_letter%")).count() == 1

    def test_successful_reconciliation_marks_component_revoked(self, db):
        svc = _svc(db)
        w = _worker(db, "recon-ok@sec.test")
        case = _close_access(db, svc, w.id)
        reconciliation.register_adapter("crm", lambda item: {"revoked": True})
        item = reconciliation.enqueue(db, case=case, integration="crm", component="crm_access")
        status = reconciliation.process_item(db, item)
        assert status == ReconciliationStatus.SUCCEEDED.value

    def test_rehire_conflict_detected(self, db):
        svc = _svc(db)
        w = _worker(db, "rehire@sec.test")
        case = _close_access(db, svc, w.id)
        # Simulate an out-of-band reactivation without a case reversal.
        db.query(User).filter(User.id == w.id).update({"is_active": True})
        db.flush()
        found = reconciliation.detect_conflicts_for_case(db, case)
        assert IdentityConflictType.REHIRE_CONFLICT.value in found

    def test_unconfigured_integration_is_noop_success(self, db):
        svc = _svc(db)
        w = _worker(db, "noop@sec.test")
        case = _close_access(db, svc, w.id)
        item = reconciliation.enqueue(db, case=case, integration="integrations", component="integration_tokens")
        # No adapter registered → nothing to revoke externally → succeeds.
        assert reconciliation.process_item(db, item) == ReconciliationStatus.SUCCEEDED.value


# ════════════════════════════════════════════════════════════════════════════
# 6 + 7. INCIDENTS + SECURITY API
# ════════════════════════════════════════════════════════════════════════════
class TestSecurityApi:
    def _sec(self, db, tenant="t1", roles=frozenset({Role.IT_SECURITY_ADMIN})):
        authz = AuthzContext(user_id=7, tenant_id=tenant, roles=roles)
        return SecurityService(OffboardingContext(db=db, authz=authz, correlation_id="cor_api"))

    def test_revoke_access_by_worker_breakglass(self, db):
        w = _worker(db, "breakglass@sec.test")
        _seed_devices(db, w.id)
        sec = self._sec(db)
        result = sec.revoke_access(worker_id=w.id, reason="security_incident")
        assert result["mode"] == "direct_worker"
        assert db.query(User).get(w.id).is_active is False
        assert all(rt.is_revoked for rt in
                   db.query(RefreshToken).filter(RefreshToken.user_id == w.id).all())

    def test_revoke_access_by_case_runs_full_sweep(self, db):
        svc = _svc(db)
        w = _worker(db, "revcase@sec.test")
        _seed_devices(db, w.id)
        case = svc.create_case(CaseCreate(engagement_id="ENG-REVK", worker_id=w.id))
        svc.submit(case.id)
        svc.approve(case.id, schedule=False)   # IN_PROGRESS
        sec = self._sec(db)
        result = sec.revoke_access(case_id=case.id)
        assert result["mode"] == "case"
        assert db.query(User).get(w.id).is_active is False

    def test_retry_revocation_on_failed_case(self, db):
        svc = _svc(db)
        w = _worker(db, "retry@sec.test")
        _seed_devices(db, w.id)
        case = _close_access(db, svc, w.id)
        sec = self._sec(db)
        out = sec.retry_revocation(case_id=case.id)
        assert out["case_id"] == case.id

    def test_security_events_lists_incidents(self, db):
        svc = _svc(db)
        w = _worker(db, "events@sec.test")
        _close_access(db, svc, w.id)
        decision = enforcement.evaluate_worker(db, w.id)
        enforcement.record_quarantine(db, worker_id=w.id,
                                      telemetry_kind=TelemetryKind.SCREENSHOT.value,
                                      decision=decision)
        db.flush()
        sec = self._sec(db, roles=frozenset({Role.IT_SECURITY_ADMIN}))
        events = sec.security_events()
        assert events["open_count"] >= 1
        assert any(i["severity"] == IncidentSeverity.CRITICAL.value for i in events["incidents"])

    def test_reconciliation_view_requires_audit_view(self, db):
        # SUPPORT_ADMIN has only case.view, not audit.view.
        sec = self._sec(db, roles=frozenset({Role.SUPPORT_ADMIN}))
        from app.offboarding.services import ServiceError
        with pytest.raises(ServiceError):
            sec.reconciliation_view()


# ════════════════════════════════════════════════════════════════════════════
# P0 BLOCKERS  — the bugs that must never ship
# ════════════════════════════════════════════════════════════════════════════
class TestP0Blockers:
    def test_p0_revoked_user_login_does_not_work(self, db):
        svc = _svc(db)
        w = _worker(db, "p0login@sec.test")
        _seed_devices(db, w.id)
        _close_access(db, svc, w.id)
        assert enforcement.is_login_allowed(db, w.id).allowed is False

    def test_p0_tracking_does_not_continue(self, db):
        svc = _svc(db)
        w = _worker(db, "p0track@sec.test")
        _close_access(db, svc, w.id)
        assert enforcement.is_telemetry_allowed(db, w.id).allowed is False

    def test_p0_api_token_not_still_valid(self, db):
        svc = _svc(db)
        w = _worker(db, "p0api@sec.test")
        _seed_devices(db, w.id)
        _close_access(db, svc, w.id)
        token = create_access_token({"user_id": w.id})
        with pytest.raises(fastapi.HTTPException):
            get_current_user(token=token, db=db)

    def test_p0_no_cross_tenant_leakage_in_gate(self, db):
        # A case for the worker exists in tenant t1; evaluating under t2 must not
        # see it (zero cross-tenant access).
        svc = _svc(db, tenant="t1")
        w = _worker(db, "p0xtenant@sec.test")
        _close_access(db, svc, w.id, engagement="ENG-XT")
        # Scoped to the OTHER tenant → no blocking case visible.
        assert enforcement.evaluate_worker(db, w.id, tenant_id="t2").allowed is True
        # Scoped to the owning tenant → blocked.
        assert enforcement.evaluate_worker(db, w.id, tenant_id="t1").allowed is False

    def test_p0_cross_tenant_revoke_returns_not_found(self, db):
        svc = _svc(db, tenant="t1")
        w = _worker(db, "p0xrevoke@sec.test")
        case = _close_access(db, svc, w.id, engagement="ENG-XR")
        # An operator in t2 trying to act on t1's case must get a 404-style miss,
        # never confirmation the id exists.
        authz = AuthzContext(user_id=7, tenant_id="t2", roles=frozenset({Role.IT_SECURITY_ADMIN}))
        sec = SecurityService(OffboardingContext(db=db, authz=authz, correlation_id="cor_xt"))
        with pytest.raises(CrossTenantOrMissing):
            sec.revoke_access(case_id=case.id)


# ════════════════════════════════════════════════════════════════════════════
# 5. BACKGROUND JOBS
# ════════════════════════════════════════════════════════════════════════════
class TestBackgroundJobs:
    def test_scheduled_final_access_processor_closes_access(self, db):
        from app.offboarding import jobs
        svc = _svc(db)
        w = _worker(db, "job-sched@sec.test")
        _seed_devices(db, w.id)
        case = svc.create_case(CaseCreate(engagement_id="ENG-JOB-SCHED", worker_id=w.id,
                                          final_access_at=_now() - timedelta(minutes=1)))
        svc.submit(case.id)
        svc.approve(case.id, schedule=True)     # → SCHEDULED, cutover already passed
        db.commit()
        jobs.process_scheduled_final_access(db)
        refreshed = Repository(db, "t1").get_case(case.id)
        assert refreshed.status == CaseState.FINAL_REVIEW_PENDING.value
        assert db.query(User).get(w.id).is_active is False

    def test_tracking_stop_sweep_closes_drifted_session(self, db):
        from app.offboarding import jobs
        svc = _svc(db)
        w = _worker(db, "job-drift@sec.test")
        case = _close_access(db, svc, w.id, engagement="ENG-JOB-DRIFT")
        # Simulate drift: a brand-new open clock session appears AFTER closure.
        drift = ClockSession(user_id=w.id, clock_in_time=_now(), is_active=True)
        db.add(drift)
        db.commit()
        jobs.enforce_tracking_stop(db)
        db.refresh(drift)
        assert drift.clock_out_time is not None      # forcibly closed
        assert db.query(SecurityIncident).filter(
            SecurityIncident.worker_id == w.id).count() >= 1

    def test_run_once_sync_smoke(self, db):
        from app.offboarding import jobs
        summary = jobs.run_once_sync()
        # Returns a per-processor summary dict without raising.
        assert set(summary).issuperset({
            "scheduled_final_access", "revocation_retries", "scim_reconciliation",
            "tracking_stop", "identity_conflicts"})
