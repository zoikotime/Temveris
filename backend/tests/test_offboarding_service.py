"""Offboarding service integration tests (§3, §9 release gates).

Drives the orchestrator end-to-end against in-memory SQLite and asserts:
  • the full happy-path lifecycle reaches COMPLETED;
  • completion is BLOCKED when controls are unmet;
  • every critical action leaves an append-only audit row + domain event, and
    the audit hash-chain verifies;
  • the one-active-case-per-engagement rule holds;
  • invalid transitions are rejected at the service boundary.
"""
import pytest

from app.offboarding import events
from app.offboarding.constants import CaseState, EventType
from app.offboarding.models import AuditEvent, DomainEvent
from app.offboarding.rbac import AuthzContext, Role
from app.offboarding.repository import Repository
from app.offboarding.schemas import CaseCreate
from app.offboarding.services import (
    CompletionBlocked, DuplicateActiveCase, OffboardingContext, OffboardingService,
)
from app.offboarding.state_machine import InvalidTransition
from database.models import User


def _worker(db, email="worker@example.com"):
    u = User(name="Worker", email=email, role="employee", is_active=True)
    db.add(u)
    db.flush()
    return u


def _svc(db, tenant="t1"):
    authz = AuthzContext(user_id=99, tenant_id=tenant, roles=frozenset({Role.SUPER_ADMIN}))
    return OffboardingService(OffboardingContext(db=db, authz=authz, correlation_id="cor_life"))


def _run_to_completion(db, svc, worker_id):
    case = svc.create_case(CaseCreate(engagement_id="ENG-LIFE", worker_id=worker_id,
                                      termination_reason="role eliminated"))
    svc.submit(case.id)
    svc.approve(case.id, schedule=False)            # → IN_PROGRESS
    svc.execute_access_closure(case.id)             # → FINAL_REVIEW_PENDING
    svc.complete_final_review(case.id)              # → PAYROLL_CLOSURE_PENDING
    svc.close_payroll(case.id)                      # → BILLING_CLOSURE_PENDING
    svc.close_billing(case.id)                      # → LEGAL_HOLD_REVIEW_PENDING
    svc.review_legal_hold(case.id)                  # → FORMER_ACCESS_PENDING
    svc.setup_former_access(case.id)
    svc.generate_certificate(case.id)
    svc.complete(case.id)                           # → COMPLETED
    return case


class TestHappyPath:
    def test_full_lifecycle_reaches_completed(self, db):
        svc = _svc(db)
        worker = _worker(db, "happy@example.com")
        case = _run_to_completion(db, svc, worker.id)
        refreshed = Repository(db, "t1").get_case(case.id)
        assert refreshed.status == CaseState.COMPLETED.value
        # Login gate was actually closed for the worker.
        assert db.get(User, worker.id).is_active is False
        # Active-case lock released on completion.
        assert refreshed.active_engagement_key is None

    def test_completion_certificate_and_former_access_created(self, db):
        svc = _svc(db)
        worker = _worker(db, "cert@example.com")
        case = _run_to_completion(db, svc, worker.id)
        repo = Repository(db, "t1")
        assert repo.certificate_for_case(case.id) is not None
        fwa = repo.former_access_for_case(case.id)
        assert fwa is not None and fwa.status == "active"
        # Former access is an isolated credential, never the old token.
        assert fwa.portal_token_hash is not None


class TestCompletionGate:
    def test_complete_before_ready_is_blocked(self, db):
        svc = _svc(db)
        worker = _worker(db, "early@example.com")
        case = svc.create_case(CaseCreate(engagement_id="ENG-EARLY", worker_id=worker.id))
        svc.submit(case.id)
        svc.approve(case.id, schedule=False)
        with pytest.raises(CompletionBlocked):
            svc.complete(case.id)
        # Case did NOT move to completed.
        assert Repository(db, "t1").get_case(case.id).status != CaseState.COMPLETED.value

    def test_legal_hold_blocks_readiness(self, db):
        svc = _svc(db)
        worker = _worker(db, "hold@example.com")
        case = svc.create_case(CaseCreate(engagement_id="ENG-HOLD", worker_id=worker.id))
        svc.submit(case.id)
        svc.approve(case.id, schedule=False)
        svc.execute_access_closure(case.id)
        svc.legal.apply(case, matter_ref="LIT-1", reason="litigation")
        readiness = svc.readiness(case.id)
        assert readiness.legal_hold_active is True
        assert readiness.ready is False
        assert "legal_hold_active" in readiness.blocking_reasons


class TestAuditAndEvents:
    def test_audit_rows_present_for_critical_actions(self, db):
        svc = _svc(db)
        worker = _worker(db, "audit@example.com")
        case = _run_to_completion(db, svc, worker.id)
        actions = {
            a.action for a in db.query(AuditEvent).filter(AuditEvent.case_id == case.id).all()
        }
        for expected in {"OffboardingCaseCreated", "AccessRevocationStarted",
                         "TrackingStopped", "CompletionCertificateGenerated",
                         "OffboardingCaseCompleted"}:
            assert expected in actions, f"missing audit action {expected}"

    def test_domain_events_emitted(self, db):
        svc = _svc(db)
        worker = _worker(db, "events@example.com")
        case = _run_to_completion(db, svc, worker.id)
        types = {
            e.event_type for e in db.query(DomainEvent).filter(DomainEvent.case_id == case.id).all()
        }
        assert EventType.CASE_CREATED.value in types
        assert EventType.CASE_COMPLETED.value in types
        assert EventType.TRACKING_STOPPED.value in types

    def test_audit_chain_verifies(self, db):
        svc = _svc(db)
        worker = _worker(db, "chain@example.com")
        case = _run_to_completion(db, svc, worker.id)
        assert events.verify_chain(db, "t1", case.id) is True

    def test_event_idempotency_key_dedupes(self, db):
        svc = _svc(db)
        case = svc.create_case(CaseCreate(engagement_id="ENG-IDEM"))
        first = events.emit_event(db, tenant_id="t1", event_type="X", case_id=case.id,
                                  idempotency_key="dup-key-1")
        second = events.emit_event(db, tenant_id="t1", event_type="X", case_id=case.id,
                                   idempotency_key="dup-key-1")
        assert first.id == second.id  # same row, replay dropped


class TestInvariants:
    def test_duplicate_active_case_rejected(self, db):
        svc = _svc(db)
        svc.create_case(CaseCreate(engagement_id="ENG-DUP"))
        with pytest.raises(DuplicateActiveCase):
            svc.create_case(CaseCreate(engagement_id="ENG-DUP"))

    def test_second_case_allowed_after_first_closed(self, db):
        svc = _svc(db)
        case = svc.create_case(CaseCreate(engagement_id="ENG-REUSE"))
        svc.cancel(case.id, reason="changed mind")
        # Lock released → a new case for the same engagement is allowed.
        again = svc.create_case(CaseCreate(engagement_id="ENG-REUSE"))
        assert again.status == CaseState.DRAFT.value

    def test_invalid_transition_rejected(self, db):
        svc = _svc(db)
        case = svc.create_case(CaseCreate(engagement_id="ENG-INVALID"))
        # Approving a Draft (skipping submit) is an illegal transition.
        with pytest.raises(InvalidTransition):
            svc.approve(case.id, schedule=False)
