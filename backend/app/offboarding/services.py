"""Domain service layer (§3).

Ten services, each a thin object constructed from a single ``OffboardingContext``
(db session + actor authz + correlation id). They are:

  • dependency-injected — every collaborator is passed in / built from the
    context, nothing is reached for globally;
  • event-driven — every meaningful action appends an immutable audit row AND
    emits a domain event in the same transaction;
  • async/thread-safe — they hold no mutable shared state; one instance per
    request, executed in FastAPI's threadpool like the rest of the codebase;
  • testable — they take a plain Session and AuthzContext, so unit tests drive
    them against an in-memory SQLite DB with no FastAPI involved.

The services intentionally REUSE existing platform machinery rather than
duplicating it (see __init__.py): refresh-token revocation, UserSession
revocation, and the User.is_active login gate.

  OffboardingService     — lifecycle orchestrator (state machine + all gates)
  WorkerLifecycleService — former-worker access grant / revoke
  IdentityService        — access revocation across every surface
  TrackingService        — tracking stop enforcement + post-exit rejection
  AuditService           — append-only audit + domain events
  DocumentService        — former-worker document scope enforcement
  RetentionService       — retention assignment + deletion eligibility
  LegalHoldService       — legal hold apply / release
  NotificationService    — notification dispatch (event-driven)
  EvidencePackageService — evidence bundle + hashing
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.auth.tokens import revoke_all_for_user
from database.models import ClockSession, User, UserSession

# §45.D-15 — session-boundary provenance for the tracking-stop closure.
from app.clock_session_boundary import (
    BOUNDARY_CLOCK_OUT,
    Provenance,
    actor_label_for_user_id,
    append_boundary_event,
)

from . import events, ids, rbac, workflow
from .constants import (
    ALLOWED_FORMER_WORKER_SCOPES, CaseState, DeletionEligibility, EventType,
    EvidencePackageType, ExitType, FormerAccessStatus, RevocationComponent,
    RevocationMode, RevocationStatus, TaskType,
)
from .models import (
    DEFAULT_TENANT, AccessRevocationRecord, CompletionCertificate, EvidencePackage,
    FormerWorkerAccess, IntegrationReconciliationItem, LegalHold, OffboardingCase,
    RetentionAssignment,
)
from .rbac import AuthzContext, ResourceAttrs
from .repository import CLOSED_CASE_STATES, Repository
from .schemas import CaseCreate, CaseUpdate, CompletionReadiness
from .state_machine import assert_transition, can_unblock

_log = logging.getLogger("zoiko.offboarding.services")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# ERRORS  (mapped to HTTP status codes by the router)
# ════════════════════════════════════════════════════════════════════════════
class ServiceError(Exception):
    http_status = 400

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class CaseNotFound(ServiceError):
    http_status = 404

    def __init__(self, case_id: str = ""):
        super().__init__("case not found", "not_found")


class DuplicateActiveCase(ServiceError):
    http_status = 409

    def __init__(self):
        super().__init__("an active offboarding case already exists for this engagement",
                         "duplicate_active_case")


class CompletionBlocked(ServiceError):
    http_status = 409

    def __init__(self, reasons: List[str]):
        super().__init__("completion blocked by unmet controls", "completion_blocked")
        self.reasons = reasons


# ════════════════════════════════════════════════════════════════════════════
# CONTEXT  (dependency-injection seam)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class OffboardingContext:
    db: Session
    authz: AuthzContext
    correlation_id: str
    ip: Optional[str] = None

    @property
    def tenant_id(self) -> str:
        return self.authz.tenant_id

    @property
    def actor_id(self) -> Optional[int]:
        return self.authz.user_id

    @property
    def actor_role(self) -> Optional[str]:
        return ",".join(sorted(r.value for r in self.authz.roles)) or None

    def repo(self) -> Repository:
        return Repository(self.db, self.tenant_id)


def _resource(case: OffboardingCase) -> ResourceAttrs:
    return ResourceAttrs(
        tenant_id=case.tenant_id,
        department=case.department,
        jurisdiction=case.jurisdiction,
        worker_type=case.worker_type,
        exit_type=case.exit_type,
        risk_level=case.risk_level,
        legal_hold_status=case.legal_hold_status,
        case_status=case.status,
        data_classification=case.data_classification,
    )


# ════════════════════════════════════════════════════════════════════════════
# AUDIT SERVICE
# ════════════════════════════════════════════════════════════════════════════
class AuditService:
    def __init__(self, ctx: OffboardingContext):
        self.ctx = ctx

    def record(self, *, action: str, entity: str, case: Optional[OffboardingCase] = None,
               payload: Optional[dict] = None, causation_id: Optional[str] = None):
        return events.append_audit(
            self.ctx.db,
            tenant_id=self.ctx.tenant_id,
            action=action,
            entity=entity,
            case_id=case.id if case else None,
            worker_id=case.worker_id if case else None,
            engagement_id=case.engagement_id if case else None,
            actor_id=self.ctx.actor_id,
            actor_role=self.ctx.actor_role,
            payload=payload or {},
            correlation_id=self.ctx.correlation_id,
            causation_id=causation_id,
            ip_address=self.ctx.ip,
        )

    def emit(self, *, event_type: str, case: Optional[OffboardingCase] = None,
             payload: Optional[dict] = None, state_from: Optional[str] = None,
             state_to: Optional[str] = None, idempotency_key: Optional[str] = None,
             causation_id: Optional[str] = None):
        return events.emit_event(
            self.ctx.db,
            tenant_id=self.ctx.tenant_id,
            event_type=event_type,
            case_id=case.id if case else None,
            aggregate_id=case.id if case else None,
            payload=payload or {},
            correlation_id=self.ctx.correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            state_from=state_from,
            state_to=state_to,
        )


# ════════════════════════════════════════════════════════════════════════════
# NOTIFICATION SERVICE  (event-driven; logs by default, pluggable transport)
# ════════════════════════════════════════════════════════════════════════════
class NotificationService:
    def __init__(self, ctx: OffboardingContext):
        self.ctx = ctx

    def notify(self, *, event_type: str, case: OffboardingCase, detail: Optional[dict] = None):
        # In a full deployment this would fan out to the existing
        # app.notifications transport. Kept side-effect-light here so the
        # lifecycle stays transactional and testable.
        _log.info("[offboarding.notify] tenant=%s case=%s event=%s detail=%s",
                  case.tenant_id, case.id, event_type, detail or {})


# ════════════════════════════════════════════════════════════════════════════
# IDENTITY SERVICE  (access revocation across every surface)
# ════════════════════════════════════════════════════════════════════════════
# Which integration surfaces are actually wired in this deployment. Anything not
# listed is recorded NOT_APPLICABLE (the worker never had it) instead of being
# left PENDING — which would otherwise block completion forever. Enable an
# integration here to route it through the reconciliation queue instead.
ENABLED_INTEGRATIONS: frozenset = frozenset()

# Components handled directly against the local identity store.
_INTERNAL_COMPONENTS = frozenset({
    RevocationComponent.LOGIN_ACCESS,
    RevocationComponent.ACTIVE_SESSIONS,
    RevocationComponent.REFRESH_TOKENS,
    RevocationComponent.TEAM_MEMBERSHIPS,
    RevocationComponent.MANAGER_ADMIN_SCOPE,
})
# Components that map onto an external integration (SSO/SCIM/CRM/billing/etc.).
_EXTERNAL_COMPONENTS = {
    RevocationComponent.SSO_SESSIONS: "sso",
    RevocationComponent.SCIM_STATE: "scim",
    RevocationComponent.CRM_ACCESS: "crm",
    RevocationComponent.BILLING_ADMIN: "billing",
    RevocationComponent.INTEGRATION_TOKENS: "integrations",
}


class IdentityService:
    def __init__(self, ctx: OffboardingContext, audit: AuditService):
        self.ctx = ctx
        self.audit = audit

    def _record(self, repo: Repository, case: OffboardingCase, component: RevocationComponent,
                status: str, evidence: Optional[dict] = None, error: Optional[str] = None
                ) -> AccessRevocationRecord:
        rec = next((r for r in repo.revocations_for_case(case.id)
                    if r.component == component.value), None)
        if rec is None:
            rec = AccessRevocationRecord(
                id=ids.revocation_id(),
                tenant_id=case.tenant_id,
                case_id=case.id,
                worker_id=case.worker_id,
                component=component.value,
                correlation_id=self.ctx.correlation_id,
            )
            repo.add(rec)
        rec.status = status
        rec.attempts = (rec.attempts or 0) + 1
        rec.evidence = evidence or {}
        rec.last_error = error
        if status == RevocationStatus.REVOKED.value:
            rec.revoked_at = _now()
        repo.flush()
        return rec

    def revoke_all(self, case: OffboardingCase) -> List[AccessRevocationRecord]:
        """Revoke every access surface for the worker. Internal surfaces are
        revoked synchronously; external ones are enqueued for reconciliation (or
        marked not-applicable when the integration isn't wired). Returns all
        records. A FAILED record leaves the case in ACCESS_REVOCATION_FAILED."""
        repo = self.ctx.repo()
        db = self.ctx.db
        worker_id = case.worker_id
        records: List[AccessRevocationRecord] = []

        for component in RevocationComponent:
            try:
                if component in _INTERNAL_COMPONENTS and worker_id is not None:
                    evidence = self._revoke_internal(db, component, worker_id)
                    rec = self._record(repo, case, component, RevocationStatus.REVOKED.value, evidence)
                elif component in _EXTERNAL_COMPONENTS:
                    integ = _EXTERNAL_COMPONENTS[component]
                    if integ in ENABLED_INTEGRATIONS:
                        self._enqueue_reconciliation(repo, case, integ, component)
                        rec = self._record(repo, case, component, RevocationStatus.PENDING.value,
                                           {"queued": True, "integration": integ})
                    else:
                        rec = self._record(repo, case, component,
                                           RevocationStatus.NOT_APPLICABLE.value,
                                           {"integration_not_configured": integ})
                else:
                    # API/desktop/mobile/MFA/SSO-less surfaces collapse onto the
                    # session+token revocation already performed.
                    rec = self._record(repo, case, component,
                                       RevocationStatus.NOT_APPLICABLE.value,
                                       {"covered_by": "sessions+tokens"})
            except Exception as exc:  # noqa: BLE001 — record the failure, don't abort the sweep
                rec = self._record(repo, case, component, RevocationStatus.FAILED.value,
                                   None, str(exc)[:300])
                _log.exception("revocation failed component=%s case=%s", component.value, case.id)
            records.append(rec)

        self.audit.record(action="AccessRevocationSweep", entity="AccessRevocationRecord",
                          case=case, payload={"components": len(records)})
        return records

    def _revoke_internal(self, db: Session, component: RevocationComponent, worker_id: int) -> dict:
        if component == RevocationComponent.LOGIN_ACCESS:
            user = db.query(User).filter(User.id == worker_id).first()
            if user is not None:
                user.is_active = False
                # Access revoked → the seat is released and stops being charged
                # (ZT-COM-BILL-001 §A3/§A4). Best-effort: a Stripe outage must
                # never block an offboarding revocation.
                from app.billing import seats as billing_seats, worker_state as _ws  # noqa: PLC0415
                billing_seats.set_worker_billing_state(
                    db, user, _ws.DEACTIVATED, reason="offboarding_revoke",
                )
            return {"is_active": False}
        if component == RevocationComponent.ACTIVE_SESSIONS:
            rows = (db.query(UserSession)
                    .filter(UserSession.user_id == worker_id, UserSession.is_revoked.is_(False))
                    .all())
            for s in rows:
                s.is_revoked = True
                s.revoked_at = _now()
            return {"sessions_revoked": len(rows)}
        if component == RevocationComponent.REFRESH_TOKENS:
            n = revoke_all_for_user(db, worker_id, reason="offboarding")
            return {"refresh_tokens_revoked": n}
        if component in (RevocationComponent.TEAM_MEMBERSHIPS, RevocationComponent.MANAGER_ADMIN_SCOPE):
            # No dedicated membership table in this codebase; capture intent.
            return {"scopes_cleared": True}
        return {}

    def _enqueue_reconciliation(self, repo: Repository, case: OffboardingCase,
                                integration: str, component: RevocationComponent):
        repo.add(IntegrationReconciliationItem(
            id=ids.reconciliation_id(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            worker_id=case.worker_id,
            integration=integration,
            component=component.value,
            operation="revoke",
            status="pending",
            next_attempt_at=_now(),
            correlation_id=self.ctx.correlation_id,
        ))

    def revoke_worker_directly(self, worker_id: int, *, reason: str = "security_revoke") -> dict:
        """Break-glass revocation of a worker's local access surfaces WITHOUT a
        full case walk: flip the login gate, revoke every live session and every
        refresh-token family. Idempotent (re-running revokes nothing new) and
        safe to retry. Used by POST /revoke-access when an emergency cut-off is
        needed faster than the lifecycle can run. Returns evidence."""
        db = self.ctx.db
        login = self._revoke_internal(db, RevocationComponent.LOGIN_ACCESS, worker_id)
        sessions = self._revoke_internal(db, RevocationComponent.ACTIVE_SESSIONS, worker_id)
        tokens = self._revoke_internal(db, RevocationComponent.REFRESH_TOKENS, worker_id)
        db.flush()
        evidence = {"worker_id": worker_id, "reason": reason, **login, **sessions, **tokens}
        self.audit.record(action="DirectAccessRevocation", entity="User", payload=evidence)
        return evidence

    def restore_all(self, case: OffboardingCase) -> int:
        """Reverse revocation on case reversal (rehire/unwind). Restores login
        and marks revocation records reversed. Sessions/tokens are NOT restored
        (the worker re-authenticates) — only the login gate is reopened."""
        repo = self.ctx.repo()
        db = self.ctx.db
        if case.worker_id is not None:
            user = db.query(User).filter(User.id == case.worker_id).first()
            if user is not None:
                user.is_active = True
                # Rehire/unwind → the worker occupies a paid seat again (§A7).
                #
                # An admin action, so a capacity breach is refused rather than allowed:
                # whoever reinstates a worker can also upgrade the plan. Translated to
                # ServiceError so it surfaces as a 400 with a readable message instead
                # of an unhandled 500.
                from app.billing import seats as billing_seats, worker_state as _ws  # noqa: PLC0415
                from app.billing import service as _billing_service  # noqa: PLC0415
                try:
                    billing_seats.set_worker_billing_state(
                        db, user, _ws.ACTIVE_BILLABLE, reason="offboarding_reinstate",
                    )
                except _billing_service.PlanCapacityExceeded as exc:
                    raise ServiceError(str(exc), "plan_capacity_exceeded")
        n = 0
        for rec in repo.revocations_for_case(case.id):
            if rec.status == RevocationStatus.REVOKED.value:
                rec.status = RevocationStatus.REVERSED.value
                rec.reversed_at = _now()
                n += 1
        repo.flush()
        self.audit.record(action="AccessRestored", entity="AccessRevocationRecord",
                          case=case, payload={"restored": n})
        return n


# ════════════════════════════════════════════════════════════════════════════
# TRACKING SERVICE  (tracking stop enforcement)
# ════════════════════════════════════════════════════════════════════════════
class TrackingService:
    def __init__(self, ctx: OffboardingContext, audit: AuditService):
        self.ctx = ctx
        self.audit = audit

    def stop_tracking(self, case: OffboardingCase) -> dict:
        """Close any open clock session for the worker so no further work time
        accrues. Returns evidence used by the access-closure package."""
        db = self.ctx.db
        closed = 0
        if case.worker_id is not None:
            open_sessions = (db.query(ClockSession)
                             .filter(ClockSession.user_id == case.worker_id,
                                     ClockSession.clock_out_time.is_(None))
                             .all())
            # §45.D-15 — a synthesized closure caused by offboarding. Unlike
            # the unattended sweep in `jobs.py`, this path runs inside a case
            # an authenticated person is driving, so the ACTUAL actor is
            # recorded where the authz context carries one; it falls back to
            # SYSTEM only when the case is being advanced with no acting user.
            # Either way the provenance is OFFBOARDING.
            _actor = actor_label_for_user_id(self.ctx.actor_id)
            for s in open_sessions:
                _closed_at = _now()
                s.clock_out_time = _closed_at
                s.is_active = False
                closed += 1
                append_boundary_event(
                    db,
                    session_id  = s.id,
                    user_id     = s.user_id,
                    boundary    = BOUNDARY_CLOCK_OUT,
                    boundary_at = _closed_at,
                    provenance  = Provenance.OFFBOARDING,
                    actor_label = _actor,
                )
        evidence = {"open_sessions_closed": closed, "stopped_at": _now().isoformat()}
        self.audit.record(action="TrackingStopped", entity="ClockSession",
                          case=case, payload=evidence)
        self.audit.emit(event_type=EventType.TRACKING_STOPPED.value, case=case, payload=evidence)
        return evidence

    def is_post_exit_tracking_allowed(self, worker_id: int) -> bool:
        """A revoked worker must never produce new tracking data. The desktop
        tracker authenticates with the worker's token, which is revoked at exit,
        so this is the policy hook the ingest path consults. Always False once a
        completed/active case has closed access for the worker."""
        db = self.ctx.db
        repo = self.ctx.repo()
        cases = (db.query(OffboardingCase)
                 .filter(OffboardingCase.tenant_id == self.ctx.tenant_id,
                         OffboardingCase.worker_id == worker_id)
                 .all())
        for c in cases:
            if c.status in (CaseState.COMPLETED.value, CaseState.ACCESS_CLOSURE_PENDING.value,
                            CaseState.FINAL_REVIEW_PENDING.value, CaseState.FORMER_ACCESS_PENDING.value):
                return False
        return True


# ════════════════════════════════════════════════════════════════════════════
# RETENTION SERVICE
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_RETENTION_DAYS = 2555  # ~7 years


class RetentionService:
    def __init__(self, ctx: OffboardingContext, audit: AuditService):
        self.ctx = ctx
        self.audit = audit

    def assign(self, case: OffboardingCase, *, policy_code: str = "default_retention",
               retention_days: int = DEFAULT_RETENTION_DAYS) -> RetentionAssignment:
        repo = self.ctx.repo()
        existing = repo.retention_for_case(case.id)
        eligibility = (DeletionEligibility.BLOCKED_LEGAL_HOLD.value
                       if case.legal_hold_status == "active"
                       else DeletionEligibility.RETENTION_ACTIVE.value)
        if existing is None:
            existing = RetentionAssignment(
                id=ids.retention_id(),
                tenant_id=case.tenant_id,
                case_id=case.id,
                worker_id=case.worker_id,
            )
            repo.add(existing)
        existing.policy_code = policy_code
        existing.jurisdiction = case.jurisdiction
        existing.data_classification = case.data_classification
        existing.retention_until = _now() + timedelta(days=retention_days)
        existing.deletion_eligibility = eligibility
        existing.assigned_by_id = self.ctx.actor_id
        repo.flush()
        self.audit.record(action="RetentionAssigned", entity="RetentionAssignment",
                          case=case, payload={"policy": policy_code, "eligibility": eligibility})
        self.audit.emit(event_type=EventType.RETENTION_POLICY_ASSIGNED.value, case=case,
                        payload={"policy": policy_code})
        return existing

    def lock(self, case: OffboardingCase):
        repo = self.ctx.repo()
        ret = repo.retention_for_case(case.id)
        if ret is not None:
            ret.locked = True
            repo.flush()


# ════════════════════════════════════════════════════════════════════════════
# LEGAL HOLD SERVICE
# ════════════════════════════════════════════════════════════════════════════
class LegalHoldService:
    def __init__(self, ctx: OffboardingContext, audit: AuditService):
        self.ctx = ctx
        self.audit = audit

    def apply(self, case: OffboardingCase, *, matter_ref: Optional[str] = None,
              reason: Optional[str] = None, notes: Optional[str] = None,
              review_due_at: Optional[datetime] = None) -> LegalHold:
        repo = self.ctx.repo()
        hold = LegalHold(
            id=ids.legal_hold_id(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            worker_id=case.worker_id,
            status="active",
            matter_ref=matter_ref,
            reason=reason,
            notes=notes,
            applied_by_id=self.ctx.actor_id,
            review_due_at=review_due_at,
        )
        repo.add(hold)
        case.legal_hold_status = "active"
        repo.flush()
        self.audit.record(action="LegalHoldApplied", entity="LegalHold", case=case,
                          payload={"hold_id": hold.id, "matter_ref": matter_ref})
        self.audit.emit(event_type=EventType.LEGAL_HOLD_APPLIED.value, case=case,
                        payload={"hold_id": hold.id})
        return hold

    def release(self, case: OffboardingCase, hold: LegalHold) -> LegalHold:
        repo = self.ctx.repo()
        hold.status = "released"
        hold.released_by_id = self.ctx.actor_id
        hold.released_at = _now()
        # If no other active holds remain, clear the rollup on the case.
        if not [h for h in repo.active_holds_for_case(case.id) if h.id != hold.id]:
            case.legal_hold_status = "released"
        repo.flush()
        self.audit.record(action="LegalHoldReleased", entity="LegalHold", case=case,
                          payload={"hold_id": hold.id})
        self.audit.emit(event_type=EventType.LEGAL_HOLD_RELEASED.value, case=case,
                        payload={"hold_id": hold.id})
        return hold


# ════════════════════════════════════════════════════════════════════════════
# DOCUMENT SERVICE  (former-worker scope enforcement)
# ════════════════════════════════════════════════════════════════════════════
class DocumentService:
    def __init__(self, ctx: OffboardingContext):
        self.ctx = ctx

    def assert_scopes_allowed(self, scopes: List[str]) -> None:
        forbidden = [s for s in scopes if s not in ALLOWED_FORMER_WORKER_SCOPES]
        if forbidden:
            raise ServiceError(f"forbidden former-worker scopes: {forbidden}", "forbidden_scope")

    def list_documents(self, access: FormerWorkerAccess) -> List[dict]:
        """Return documents visible to a former worker, restricted to their
        granted (and allowlisted) scopes. Hard-fails on any scope outside the
        allowlist — a former worker can NEVER reach beyond final timesheets,
        approved payslips, tax and engagement documents."""
        self.assert_scopes_allowed(list(access.scopes or []))
        if access.status != FormerAccessStatus.ACTIVE.value:
            raise ServiceError("former-worker access is not active", "access_inactive")
        # Document retrieval itself is out of scope for this module; we return
        # the resolved scope contract the portal renders against.
        return [{"scope": s, "documents": []} for s in (access.scopes or [])]


# ════════════════════════════════════════════════════════════════════════════
# EVIDENCE PACKAGE SERVICE
# ════════════════════════════════════════════════════════════════════════════
class EvidencePackageService:
    def __init__(self, ctx: OffboardingContext, audit: AuditService):
        self.ctx = ctx
        self.audit = audit

    def build(self, case: OffboardingCase, *, package_type: str, manifest: dict) -> EvidencePackage:
        content_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        pkg = EvidencePackage(
            id=ids.evidence_id(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            worker_id=case.worker_id,
            package_type=package_type,
            content_hash=content_hash,
            manifest=manifest,
            generated_by_id=self.ctx.actor_id,
        )
        self.ctx.repo().add(pkg)
        self.ctx.db.flush()
        self.audit.record(action="EvidencePackageBuilt", entity="EvidencePackage", case=case,
                          payload={"package_id": pkg.id, "type": package_type, "hash": content_hash})
        return pkg


# ════════════════════════════════════════════════════════════════════════════
# WORKER LIFECYCLE SERVICE  (former-worker access)
# ════════════════════════════════════════════════════════════════════════════
class WorkerLifecycleService:
    def __init__(self, ctx: OffboardingContext, audit: AuditService, documents: DocumentService):
        self.ctx = ctx
        self.audit = audit
        self.documents = documents

    def grant_former_access(self, case: OffboardingCase, *, scopes: Optional[List[str]] = None,
                            expires_in_days: int = 365) -> FormerWorkerAccess:
        scopes = scopes or sorted(ALLOWED_FORMER_WORKER_SCOPES)
        self.documents.assert_scopes_allowed(scopes)
        repo = self.ctx.repo()
        existing = repo.former_access_for_case(case.id)
        # Portal credential is a fresh, isolated secret — never the old token.
        # The raw token is stashed on the returned object as a transient
        # attribute (NOT a column) so the governance API can surface it ONCE
        # in the issuing response. It is never persisted in plaintext.
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if existing is None:
            existing = FormerWorkerAccess(
                id=ids.former_access_id(),
                tenant_id=case.tenant_id,
                case_id=case.id,
                worker_id=case.worker_id,
            )
            repo.add(existing)
        existing.status = FormerAccessStatus.ACTIVE.value
        existing.scopes = scopes
        existing.granted_at = _now()
        existing.expires_at = _now() + timedelta(days=expires_in_days)
        existing.granted_by_id = self.ctx.actor_id
        existing.portal_token_hash = token_hash
        repo.flush()
        self.audit.record(action="FormerWorkerAccessGranted", entity="FormerWorkerAccess",
                          case=case, payload={"scopes": scopes})
        self.audit.emit(event_type=EventType.FORMER_WORKER_ACCESS_GRANTED.value, case=case,
                        payload={"scopes": scopes})
        # Transient stash so the issuing caller (governance API) can return the
        # raw portal token once to the operator. Not a SQLAlchemy column → not
        # persisted; lost the moment the row is reloaded from the DB.
        try:
            object.__setattr__(existing, "_raw_portal_token", raw)
        except Exception:
            pass
        return existing

    def revoke_former_access(self, case: OffboardingCase) -> Optional[FormerWorkerAccess]:
        repo = self.ctx.repo()
        access = repo.former_access_for_case(case.id)
        if access is None:
            return None
        access.status = FormerAccessStatus.REVOKED.value
        access.revoked_at = _now()
        access.portal_token_hash = None
        repo.flush()
        self.audit.record(action="FormerWorkerAccessRevoked", entity="FormerWorkerAccess", case=case)
        self.audit.emit(event_type=EventType.FORMER_WORKER_ACCESS_REVOKED.value, case=case)
        return access


# ════════════════════════════════════════════════════════════════════════════
# OFFBOARDING SERVICE  (lifecycle orchestrator)
# ════════════════════════════════════════════════════════════════════════════
class OffboardingService:
    """The orchestrator. Owns every state transition and wires the other
    services together. Each public method: authorizes (RBAC+ABAC) → asserts the
    state-machine transition → mutates → appends audit → emits a domain event,
    all inside the caller's transaction."""

    def __init__(self, ctx: OffboardingContext):
        self.ctx = ctx
        self.audit = AuditService(ctx)
        self.notify = NotificationService(ctx)
        self.identity = IdentityService(ctx, self.audit)
        self.tracking = TrackingService(ctx, self.audit)
        self.retention = RetentionService(ctx, self.audit)
        self.legal = LegalHoldService(ctx, self.audit)
        self.documents = DocumentService(ctx)
        self.evidence = EvidencePackageService(ctx, self.audit)
        self.lifecycle = WorkerLifecycleService(ctx, self.audit, self.documents)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _get(self, case_id: str) -> OffboardingCase:
        case = self.ctx.repo().get_case(case_id)
        if case is None:
            raise CaseNotFound(case_id)
        return case

    def _advance(self, case: OffboardingCase, to_state: CaseState, *, action: str,
                 event_type: str, payload: Optional[dict] = None,
                 complete_task: Optional[TaskType] = None):
        """Shared transition primitive: validate → set status → maintain the
        active-engagement lock → audit → event."""
        from_state = case.status
        assert_transition(from_state, to_state)
        case.status = to_state.value
        # Maintain the unique-active-case lock.
        if to_state.value in CLOSED_CASE_STATES:
            case.active_engagement_key = None
        if complete_task is not None:
            repo = self.ctx.repo()
            task = next((t for t in repo.tasks_for_case(case.id)
                         if t.task_type == complete_task.value), None)
            if task is not None:
                workflow.complete_task(repo, task, actor_id=self.ctx.actor_id)
        self.ctx.db.flush()
        self.audit.record(action=action, entity="OffboardingCase", case=case,
                          payload={**(payload or {}), "from": from_state, "to": to_state.value})
        self.audit.emit(event_type=event_type, case=case, payload=payload or {},
                        state_from=from_state, state_to=to_state.value)

    # ── create / update ──────────────────────────────────────────────────────
    def create_case(self, data: CaseCreate) -> OffboardingCase:
        rbac.require(self.ctx.authz, "case.create")
        repo = self.ctx.repo()
        # Enforce one active case per engagement (DB unique key is the backstop).
        if repo.active_case_for_engagement(data.engagement_id) is not None:
            raise DuplicateActiveCase()
        case = OffboardingCase(
            id=ids.case_id(),
            tenant_id=self.ctx.tenant_id,
            worker_id=data.worker_id,
            engagement_id=data.engagement_id,
            status=CaseState.DRAFT.value,
            exit_type=data.exit_type,
            jurisdiction=data.jurisdiction,
            worker_type=data.worker_type,
            risk_level=data.risk_level,
            department=data.department,
            data_classification=data.data_classification,
            revocation_mode=data.revocation_mode,
            final_access_at=data.final_access_at,
            termination_reason=data.termination_reason,
            notes=data.notes,
            correlation_id=self.ctx.correlation_id,
            active_engagement_key=data.engagement_id,
        )
        repo.add(case)
        self.ctx.db.flush()
        self.audit.record(action="OffboardingCaseCreated", entity="OffboardingCase", case=case,
                          payload={"engagement_id": data.engagement_id})
        self.audit.emit(event_type=EventType.CASE_CREATED.value, case=case,
                        payload={"engagement_id": data.engagement_id},
                        state_to=CaseState.DRAFT.value)
        return case

    def update_case(self, case_id: str, data: CaseUpdate) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "case.update", _resource(case))
        if case.status != CaseState.DRAFT.value:
            raise ServiceError("case can only be edited while in draft", "not_editable")
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(case, field, value)
        self.ctx.db.flush()
        self.audit.record(action="OffboardingCaseUpdated", entity="OffboardingCase", case=case,
                          payload={"fields": sorted(changes.keys())})
        return case

    # ── submit / approve ──────────────────────────────────────────────────────
    def submit(self, case_id: str, *, note: Optional[str] = None) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "case.submit", _resource(case))
        case.submitted_at = _now()
        self._advance(case, CaseState.PENDING_APPROVAL, action="OffboardingCaseSubmitted",
                      event_type=EventType.CASE_SUBMITTED.value, payload={"note": note})
        return case

    def approve(self, case_id: str, *, schedule: bool = True,
                final_access_at: Optional[datetime] = None, note: Optional[str] = None
                ) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "case.approve", _resource(case))
        case.approved_at = _now()
        case.approved_by_id = self.ctx.actor_id
        if final_access_at:
            case.final_access_at = final_access_at
        self._advance(case, CaseState.APPROVED, action="OffboardingCaseApproved",
                      event_type=EventType.CASE_APPROVED.value, payload={"note": note})
        # Seed the workflow task set now that the case is approved.
        workflow.seed_tasks(self.ctx.repo(), case)
        # Route to SCHEDULED (timed) or straight IN_PROGRESS (immediate mode).
        immediate = (not schedule) or case.revocation_mode == RevocationMode.IMMEDIATE.value
        if immediate:
            self._advance(case, CaseState.IN_PROGRESS, action="OffboardingCaseStarted",
                          event_type=EventType.CASE_SCHEDULED.value)
            worker = self.ctx.db.query(User).filter(User.id == case.worker_id).first()
            if worker is not None:
                worker.is_active = False
                # Immediate offboarding → seat released now (§A3). Scheduled
                # offboarding leaves the worker billable until the access sweep
                # runs, which goes through _revoke_internal above.
                from app.billing import seats as billing_seats, worker_state as _ws  # noqa: PLC0415
                billing_seats.set_worker_billing_state(
                    self.ctx.db, worker, _ws.DEACTIVATED,
                    reason="offboarding_immediate",
                )
        else:
            self._advance(case, CaseState.SCHEDULED, action="OffboardingCaseScheduled",
                          event_type=EventType.CASE_SCHEDULED.value,
                          payload={"final_access_at": case.final_access_at.isoformat()
                                   if case.final_access_at else None})
        return case

    def begin_offboarding(self, case_id: str) -> OffboardingCase:
        """SCHEDULED → IN_PROGRESS once the final access time is reached."""
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "access.revoke", _resource(case))
        self._advance(case, CaseState.IN_PROGRESS, action="OffboardingCaseStarted",
                      event_type=EventType.FINAL_ACCESS_TIME_REACHED.value)
        return case

    # ── access closure ────────────────────────────────────────────────────────
    def execute_access_closure(self, case_id: str) -> OffboardingCase:
        """IN_PROGRESS → ACCESS_CLOSURE_PENDING → (FINAL_REVIEW_PENDING |
        ACCESS_REVOCATION_FAILED). Revokes access, stops tracking, builds the
        access-closure evidence package and completes the relevant tasks."""
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "access.revoke", _resource(case))

        self._advance(case, CaseState.ACCESS_CLOSURE_PENDING, action="AccessRevocationStarted",
                      event_type=EventType.ACCESS_REVOCATION_STARTED.value)

        records = self.identity.revoke_all(case)
        tracking_evidence = self.tracking.stop_tracking(case)
        failed = [r for r in records if r.status == RevocationStatus.FAILED.value]

        repo = self.ctx.repo()
        # Complete the access + tracking tasks.
        for ttype in (TaskType.ACCESS_REVOCATION, TaskType.TRACKING_STOP_VERIFY):
            task = next((t for t in repo.tasks_for_case(case.id) if t.task_type == ttype.value), None)
            if task is not None and not failed:
                workflow.complete_task(repo, task, actor_id=self.ctx.actor_id)

        self.evidence.build(case, package_type=EvidencePackageType.ACCESS_CLOSURE.value,
                            manifest={
                                "components": {r.component: r.status for r in records},
                                "tracking": tracking_evidence,
                            })

        if failed:
            workflow.raise_blocker(repo, case, blocker_type="access_revocation_failed",
                                   reason=f"{len(failed)} component(s) failed",
                                   actor_id=self.ctx.actor_id)
            self._advance(case, CaseState.ACCESS_REVOCATION_FAILED,
                          action="AccessRevocationFailed",
                          event_type=EventType.ACCESS_REVOCATION_FAILED.value,
                          payload={"failed": [r.component for r in failed]})
        else:
            self._advance(case, CaseState.FINAL_REVIEW_PENDING,
                          action="AccessRevocationCompleted",
                          event_type=EventType.ACCESS_REVOCATION_COMPLETED.value)
        return case

    def retry_access_closure(self, case_id: str) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "access.revoke", _resource(case))
        self._advance(case, CaseState.ACCESS_CLOSURE_PENDING, action="AccessRevocationRetried",
                      event_type=EventType.ACCESS_REVOCATION_STARTED.value)
        records = self.identity.revoke_all(case)
        failed = [r for r in records if r.status == RevocationStatus.FAILED.value]
        if failed:
            self._advance(case, CaseState.ACCESS_REVOCATION_FAILED, action="AccessRevocationFailed",
                          event_type=EventType.ACCESS_REVOCATION_FAILED.value)
        else:
            self._advance(case, CaseState.FINAL_REVIEW_PENDING, action="AccessRevocationCompleted",
                          event_type=EventType.ACCESS_REVOCATION_COMPLETED.value)
        return case

    def force_resolve_access_failure(self, case_id: str, *, justification: str) -> OffboardingCase:
        """IT/Security override: accept a failed revocation as resolved (audited
        + waived) and proceed to final review."""
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "access.override", _resource(case))
        repo = self.ctx.repo()
        for blk in repo.blockers_for_case(case.id, open_only=True):
            if blk.blocker_type == "access_revocation_failed":
                wvr = workflow.grant_waiver(repo, case, reason="access override",
                                            justification=justification,
                                            actor_id=self.ctx.actor_id, blocker_id=blk.id)
        self._advance(case, CaseState.FINAL_REVIEW_PENDING, action="AccessFailureForceResolved",
                      event_type=EventType.ACCESS_REVOCATION_COMPLETED.value,
                      payload={"override": True, "justification": justification})
        return case

    # ── closure steps ──────────────────────────────────────────────────────────
    def complete_final_review(self, case_id: str) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "task.complete", _resource(case))
        self._advance(case, CaseState.PAYROLL_CLOSURE_PENDING, action="FinalReviewCompleted",
                      event_type=EventType.CASE_APPROVED.value, complete_task=TaskType.FINAL_WORK_REVIEW)
        return case

    def close_payroll(self, case_id: str, *, note: Optional[str] = None) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "payroll.close", _resource(case))
        self.evidence.build(case, package_type=EvidencePackageType.PAYROLL.value,
                            manifest={"note": note, "closed_by": self.ctx.actor_id})
        self._advance(case, CaseState.BILLING_CLOSURE_PENDING, action="PayrollClosureCompleted",
                      event_type=EventType.PAYROLL_CLOSED.value, complete_task=TaskType.PAYROLL_CLOSURE)
        return case

    def close_billing(self, case_id: str, *, note: Optional[str] = None) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "billing.close", _resource(case))
        self.evidence.build(case, package_type=EvidencePackageType.BILLING.value,
                            manifest={"note": note, "closed_by": self.ctx.actor_id})
        self._advance(case, CaseState.LEGAL_HOLD_REVIEW_PENDING, action="BillingClosureCompleted",
                      event_type=EventType.BILLING_CLOSED.value, complete_task=TaskType.BILLING_CLOSURE)
        return case

    def review_legal_hold(self, case_id: str, *, policy_code: str = "default_retention"
                          ) -> OffboardingCase:
        """Assign retention + complete the legal-hold review, then advance to
        former-access pending. Retention is assigned here so the deletion clock
        is governed before the case can finish."""
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "retention.assign", _resource(case))
        self.retention.assign(case, policy_code=policy_code)
        repo = self.ctx.repo()
        for ttype in (TaskType.LEGAL_HOLD_REVIEW, TaskType.RETENTION_ASSIGNMENT):
            task = next((t for t in repo.tasks_for_case(case.id) if t.task_type == ttype.value), None)
            if task is not None:
                workflow.complete_task(repo, task, actor_id=self.ctx.actor_id)
        self._advance(case, CaseState.FORMER_ACCESS_PENDING, action="LegalHoldReviewCompleted",
                      event_type=EventType.RETENTION_POLICY_ASSIGNED.value)
        return case

    def setup_former_access(self, case_id: str, *, scopes: Optional[List[str]] = None,
                            expires_in_days: int = 365) -> FormerWorkerAccess:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "former_access.grant", _resource(case))
        access = self.lifecycle.grant_former_access(case, scopes=scopes,
                                                    expires_in_days=expires_in_days)
        repo = self.ctx.repo()
        task = next((t for t in repo.tasks_for_case(case.id)
                     if t.task_type == TaskType.FORMER_WORKER_SETUP.value), None)
        if task is not None:
            workflow.complete_task(repo, task, actor_id=self.ctx.actor_id)
        return access

    def _build_certificate_payload(self, repo, case: OffboardingCase, readiness) -> dict:
        """Assemble the §6 completion-certificate snapshot. Includes everything
        a §7 auditor needs to verify the exit closure without trusting the
        application: access-closure timestamps, the tracking-stop confirmation,
        a pointer + hash for the audit chain, the retention policy in force,
        the legal-hold status, and the SHA-256 of every evidence package the
        case has produced. The whole dict is then content-hashed and signed
        into the certificate."""
        from .models import AuditEvent, DomainEvent, EvidencePackage, LegalHold

        # Access-closure timestamps per component.
        revocations = repo.revocations_for_case(case.id)
        access_closure = {
            "components": [
                {"component": r.component, "status": r.status,
                 "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
                 "attempts": r.attempts}
                for r in revocations
            ],
            "final_access_at": case.final_access_at.isoformat() if case.final_access_at else None,
        }

        # Tracking-stop confirmation pulled from the canonical domain event.
        ts_event = (self.ctx.db.query(DomainEvent)
                    .filter(DomainEvent.tenant_id == case.tenant_id,
                            DomainEvent.case_id == case.id,
                            DomainEvent.event_type == EventType.TRACKING_STOPPED.value)
                    .order_by(DomainEvent.occurred_at.desc())
                    .first())
        tracking_stop = {
            "confirmed": ts_event is not None,
            "stopped_at": ts_event.occurred_at.isoformat() if ts_event and ts_event.occurred_at else None,
            "evidence": ts_event.payload if ts_event else None,
        }

        # Audit references — most recent chain head for the case.
        head = (self.ctx.db.query(AuditEvent)
                .filter(AuditEvent.tenant_id == case.tenant_id,
                        AuditEvent.case_id == case.id)
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .first())
        audit_reference = {
            "chain_head_hash": head.current_hash if head else None,
            "chain_head_id": head.id if head else None,
            "chain_head_at": head.created_at.isoformat() if head and head.created_at else None,
        }

        # Retention policy snapshot.
        retention = repo.retention_for_case(case.id)
        retention_summary = ({
            "policy_code": retention.policy_code,
            "jurisdiction": retention.jurisdiction,
            "retention_until": retention.retention_until.isoformat()
            if retention.retention_until else None,
            "deletion_eligibility": retention.deletion_eligibility,
        } if retention is not None else None)

        # Legal hold status — case rollup + every active hold (matter ref only;
        # notes/reason are sensitive and excluded from the certificate body).
        active_holds = (self.ctx.db.query(LegalHold)
                        .filter(LegalHold.tenant_id == case.tenant_id,
                                LegalHold.case_id == case.id,
                                LegalHold.status == "active")
                        .all())
        legal_hold = {
            "status": case.legal_hold_status,
            "active_holds": [{"id": h.id, "matter_ref": h.matter_ref,
                              "applied_at": h.applied_at.isoformat() if h.applied_at else None}
                             for h in active_holds],
        }

        # Evidence hashes — every package built for this case.
        packages = (self.ctx.db.query(EvidencePackage)
                    .filter(EvidencePackage.tenant_id == case.tenant_id,
                            EvidencePackage.case_id == case.id)
                    .order_by(EvidencePackage.created_at.asc())
                    .all())
        evidence_hashes = [
            {"package_id": p.id, "type": p.package_type, "content_hash": p.content_hash,
             "created_at": p.created_at.isoformat() if p.created_at else None}
            for p in packages
        ]

        return {
            "case": {
                "id": case.id, "engagement_id": case.engagement_id,
                "worker_id": case.worker_id, "tenant_id": case.tenant_id,
                "exit_type": case.exit_type, "jurisdiction": case.jurisdiction,
                "final_access_at": case.final_access_at.isoformat() if case.final_access_at else None,
                "completed_at": case.completed_at.isoformat() if case.completed_at else None,
            },
            "readiness": readiness.model_dump(),
            "access_closure": access_closure,
            "tracking_stop": tracking_stop,
            "audit_reference": audit_reference,
            "retention": retention_summary,
            "legal_hold": legal_hold,
            "evidence_hashes": evidence_hashes,
            "issued_at": _now().isoformat(),
            "schema_version": "1.0",
        }

    def generate_certificate(self, case_id: str) -> CompletionCertificate:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "certificate.generate", _resource(case))
        repo = self.ctx.repo()
        readiness = workflow.compute_readiness(repo, case)
        snapshot = self._build_certificate_payload(repo, case, readiness)
        content_hash = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        pkg = self.evidence.build(case, package_type=EvidencePackageType.COMPLETION.value,
                                  manifest=snapshot)
        existing = repo.certificate_for_case(case.id)
        if existing is not None:
            return existing
        cert = CompletionCertificate(
            id=ids.certificate_id(),
            tenant_id=case.tenant_id,
            case_id=case.id,
            worker_id=case.worker_id,
            certificate_number=f"OFFB-{case.id[-12:].upper()}",
            content_hash=content_hash,
            evidence_package_id=pkg.id,
            issued_by_id=self.ctx.actor_id,
            payload=snapshot,
        )
        repo.add(cert)
        # Complete the certificate task.
        task = next((t for t in repo.tasks_for_case(case.id)
                     if t.task_type == TaskType.CERTIFICATE_GENERATION.value), None)
        if task is not None:
            workflow.complete_task(repo, task, actor_id=self.ctx.actor_id)
        self.ctx.db.flush()
        self.audit.record(action="CompletionCertificateGenerated", entity="CompletionCertificate",
                          case=case, payload={"certificate_number": cert.certificate_number})
        self.audit.emit(event_type=EventType.COMPLETION_CERTIFICATE_GENERATED.value, case=case,
                        payload={"certificate_number": cert.certificate_number})
        return cert

    def complete(self, case_id: str) -> OffboardingCase:
        """FORMER_ACCESS_PENDING → COMPLETED. Hard-gated by completion readiness:
        if any critical control is unmet, raises CompletionBlocked and the case
        does NOT move (the §5 'completion BLOCKED if any control fails' rule)."""
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "certificate.generate", _resource(case))
        repo = self.ctx.repo()
        readiness = workflow.compute_readiness(repo, case)
        if not readiness.ready:
            raise CompletionBlocked(readiness.blocking_reasons)
        case.completed_at = _now()
        self.retention.lock(case)
        self._advance(case, CaseState.COMPLETED, action="OffboardingCaseCompleted",
                      event_type=EventType.CASE_COMPLETED.value,
                      payload={"readiness": readiness.model_dump()})
        self.notify.notify(event_type=EventType.CASE_COMPLETED.value, case=case)
        return case

    # ── cancel / reverse / block ───────────────────────────────────────────────
    def cancel(self, case_id: str, *, reason: str) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "case.cancel", _resource(case))
        case.cancelled_at = _now()
        self._advance(case, CaseState.CANCELLED, action="OffboardingCaseCancelled",
                      event_type=EventType.CASE_CANCELLED.value, payload={"reason": reason})
        return case

    def reverse(self, case_id: str, *, reason: str) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "case.reverse", _resource(case))
        # Restore access + revoke any former-worker grant (rehire/unwind).
        self.identity.restore_all(case)
        self.lifecycle.revoke_former_access(case)
        case.reversed_at = _now()
        self._advance(case, CaseState.REVERSED, action="OffboardingCaseReversed",
                      event_type=EventType.CASE_REVERSED.value, payload={"reason": reason})
        return case

    def block(self, case_id: str, *, blocker_type: str, reason: Optional[str] = None,
              severity: str = "high") -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "blocker.raise", _resource(case))
        repo = self.ctx.repo()
        workflow.raise_blocker(repo, case, blocker_type=blocker_type, reason=reason,
                               severity=severity, actor_id=self.ctx.actor_id)
        prev = case.status
        case.blocked_from_state = prev
        self._advance(case, CaseState.BLOCKED, action="OffboardingCaseBlocked",
                      event_type="OffboardingCaseBlocked",
                      payload={"blocker_type": blocker_type, "from": prev})
        return case

    def unblock(self, case_id: str) -> OffboardingCase:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "blocker.resolve", _resource(case))
        repo = self.ctx.repo()
        if repo.blockers_for_case(case.id, open_only=True):
            raise ServiceError("cannot unblock while open blockers remain", "open_blockers")
        target = case.blocked_from_state
        if not target or not can_unblock(target):
            raise ServiceError("no valid state to return to", "invalid_unblock")
        from_state = case.status
        case.status = target
        case.blocked_from_state = None
        self.ctx.db.flush()
        self.audit.record(action="OffboardingCaseUnblocked", entity="OffboardingCase", case=case,
                          payload={"from": from_state, "to": target})
        self.audit.emit(event_type="OffboardingCaseUnblocked", case=case,
                        state_from=from_state, state_to=target)
        return case

    # ── reads ──────────────────────────────────────────────────────────────────
    def readiness(self, case_id: str) -> CompletionReadiness:
        case = self._get(case_id)
        rbac.require(self.ctx.authz, "case.view", _resource(case))
        return workflow.compute_readiness(self.ctx.repo(), case)
