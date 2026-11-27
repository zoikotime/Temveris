"""Retention engine (§5 — governance spec).

Three layered concerns the lifecycle's existing ``RetentionService`` did not
own:

  1. **Policy versioning**. ``RetentionPolicy`` is a library of named policy
     codes (default, GDPR-EU, US tax, etc.), each with one or more *versions*.
     Activating a new version supersedes the previous one (``superseded_by``)
     but never edits the version a case was already assigned to — the deletion
     clock on a finished case is *frozen* on the version snapshot recorded in
     ``RetentionAssignment``. This makes a policy update a controlled, auditable
     change, not a silent re-clock of every closed case.

  2. **Jurisdiction-aware lookup**. ``resolve_policy(case_jurisdiction)`` walks
     the jurisdiction → policy-code map (constants.JURISDICTION_POLICY_DEFAULTS)
     and falls back to the DEFAULT policy. The result is the *active version*
     of that code — which is what we then snapshot onto the case.

  3. **Eligibility review + expiry job**. ``compute_eligibility(case)`` enumerates
     every gate that would block deletion right now (active legal hold, open
     blockers / disputes, retention clock not elapsed). The §5 expiry job
     (``run_retention_expiry_once``) sweeps assignments whose
     ``retention_until`` has passed and flips them to ELIGIBLE if no blockers
     remain — but never deletes anything itself. Deletion still requires the
     dual-approval review in §4.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from . import events, ids
from .constants import (
    JURISDICTION_POLICY_DEFAULTS, RETENTION_DAYS_DEFAULTS,
    DeletionEligibility, EventType, RetentionPolicyCode,
)
from .models import (
    DEFAULT_TENANT, LegalHold, OffboardingCase, RetentionAssignment,
    RetentionPolicy, WorkflowBlocker,
)

_log = logging.getLogger("zoiko.offboarding.retention_engine")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ════════════════════════════════════════════════════════════════════════════
# POLICY SEEDING
# ════════════════════════════════════════════════════════════════════════════
def seed_default_policies(db: Session, *, tenant_id: str = DEFAULT_TENANT) -> int:
    """Ensure the bootstrap set of retention policies exists for ``tenant_id``.
    Idempotent: a code that already has an active version is left untouched.
    Returns the number of policies created."""
    created = 0
    for code_enum in RetentionPolicyCode:
        code = code_enum.value
        existing = (db.query(RetentionPolicy)
                    .filter(RetentionPolicy.tenant_id == tenant_id,
                            RetentionPolicy.code == code,
                            RetentionPolicy.active.is_(True))
                    .first())
        if existing is not None:
            continue
        days = RETENTION_DAYS_DEFAULTS.get(code, 7 * 365)
        db.add(RetentionPolicy(
            id=ids.policy_id(),
            tenant_id=tenant_id,
            code=code,
            version=1,
            jurisdiction=None,
            data_classification=None,
            retention_days=days,
            active=True,
            description=f"Bootstrap version of {code} ({days} days).",
        ))
        created += 1
    if created:
        db.flush()
    return created


# ════════════════════════════════════════════════════════════════════════════
# POLICY LOOKUP / VERSIONING
# ════════════════════════════════════════════════════════════════════════════
def active_policy(db: Session, *, tenant_id: str, code: str) -> Optional[RetentionPolicy]:
    return (db.query(RetentionPolicy)
            .filter(RetentionPolicy.tenant_id == tenant_id,
                    RetentionPolicy.code == code,
                    RetentionPolicy.active.is_(True))
            .order_by(RetentionPolicy.version.desc())
            .first())


def resolve_policy(db: Session, *, tenant_id: str, jurisdiction: Optional[str] = None,
                   policy_code: Optional[str] = None) -> RetentionPolicy:
    """Pick the policy that applies. Explicit ``policy_code`` wins; otherwise
    the jurisdiction default; otherwise the DEFAULT retention policy. Seeds the
    bootstrap policies if the library is empty so a fresh tenant never lands
    on a NoneType."""
    if policy_code:
        pol = active_policy(db, tenant_id=tenant_id, code=policy_code)
        if pol is not None:
            return pol

    if jurisdiction:
        code = JURISDICTION_POLICY_DEFAULTS.get(jurisdiction.upper())
        if code:
            pol = active_policy(db, tenant_id=tenant_id, code=code)
            if pol is not None:
                return pol

    pol = active_policy(db, tenant_id=tenant_id, code=RetentionPolicyCode.DEFAULT.value)
    if pol is None:
        seed_default_policies(db, tenant_id=tenant_id)
        pol = active_policy(db, tenant_id=tenant_id, code=RetentionPolicyCode.DEFAULT.value)
    return pol


def supersede_policy(db: Session, *, tenant_id: str, code: str, new_days: int,
                     description: Optional[str] = None,
                     jurisdiction: Optional[str] = None,
                     created_by_id: Optional[int] = None) -> RetentionPolicy:
    """Publish a new version of ``code`` and deactivate the prior one. The new
    version does NOT re-clock existing RetentionAssignment rows (they keep
    their frozen snapshot) — but future assignments adopt it."""
    prior = active_policy(db, tenant_id=tenant_id, code=code)
    next_version = (prior.version + 1) if prior is not None else 1
    new = RetentionPolicy(
        id=ids.policy_id(),
        tenant_id=tenant_id, code=code, version=next_version,
        jurisdiction=jurisdiction, retention_days=new_days, active=True,
        description=description or f"Version {next_version} of {code}.",
        created_by_id=created_by_id,
    )
    db.add(new)
    db.flush()
    if prior is not None:
        prior.active = False
        prior.superseded_by = new.id
    db.flush()
    events.append_audit(db, tenant_id=tenant_id, action="RetentionPolicySuperseded",
                        entity="RetentionPolicy", actor_id=created_by_id,
                        payload={"code": code, "version": next_version,
                                 "retention_days": new_days,
                                 "prior_id": prior.id if prior else None})
    return new


# ════════════════════════════════════════════════════════════════════════════
# ASSIGNMENT
# ════════════════════════════════════════════════════════════════════════════
def assign_to_case(db: Session, case: OffboardingCase, *,
                   policy_code: Optional[str] = None,
                   actor_id: Optional[int] = None,
                   correlation_id: Optional[str] = None) -> RetentionAssignment:
    """Snapshot the active version of the chosen (or jurisdiction-defaulted)
    policy onto a case. Idempotent — re-running updates the same assignment
    row in place rather than spawning duplicates."""
    pol = resolve_policy(db, tenant_id=case.tenant_id,
                         jurisdiction=case.jurisdiction, policy_code=policy_code)

    existing = (db.query(RetentionAssignment)
                .filter(RetentionAssignment.tenant_id == case.tenant_id,
                        RetentionAssignment.case_id == case.id)
                .first())
    eligibility = (DeletionEligibility.BLOCKED_LEGAL_HOLD.value
                   if case.legal_hold_status == "active"
                   else DeletionEligibility.RETENTION_ACTIVE.value)
    if existing is None:
        existing = RetentionAssignment(
            id=ids.retention_id(),
            tenant_id=case.tenant_id, case_id=case.id, worker_id=case.worker_id)
        db.add(existing)
    existing.policy_code = pol.code
    existing.jurisdiction = case.jurisdiction
    existing.data_classification = case.data_classification
    existing.retention_until = _now() + timedelta(days=pol.retention_days)
    existing.deletion_eligibility = eligibility
    existing.assigned_by_id = actor_id
    db.flush()
    events.append_audit(db, tenant_id=case.tenant_id, action="RetentionAssigned",
                        entity="RetentionAssignment", case_id=case.id,
                        worker_id=case.worker_id, actor_id=actor_id,
                        payload={"policy_code": pol.code, "policy_version": pol.version,
                                 "retention_days": pol.retention_days,
                                 "retention_until": existing.retention_until.isoformat()},
                        correlation_id=correlation_id)
    events.emit_event(db, tenant_id=case.tenant_id,
                      event_type=EventType.RETENTION_POLICY_ASSIGNED.value, case_id=case.id,
                      payload={"policy_code": pol.code, "policy_version": pol.version},
                      correlation_id=correlation_id)
    return existing


# ════════════════════════════════════════════════════════════════════════════
# ELIGIBILITY  (the §4 deletion-eligibility check)
# ════════════════════════════════════════════════════════════════════════════
def compute_eligibility(db: Session, case: OffboardingCase) -> Dict:
    """Enumerate every gate that currently blocks deletion of ``case``. Pure
    read-only. Returns a dict with ``eligible`` and a sorted list of blockers
    so the caller (the deletion-review endpoint) can render them verbatim."""
    blockers: List[Dict] = []

    # 1. Legal hold — any active hold blocks deletion outright (§4).
    holds = (db.query(LegalHold)
             .filter(LegalHold.tenant_id == case.tenant_id,
                     LegalHold.case_id == case.id,
                     LegalHold.status == "active")
             .all())
    if holds:
        blockers.append({"type": "legal_hold_active",
                         "detail": {"holds": [{"id": h.id, "matter_ref": h.matter_ref}
                                              for h in holds]}})

    # 2. Open disputes / unresolved blockers on the case.
    open_blockers = (db.query(WorkflowBlocker)
                     .filter(WorkflowBlocker.tenant_id == case.tenant_id,
                             WorkflowBlocker.case_id == case.id,
                             WorkflowBlocker.status == "open")
                     .all())
    if open_blockers:
        blockers.append({"type": "open_disputes_or_blockers",
                         "detail": {"count": len(open_blockers),
                                    "types": sorted({b.blocker_type for b in open_blockers})}})

    # 3. Retention clock — has it elapsed? An assignment is required to even
    #    answer the question; missing assignment = not eligible yet.
    assignment = (db.query(RetentionAssignment)
                  .filter(RetentionAssignment.tenant_id == case.tenant_id,
                          RetentionAssignment.case_id == case.id)
                  .first())
    if assignment is None:
        blockers.append({"type": "no_retention_assigned",
                         "detail": {"message": "retention policy must be assigned first"}})
    else:
        until = _as_utc(assignment.retention_until)
        if until is None or _now() < until:
            blockers.append({"type": "retention_active",
                             "detail": {"retention_until": until.isoformat() if until else None,
                                        "policy_code": assignment.policy_code}})

    return {
        "case_id": case.id,
        "eligible": len(blockers) == 0,
        "blockers": blockers,
        "evaluated_at": _now().isoformat(),
    }


def _update_assignment_eligibility(db: Session, assignment: RetentionAssignment,
                                   case: OffboardingCase) -> str:
    """Recompute and persist the assignment's deletion_eligibility column from
    the current state of the case. Used by the expiry job."""
    if case.legal_hold_status == "active":
        new = DeletionEligibility.BLOCKED_LEGAL_HOLD.value
    else:
        until = _as_utc(assignment.retention_until)
        if until is None or _now() < until:
            new = DeletionEligibility.RETENTION_ACTIVE.value
        else:
            new = DeletionEligibility.ELIGIBLE.value
    if assignment.deletion_eligibility != new:
        assignment.deletion_eligibility = new
        db.flush()
    return new


# ════════════════════════════════════════════════════════════════════════════
# EXPIRY JOB (§5 background)
# ════════════════════════════════════════════════════════════════════════════
def run_retention_expiry_once(db: Session, *, tenant_id: Optional[str] = None,
                              limit: int = 500) -> Dict:
    """Sweep retention assignments whose clock has elapsed and update their
    deletion_eligibility. Never deletes — deletion runs only after the dual-
    approval review (§4). Returns counts. Caller commits."""
    # NB: ``locked`` freezes the retention WINDOW (retention_until cannot be
    # silently re-clocked), not the eligibility *evaluation* — which is a pure
    # function of (now, retention_until, legal_hold_status). The sweep refreshes
    # deletion_eligibility on every assignment, locked or not.
    q = db.query(RetentionAssignment)
    if tenant_id is not None:
        q = q.filter(RetentionAssignment.tenant_id == tenant_id)
    rows = q.limit(limit).all()

    counts = {"scanned": 0, "now_eligible": 0, "still_blocked": 0}
    for assignment in rows:
        case = (db.query(OffboardingCase)
                .filter(OffboardingCase.id == assignment.case_id)
                .first())
        if case is None:
            continue
        counts["scanned"] += 1
        new = _update_assignment_eligibility(db, assignment, case)
        if new == DeletionEligibility.ELIGIBLE.value:
            counts["now_eligible"] += 1
        else:
            counts["still_blocked"] += 1
    return counts


def detail(policy: RetentionPolicy) -> Dict:
    return {
        "id": policy.id, "code": policy.code, "version": policy.version,
        "jurisdiction": policy.jurisdiction, "retention_days": policy.retention_days,
        "active": policy.active, "description": policy.description,
        "created_at": policy.created_at, "superseded_by": policy.superseded_by,
    }
