"""Offboarding QA automation matrix (§1, §3-§6 of the production-hardening spec).

One named test per spec scenario so a failure maps straight back to a line.
Covers core workflow, the new metrics / edge-case / audit-checker modules, and
background-job hardening (retry, backoff, dead-letter, idempotency, replay).
The §2/§10 P0 release blockers live in test_offboarding_release_gate.py.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.offboarding import (
    audit_checker, edge_cases, events, metrics, reconciliation,
)
from app.offboarding.constants import (
    CaseState, ReconciliationStatus, RetentionPolicyCode,
)
from app.offboarding.edge_cases import EdgeCaseService, TenantDeleted
from app.offboarding.models import AuditEvent, EvidencePackage
from app.offboarding.rbac import AuthzContext, Role
from app.offboarding.repository import Repository
from app.offboarding.schemas import CaseCreate
from app.offboarding.services import (
    OffboardingContext, OffboardingService, ServiceError,
)
from database.models import ClockSession, RefreshToken, User, UserSession


def _now():
    return datetime.now(timezone.utc)


def _worker(db, email):
    u = User(name="QA Worker", email=email, role="employee", is_active=True)
    db.add(u); db.flush()
    return u


def _seed_devices(db, wid):
    db.add(UserSession(user_id=wid, token_hash=f"s{wid}", is_revoked=False))
    db.add(RefreshToken(user_id=wid, token_hash=f"r{wid}", family_id=f"f{wid}",
                        remember=True, is_revoked=False, expires_at=_now() + timedelta(days=30)))
    db.add(ClockSession(user_id=wid, clock_in_time=_now(), is_active=True))
    db.flush()


def _ctx(db, tenant="t1", roles=frozenset({Role.SUPER_ADMIN})):
    return OffboardingContext(db=db, authz=AuthzContext(user_id=1, tenant_id=tenant, roles=roles),
                              correlation_id="cor_qa")


def _svc(db, **kw):
    return OffboardingService(_ctx(db, **kw))


def _to_completed(db, svc, wid, eng="ENG-QA"):
    c = svc.create_case(CaseCreate(engagement_id=eng, worker_id=wid))
    svc.submit(c.id); svc.approve(c.id, schedule=False)
    svc.execute_access_closure(c.id); svc.complete_final_review(c.id)
    svc.close_payroll(c.id); svc.close_billing(c.id); svc.review_legal_hold(c.id)
    svc.setup_former_access(c.id); svc.generate_certificate(c.id); svc.complete(c.id)
    return c


# ════════════════════════════════════════════════════════════════════════════
# §1 CORE WORKFLOW
# ════════════════════════════════════════════════════════════════════════════
class TestCoreWorkflow:
    def test_create_case(self, db):
        c = _svc(db).create_case(CaseCreate(engagement_id="ENG-C", worker_id=_worker(db, "c@q").id))
        assert c.status == CaseState.DRAFT.value

    def test_approve_case(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-A", worker_id=_worker(db, "a@q").id))
        svc.submit(c.id)
        svc.approve(c.id, schedule=False)
        assert Repository(db, "t1").get_case(c.id).status == CaseState.IN_PROGRESS.value

    def test_scheduled_exit(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-S", worker_id=_worker(db, "s@q").id,
                                       final_access_at=_now() + timedelta(days=2)))
        svc.submit(c.id)
        svc.approve(c.id, schedule=True)
        assert Repository(db, "t1").get_case(c.id).status == CaseState.SCHEDULED.value

    def test_immediate_security_exit(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-I", worker_id=_worker(db, "i@q").id,
                                       revocation_mode="immediate"))
        svc.submit(c.id)
        svc.approve(c.id, schedule=True)   # immediate mode overrides the schedule flag
        assert Repository(db, "t1").get_case(c.id).status == CaseState.IN_PROGRESS.value

    def test_cancel_case(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-X", worker_id=_worker(db, "x@q").id))
        svc.cancel(c.id, reason="duplicate")
        assert Repository(db, "t1").get_case(c.id).status == CaseState.CANCELLED.value

    def test_reverse_completed_case(self, db):
        svc = _svc(db)
        w = _worker(db, "rev@q"); _seed_devices(db, w.id)
        c = _to_completed(db, svc, w.id, eng="ENG-REVQA")
        svc.reverse(c.id, reason="rehire")
        refreshed = Repository(db, "t1").get_case(c.id)
        assert refreshed.status == CaseState.REVERSED.value
        # Reversal restores the login gate.
        assert db.query(User).get(w.id).is_active is True


# ════════════════════════════════════════════════════════════════════════════
# §3 METRICS
# ════════════════════════════════════════════════════════════════════════════
class TestMetrics:
    def test_metrics_count_blocked_and_former_access(self, db):
        svc = _svc(db)
        w = _worker(db, "metric@q"); _seed_devices(db, w.id)
        _to_completed(db, svc, w.id, eng="ENG-MET")   # former access active + completed
        # A separate blocked case.
        b = svc.create_case(CaseCreate(engagement_id="ENG-BLK", worker_id=_worker(db, "b@q").id))
        svc.block(b.id, blocker_type="manual_hold", reason="qa")
        db.flush()
        m = metrics.compute_metrics(db, tenant_id="t1")
        assert m["cases"]["blocked"] >= 1
        assert m["former_worker_access_count"] >= 1
        assert "by_severity" in m["incidents"]

    def test_prometheus_text_renders_gauges(self, db):
        text = metrics.prometheus_text(db, tenant_id="t1")
        assert "offboarding_blocked_cases" in text
        assert "offboarding_former_worker_access" in text


# ════════════════════════════════════════════════════════════════════════════
# §4 EDGE CASES
# ════════════════════════════════════════════════════════════════════════════
class TestEdgeCases:
    def test_transfer_updates_department_and_audits(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-T", worker_id=_worker(db, "t@q").id,
                                       department="Eng"))
        edge = EdgeCaseService(_ctx(db))
        edge.transfer(c.id, new_department="Platform")
        assert Repository(db, "t1").get_case(c.id).department == "Platform"
        actions = {a.action for a in db.query(AuditEvent).filter(AuditEvent.case_id == c.id).all()}
        assert "WorkerTransferred" in actions

    def test_manager_reassignment(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-M", worker_id=_worker(db, "m@q").id))
        svc.submit(c.id); svc.approve(c.id, schedule=False)   # seeds tasks
        edge = EdgeCaseService(_ctx(db))
        edge.reassign_task(c.id, task_type="access_revocation", new_assignee_id=555)
        task = next(t for t in Repository(db, "t1").tasks_for_case(c.id)
                    if t.task_type == "access_revocation")
        assert task.assignee_id == 555

    def test_payroll_unavailable_blocks(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-PU", worker_id=_worker(db, "pu@q").id))
        edge = EdgeCaseService(_ctx(db))
        edge.mark_dependency_unavailable(c.id, dependency="payroll", detail="provider 503")
        open_blockers = Repository(db, "t1").blockers_for_case(c.id, open_only=True)
        assert any(b.blocker_type == "payroll_unavailable" for b in open_blockers)

    def test_deleted_tenant_guard_refuses(self, db, monkeypatch):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-DT", worker_id=_worker(db, "dt@q").id))
        # Simulate the commercial registry reporting this tenant as deleted.
        monkeypatch.setattr(edge_cases, "is_tenant_deleted", lambda db_, t: True)
        edge = EdgeCaseService(_ctx(db))
        with pytest.raises(TenantDeleted):
            edge.transfer(c.id, new_department="Anywhere")

    def test_rehire_reverses_and_restores(self, db):
        svc = _svc(db)
        w = _worker(db, "rh@q"); _seed_devices(db, w.id)
        c = _to_completed(db, svc, w.id, eng="ENG-RH")
        edge = EdgeCaseService(_ctx(db))
        result = edge.rehire(c.id, reason="boomerang hire")
        assert result["status"] == CaseState.REVERSED.value
        assert db.query(User).get(w.id).is_active is True


# ════════════════════════════════════════════════════════════════════════════
# §5 BACKGROUND JOB HARDENING
# ════════════════════════════════════════════════════════════════════════════
class TestJobHardening:
    def teardown_method(self):
        reconciliation.clear_adapters()

    def test_idempotent_enqueue(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-IDQ", worker_id=_worker(db, "idq@q").id))
        i1 = reconciliation.enqueue(db, case=c, integration="scim", component="scim_state")
        i2 = reconciliation.enqueue(db, case=c, integration="scim", component="scim_state")
        assert i1.id == i2.id   # no duplicate queue item

    def test_exponential_backoff_then_dead_letter(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-BO", worker_id=_worker(db, "bo@q").id))
        reconciliation.register_adapter("scim", lambda item: (_ for _ in ()).throw(RuntimeError("503")))
        item = reconciliation.enqueue(db, case=c, integration="scim", component="scim_state")

        reconciliation.process_item(db, item)
        assert item.status == ReconciliationStatus.FAILED.value
        first_backoff = item.next_attempt_at
        reconciliation.process_item(db, item)
        # Backoff grows (next attempt pushed further out as attempts increase).
        assert item.next_attempt_at >= first_backoff
        # Burn the rest of the budget → dead-letter (no infinite retry).
        for _ in range(item.max_attempts):
            reconciliation.process_item(db, item)
        assert item.status == ReconciliationStatus.DEAD_LETTER.value

    def test_terminal_item_is_noop_on_replay(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-NR", worker_id=_worker(db, "nr@q").id))
        reconciliation.register_adapter("crm", lambda item: {"ok": True})
        item = reconciliation.enqueue(db, case=c, integration="crm", component="crm_access")
        assert reconciliation.process_item(db, item) == ReconciliationStatus.SUCCEEDED.value
        attempts_after_success = item.attempts
        # Re-processing a succeeded item changes nothing (idempotent replay).
        assert reconciliation.process_item(db, item) == ReconciliationStatus.SUCCEEDED.value
        assert item.attempts == attempts_after_success

    def test_event_replay_protection_via_idempotency_key(self, db):
        svc = _svc(db)
        c = svc.create_case(CaseCreate(engagement_id="ENG-EV", worker_id=_worker(db, "ev@q").id))
        e1 = events.emit_event(db, tenant_id="t1", event_type="X", case_id=c.id,
                               idempotency_key="dup-1")
        e2 = events.emit_event(db, tenant_id="t1", event_type="X", case_id=c.id,
                               idempotency_key="dup-1")
        assert e1.id == e2.id   # replayed event dropped


# ════════════════════════════════════════════════════════════════════════════
# §6 AUDIT CONSISTENCY CHECKER
# ════════════════════════════════════════════════════════════════════════════
class TestAuditChecker:
    def test_completed_case_passes_all_checks(self, db):
        svc = _svc(db)
        w = _worker(db, "ck@q"); _seed_devices(db, w.id)
        c = _to_completed(db, svc, w.id, eng="ENG-CK")
        report = audit_checker.check_case(db, Repository(db, "t1").get_case(c.id))
        assert report.ok is True
        assert report.chain_intact is True
        assert report.missing_actions == []
        assert report.invalid_evidence_hashes == []

    def test_broken_chain_is_detected(self, db):
        svc = _svc(db)
        w = _worker(db, "tamper@q"); _seed_devices(db, w.id)
        c = _to_completed(db, svc, w.id, eng="ENG-TAMPER")
        # Tamper with a historical audit row's payload — the recomputed hash
        # will no longer match, breaking the chain from that row onward.
        row = (db.query(AuditEvent).filter(AuditEvent.case_id == c.id)
               .order_by(AuditEvent.created_at.asc()).first())
        row.payload = {**(row.payload or {}), "tampered": True}
        db.flush()
        report = audit_checker.check_case(db, Repository(db, "t1").get_case(c.id))
        assert report.chain_intact is False
        assert report.ok is False

    def test_mutated_evidence_hash_is_detected(self, db):
        svc = _svc(db)
        w = _worker(db, "evd@q"); _seed_devices(db, w.id)
        c = _to_completed(db, svc, w.id, eng="ENG-EVD")
        pkg = (db.query(EvidencePackage).filter(EvidencePackage.case_id == c.id).first())
        pkg.manifest = {**(pkg.manifest or {}), "mutated": True}   # content drifts from sealed hash
        db.flush()
        report = audit_checker.check_case(db, Repository(db, "t1").get_case(c.id))
        assert pkg.id in report.invalid_evidence_hashes
        assert report.ok is False
