"""Audit consistency checker (§6 — production governance).

A read-only verifier that proves the audit estate is sound. It answers four
questions a compliance auditor (or a release gate) asks:

  1. **Chain integrity** — is the hash-chained ``offboarding_audit_events``
     stream for the case intact end-to-end? (delegates to events.verify_chain)
  2. **Action completeness** — does every critical action the case's lifecycle
     state implies actually have an audit row? (a COMPLETED case with no
     ``AccessRevocationStarted`` audit is a red flag)
  3. **Evidence-hash validity** — does each EvidencePackage / certificate
     content_hash still equal the SHA-256 of its stored manifest/payload? (a
     mismatch means the evidence was mutated after sealing)
  4. **Correlation completeness** — does every domain event carry a
     correlation id, so a single user action's fan-out can be reconstructed?

Nothing here writes. It is safe to run continuously (a background sweep) and is
wired into the release gate: a failing report on any completed case blocks
release (§2 "audit event missing", §10 "immutable audit chain").
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from . import events
from .constants import CaseState
from .models import (
    AuditEvent, CompletionCertificate, DomainEvent, EvidencePackage,
    OffboardingCase,
)

_log = logging.getLogger("zoiko.offboarding.audit_checker")


# Audit actions that MUST be present once a case has reached a given lifecycle
# milestone. Keyed by the state at/after which the action is mandatory. The
# checker walks the case's current state and requires every action whose
# milestone the case has passed.
_REQUIRED_ACTIONS: Dict[str, List[str]] = {
    CaseState.ACCESS_CLOSURE_PENDING.value: ["AccessRevocationStarted"],
    CaseState.FINAL_REVIEW_PENDING.value:   ["AccessRevocationStarted", "TrackingStopped"],
    CaseState.COMPLETED.value: [
        "OffboardingCaseCreated", "AccessRevocationStarted", "TrackingStopped",
        "CompletionCertificateGenerated", "OffboardingCaseCompleted",
    ],
}

# Milestone ordering so "has the case passed milestone X" can be answered.
_STATE_ORDER = [
    CaseState.DRAFT.value, CaseState.PENDING_APPROVAL.value, CaseState.APPROVED.value,
    CaseState.SCHEDULED.value, CaseState.IN_PROGRESS.value,
    CaseState.ACCESS_CLOSURE_PENDING.value, CaseState.FINAL_REVIEW_PENDING.value,
    CaseState.PAYROLL_CLOSURE_PENDING.value, CaseState.BILLING_CLOSURE_PENDING.value,
    CaseState.LEGAL_HOLD_REVIEW_PENDING.value, CaseState.FORMER_ACCESS_PENDING.value,
    CaseState.COMPLETED.value,
]


@dataclass
class CaseAuditReport:
    case_id: str
    ok: bool = True
    chain_intact: bool = True
    missing_actions: List[str] = field(default_factory=list)
    invalid_evidence_hashes: List[str] = field(default_factory=list)
    events_missing_correlation: int = 0
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id, "ok": self.ok, "chain_intact": self.chain_intact,
            "missing_actions": self.missing_actions,
            "invalid_evidence_hashes": self.invalid_evidence_hashes,
            "events_missing_correlation": self.events_missing_correlation,
            "notes": self.notes,
        }


def _recompute_hash(obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _state_index(state: str) -> int:
    try:
        return _STATE_ORDER.index(state)
    except ValueError:
        return -1   # blocked/reversed/cancelled — not on the happy path


def check_case(db: Session, case: OffboardingCase) -> CaseAuditReport:
    """Run all four checks for one case. Read-only."""
    report = CaseAuditReport(case_id=case.id)

    # 1. Chain integrity.
    report.chain_intact = events.verify_chain(db, case.tenant_id, case.id)
    if not report.chain_intact:
        report.ok = False
        report.notes.append("hash chain broken — a historical audit row was altered or deleted")

    # 2. Action completeness — only enforced for states on the happy path that
    #    have a defined requirement at or below the case's current milestone.
    present = {
        a.action for a in db.query(AuditEvent.action)
        .filter(AuditEvent.tenant_id == case.tenant_id, AuditEvent.case_id == case.id)
        .all()
    }
    case_idx = _state_index(case.status)
    if case_idx >= 0:
        for milestone, actions in _REQUIRED_ACTIONS.items():
            if _state_index(milestone) <= case_idx:
                for action in actions:
                    if action not in present:
                        report.missing_actions.append(action)
        if report.missing_actions:
            report.ok = False
            report.notes.append(f"missing required audit action(s): {sorted(set(report.missing_actions))}")

    # 3. Evidence-hash validity — recompute each package + certificate hash.
    for pkg in (db.query(EvidencePackage)
                .filter(EvidencePackage.tenant_id == case.tenant_id,
                        EvidencePackage.case_id == case.id).all()):
        if _recompute_hash(pkg.manifest or {}) != pkg.content_hash:
            report.invalid_evidence_hashes.append(pkg.id)
    cert = (db.query(CompletionCertificate)
            .filter(CompletionCertificate.tenant_id == case.tenant_id,
                    CompletionCertificate.case_id == case.id).first())
    if cert is not None and _recompute_hash(cert.payload or {}) != cert.content_hash:
        report.invalid_evidence_hashes.append(cert.id)
    if report.invalid_evidence_hashes:
        report.ok = False
        report.notes.append("evidence hash mismatch — sealed evidence was mutated after issuance")

    # 4. Correlation completeness.
    report.events_missing_correlation = (
        db.query(DomainEvent)
        .filter(DomainEvent.tenant_id == case.tenant_id, DomainEvent.case_id == case.id,
                DomainEvent.correlation_id.is_(None))
        .count()
    )
    if report.events_missing_correlation > 0:
        report.ok = False
        report.notes.append(f"{report.events_missing_correlation} domain event(s) missing a correlation id")

    return report


def check_tenant(db: Session, *, tenant_id: str, limit: int = 500,
                 only_completed: bool = False) -> dict:
    """Sweep a tenant's cases and aggregate the per-case reports. Returns a
    summary plus the failing reports (passing ones are omitted to keep the
    payload small). Used by the ops endpoint and the release gate."""
    q = db.query(OffboardingCase).filter(OffboardingCase.tenant_id == tenant_id)
    if only_completed:
        q = q.filter(OffboardingCase.status == CaseState.COMPLETED.value)
    cases = q.order_by(OffboardingCase.updated_at.desc()).limit(limit).all()

    failing: List[dict] = []
    checked = 0
    for case in cases:
        checked += 1
        rep = check_case(db, case)
        if not rep.ok:
            failing.append(rep.as_dict())
    return {
        "tenant_id": tenant_id,
        "cases_checked": checked,
        "failing_count": len(failing),
        "all_ok": len(failing) == 0,
        "failing": failing,
    }


def check_all(db: Session, *, limit_per_tenant: int = 1000,
              only_completed: bool = False) -> dict:
    """Cross-tenant sweep — the release-gate entry point. ``all_ok`` is the
    single boolean a CI gate keys on."""
    tenants = [row[0] for row in db.query(OffboardingCase.tenant_id).distinct().all()]
    reports = [check_tenant(db, tenant_id=t, limit=limit_per_tenant,
                            only_completed=only_completed) for t in tenants]
    return {
        "all_ok": all(r["all_ok"] for r in reports),
        "tenants": len(tenants),
        "total_failing": sum(r["failing_count"] for r in reports),
        "by_tenant": reports,
    }
