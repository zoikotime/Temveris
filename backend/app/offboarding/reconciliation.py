"""SSO + SCIM identity reconciliation engine (§4).

The lifecycle revokes Temveris-local access synchronously. External identity
surfaces (SSO, SCIM, CRM, billing, generic integrations) can't
always be torn down in the request that closes a case — the IdP may be slow,
rate-limited, or briefly unreachable. Those revocations are enqueued as
``IntegrationReconciliationItem`` rows and driven to completion *here*, with:

  • bounded retries + exponential backoff, then a **dead-letter** state that
    stops silently retrying forever and raises a HIGH incident for a human;
  • a **conflict detector** for the five drift scenarios the spec calls out:
        - IdP active but Temveris revoked   (couldn't tear the IdP grant down)
        - SCIM deprovision failure
        - stale SSO state                (closure done, SSO never reconciled)
        - multiple identities            (duplicate active accounts for a worker)
        - rehire conflict                (reactivated while an exit case is closed)
  • a **blocker** on the owning case for every dead-letter / conflict, so a case
    can never reach COMPLETED while its external identity is still drifting;
  • an append-only **audit chain** entry + domain event for every transition.

External calls go through a small adapter registry. In a deployment with no
configured IdP/SCIM the queue is a no-op (nothing to revoke externally) — the
engine still runs, just with nothing to do. Real integrations register an
adapter via ``register_adapter`` and may raise to signal a retryable failure.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from . import events, ids, incidents, workflow
from .constants import (
    RECONCILIATION_BACKOFF_BASE_SECONDS, RECONCILIATION_BACKOFF_CAP_SECONDS,
    RECONCILIATION_MAX_ATTEMPTS, AlertType, CaseState, EventType,
    IdentityConflictType, ReconciliationStatus, RevocationStatus,
)
from .models import (
    DEFAULT_TENANT, AccessRevocationRecord, IntegrationReconciliationItem,
    OffboardingCase,
)
from .repository import Repository

_log = logging.getLogger("zoiko.offboarding.reconciliation")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ════════════════════════════════════════════════════════════════════════════
# ADAPTER REGISTRY  (external identity systems plug in here)
# ════════════════════════════════════════════════════════════════════════════
# integration name → callable(item) -> evidence dict. The callable revokes the
# worker on that external system and returns evidence; it raises to signal a
# RETRYABLE failure (network, 5xx, rate limit). Unregistered integrations are a
# no-op: this deployment has nothing to revoke there.
_ADAPTERS: Dict[str, Callable[[IntegrationReconciliationItem], dict]] = {}


def register_adapter(integration: str, fn: Callable[[IntegrationReconciliationItem], dict]) -> None:
    _ADAPTERS[integration] = fn


def clear_adapters() -> None:
    """Test seam — drop all registered adapters."""
    _ADAPTERS.clear()


def _backoff_seconds(attempts: int) -> int:
    delay = RECONCILIATION_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return min(delay, RECONCILIATION_BACKOFF_CAP_SECONDS)


# ════════════════════════════════════════════════════════════════════════════
# ENQUEUE
# ════════════════════════════════════════════════════════════════════════════
def enqueue(db: Session, *, case: OffboardingCase, integration: str,
            component: Optional[str] = None, operation: str = "revoke",
            payload: Optional[dict] = None,
            correlation_id: Optional[str] = None) -> IntegrationReconciliationItem:
    """Add a reconciliation item (idempotent per case+integration+operation:
    an existing non-terminal item is returned rather than duplicated)."""
    existing = (db.query(IntegrationReconciliationItem)
                .filter(IntegrationReconciliationItem.case_id == case.id,
                        IntegrationReconciliationItem.integration == integration,
                        IntegrationReconciliationItem.operation == operation,
                        IntegrationReconciliationItem.status.in_([
                            ReconciliationStatus.PENDING.value,
                            ReconciliationStatus.IN_PROGRESS.value,
                            ReconciliationStatus.FAILED.value]))
                .first())
    if existing is not None:
        return existing
    item = IntegrationReconciliationItem(
        id=ids.reconciliation_id(),
        tenant_id=case.tenant_id,
        case_id=case.id,
        worker_id=case.worker_id,
        integration=integration,
        component=component,
        operation=operation,
        status=ReconciliationStatus.PENDING.value,
        attempts=0,
        max_attempts=RECONCILIATION_MAX_ATTEMPTS,
        next_attempt_at=_now(),
        payload=payload or {},
        correlation_id=correlation_id,
    )
    db.add(item)
    db.flush()
    events.emit_event(db, tenant_id=case.tenant_id,
                      event_type=EventType.RECONCILIATION_ENQUEUED.value, case_id=case.id,
                      payload={"integration": integration, "operation": operation},
                      correlation_id=correlation_id)
    return item


# ════════════════════════════════════════════════════════════════════════════
# QUEUE PROCESSING  (retry + dead-letter)
# ════════════════════════════════════════════════════════════════════════════
def process_item(db: Session, item: IntegrationReconciliationItem, *,
                 correlation_id: Optional[str] = None) -> str:
    """Attempt one reconciliation item exactly once. Idempotent: an item already
    in a terminal state (succeeded / dead_letter) is left untouched. Returns the
    resulting status."""
    if item.status in (ReconciliationStatus.SUCCEEDED.value,
                       ReconciliationStatus.DEAD_LETTER.value):
        return item.status

    corr = correlation_id or item.correlation_id
    item.status = ReconciliationStatus.IN_PROGRESS.value
    item.attempts = (item.attempts or 0) + 1
    adapter = _ADAPTERS.get(item.integration)

    try:
        evidence = adapter(item) if adapter is not None else {"noop": "integration_not_configured"}
        item.status = ReconciliationStatus.SUCCEEDED.value
        item.last_error = None
        item.next_attempt_at = None
        item.payload = {**(item.payload or {}), "evidence": evidence}
        db.flush()
        _mark_component_revoked(db, item)
        events.append_audit(db, tenant_id=item.tenant_id, action="ReconciliationSucceeded",
                            entity="IntegrationReconciliationItem", case_id=item.case_id,
                            worker_id=item.worker_id,
                            payload={"integration": item.integration, "attempts": item.attempts},
                            correlation_id=corr)
        events.emit_event(db, tenant_id=item.tenant_id,
                         event_type=EventType.RECONCILIATION_SUCCEEDED.value,
                         case_id=item.case_id,
                         payload={"integration": item.integration}, correlation_id=corr)
        return item.status
    except Exception as exc:  # noqa: BLE001 — retryable failure path
        item.last_error = str(exc)[:300]
        if item.attempts >= (item.max_attempts or RECONCILIATION_MAX_ATTEMPTS):
            item.status = ReconciliationStatus.DEAD_LETTER.value
            item.next_attempt_at = None
            db.flush()
            _on_dead_letter(db, item, corr)
        else:
            item.status = ReconciliationStatus.FAILED.value
            item.next_attempt_at = _now() + timedelta(seconds=_backoff_seconds(item.attempts))
            db.flush()
            events.append_audit(db, tenant_id=item.tenant_id, action="ReconciliationRetryScheduled",
                                entity="IntegrationReconciliationItem", case_id=item.case_id,
                                worker_id=item.worker_id,
                                payload={"integration": item.integration, "attempts": item.attempts,
                                         "next_attempt_at": item.next_attempt_at.isoformat()},
                                correlation_id=corr)
        _log.warning("reconciliation %s failed (attempt %s/%s): %s", item.integration,
                     item.attempts, item.max_attempts, item.last_error)
        return item.status


def _mark_component_revoked(db: Session, item: IntegrationReconciliationItem) -> None:
    """When an external revoke finally lands, flip the matching access-revocation
    record to REVOKED so the readiness gate can see the surface is closed."""
    if not item.component:
        return
    rec = (db.query(AccessRevocationRecord)
           .filter(AccessRevocationRecord.case_id == item.case_id,
                   AccessRevocationRecord.component == item.component)
           .first())
    if rec is not None and rec.status != RevocationStatus.REVOKED.value:
        rec.status = RevocationStatus.REVOKED.value
        rec.revoked_at = _now()
        rec.evidence = {**(rec.evidence or {}), "reconciled": True, "integration": item.integration}
        db.flush()


def _on_dead_letter(db: Session, item: IntegrationReconciliationItem, corr: Optional[str]) -> None:
    """A reconciliation item exhausted its retries — raise a HIGH incident and a
    case blocker so completion stays gated until a human intervenes."""
    incidents.open_incident(
        db, tenant_id=item.tenant_id, incident_type=AlertType.RECONCILIATION_CONFLICT.value,
        title=f"Reconciliation dead-letter: {item.integration} for case {item.case_id}",
        worker_id=item.worker_id, case_id=item.case_id,
        diagnostics={"integration": item.integration, "operation": item.operation,
                     "attempts": item.attempts, "last_error": item.last_error},
        correlation_id=corr,
    )
    case = Repository(db, item.tenant_id).get_case(item.case_id) if item.case_id else None
    if case is not None and not _has_open_blocker(db, case.id, f"reconciliation_dead_letter:{item.integration}"):
        workflow.raise_blocker(Repository(db, item.tenant_id), case,
                               blocker_type=f"reconciliation_dead_letter:{item.integration}",
                               reason=f"{item.integration} deprovision failed after "
                                      f"{item.attempts} attempts",
                               severity="high")
    events.append_audit(db, tenant_id=item.tenant_id, action="ReconciliationDeadLettered",
                        entity="IntegrationReconciliationItem", case_id=item.case_id,
                        worker_id=item.worker_id,
                        payload={"integration": item.integration, "attempts": item.attempts},
                        correlation_id=corr)
    events.emit_event(db, tenant_id=item.tenant_id,
                     event_type=EventType.RECONCILIATION_DEAD_LETTER.value, case_id=item.case_id,
                     payload={"integration": item.integration}, correlation_id=corr)


def _has_open_blocker(db: Session, case_id: str, blocker_type: str) -> bool:
    from .models import WorkflowBlocker
    return (db.query(WorkflowBlocker)
            .filter(WorkflowBlocker.case_id == case_id,
                    WorkflowBlocker.blocker_type == blocker_type,
                    WorkflowBlocker.status == "open")
            .first() is not None)


def process_queue(db: Session, *, tenant_id: Optional[str] = None, limit: int = 100,
                  now: Optional[datetime] = None) -> dict:
    """Process all due reconciliation items (pending, or failed with a
    next_attempt_at that has elapsed). Returns per-status counts. Safe to call
    repeatedly — terminal items are skipped."""
    now = now or _now()
    q = (db.query(IntegrationReconciliationItem)
         .filter(IntegrationReconciliationItem.status.in_([
             ReconciliationStatus.PENDING.value, ReconciliationStatus.FAILED.value])))
    if tenant_id is not None:
        q = q.filter(IntegrationReconciliationItem.tenant_id == tenant_id)
    items = q.order_by(IntegrationReconciliationItem.created_at.asc()).limit(limit).all()

    counts = {"processed": 0, "succeeded": 0, "failed": 0, "dead_letter": 0, "skipped": 0}
    for item in items:
        due = item.next_attempt_at is None or now >= _as_utc(item.next_attempt_at)
        if not due:
            counts["skipped"] += 1
            continue
        status = process_item(db, item)
        counts["processed"] += 1
        if status == ReconciliationStatus.SUCCEEDED.value:
            counts["succeeded"] += 1
        elif status == ReconciliationStatus.DEAD_LETTER.value:
            counts["dead_letter"] += 1
        elif status == ReconciliationStatus.FAILED.value:
            counts["failed"] += 1
    return counts


# ════════════════════════════════════════════════════════════════════════════
# CONFLICT DETECTION  (the five drift scenarios)
# ════════════════════════════════════════════════════════════════════════════
def detect_conflicts_for_case(db: Session, case: OffboardingCase, *,
                              correlation_id: Optional[str] = None) -> List[str]:
    """Evaluate the identity-drift scenarios for one case and open an incident
    (+ case blocker) for each that fires. Returns the conflict types found.
    Idempotent — incident dedupe + blocker dedupe keep re-runs quiet."""
    found: List[str] = []
    tenant = case.tenant_id
    repo = Repository(db, tenant)
    recon_items = repo.reconciliation_for_case(case.id)
    case_closed = case.status in (CaseState.COMPLETED.value,) or case.status in _closed_set()

    # 1/3. Stale SSO / IdP-active: the case has closed access but an SSO/SCIM
    #      item is still unresolved (pending/failed) or dead-lettered.
    for item in recon_items:
        if item.integration in ("sso", "scim") and case_closed:
            if item.status in (ReconciliationStatus.PENDING.value,
                               ReconciliationStatus.FAILED.value,
                               ReconciliationStatus.IN_PROGRESS.value):
                ctype = (IdentityConflictType.STALE_SSO_STATE.value if item.integration == "sso"
                         else IdentityConflictType.SCIM_FAILURE.value)
                _raise_conflict(db, case, ctype,
                                detail={"integration": item.integration, "status": item.status},
                                correlation_id=correlation_id)
                found.append(ctype)
            elif item.status == ReconciliationStatus.DEAD_LETTER.value:
                ctype = (IdentityConflictType.IDP_ACTIVE_ZOIKO_REVOKED.value
                         if item.integration == "sso"
                         else IdentityConflictType.SCIM_FAILURE.value)
                _raise_conflict(db, case, ctype,
                                detail={"integration": item.integration, "status": item.status},
                                correlation_id=correlation_id)
                found.append(ctype)

    # 4. Multiple identities: more than one active user account shares this
    #    worker's email (a duplicate that survived deprovisioning).
    if case.worker_id is not None:
        dupes = _duplicate_active_accounts(db, case.worker_id)
        if dupes:
            _raise_conflict(db, case, IdentityConflictType.MULTIPLE_IDENTITIES.value,
                            detail={"duplicate_user_ids": dupes},
                            correlation_id=correlation_id)
            found.append(IdentityConflictType.MULTIPLE_IDENTITIES.value)

    # 5. Rehire conflict: a worker whose exit case is closed has been
    #    reactivated (login re-enabled) without a case reversal.
    if case.worker_id is not None and case_closed:
        from database.models import User
        user = db.query(User).filter(User.id == case.worker_id).first()
        if user is not None and getattr(user, "is_active", False):
            _raise_conflict(db, case, IdentityConflictType.REHIRE_CONFLICT.value,
                            detail={"worker_id": case.worker_id, "is_active": True},
                            correlation_id=correlation_id)
            found.append(IdentityConflictType.REHIRE_CONFLICT.value)

    return found


def _closed_set() -> frozenset:
    from .constants import ACCESS_CLOSED_STATES
    return ACCESS_CLOSED_STATES


def _duplicate_active_accounts(db: Session, worker_id: int) -> List[int]:
    """Other active User rows sharing this worker's email — a duplicate identity
    that would survive the single-account deprovision."""
    from database.models import User
    me = db.query(User).filter(User.id == worker_id).first()
    if me is None or not getattr(me, "email", None):
        return []
    rows = (db.query(User)
            .filter(User.email == me.email, User.id != worker_id,
                    User.is_active.is_(True))
            .all())
    return [r.id for r in rows]


def _raise_conflict(db: Session, case: OffboardingCase, conflict_type: str, *,
                    detail: dict, correlation_id: Optional[str] = None) -> None:
    incidents.open_incident(
        db, tenant_id=case.tenant_id, incident_type=conflict_type,
        title=f"Identity conflict ({conflict_type}) on case {case.id}",
        worker_id=case.worker_id, case_id=case.id, diagnostics=detail,
        correlation_id=correlation_id,
    )
    blocker_type = f"identity_conflict:{conflict_type}"
    if not _has_open_blocker(db, case.id, blocker_type):
        workflow.raise_blocker(Repository(db, case.tenant_id), case,
                               blocker_type=blocker_type,
                               reason=f"identity reconciliation conflict: {conflict_type}",
                               severity="high")
    events.append_audit(db, tenant_id=case.tenant_id, action="IdentityConflictDetected",
                        entity="OffboardingCase", case_id=case.id, worker_id=case.worker_id,
                        payload={"conflict_type": conflict_type, **detail},
                        correlation_id=correlation_id)
    events.emit_event(db, tenant_id=case.tenant_id,
                     event_type=EventType.IDENTITY_CONFLICT_DETECTED.value, case_id=case.id,
                     payload={"conflict_type": conflict_type}, correlation_id=correlation_id)


def detect_conflicts(db: Session, *, tenant_id: Optional[str] = None, limit: int = 200) -> dict:
    """Sweep cases that have begun/finished access closure and run the conflict
    detector on each. Returns counts. Used by the background job (§5)."""
    from .constants import ACCESS_CLOSED_STATES
    q = db.query(OffboardingCase).filter(
        OffboardingCase.status.in_(list(ACCESS_CLOSED_STATES)))
    if tenant_id is not None:
        q = q.filter(OffboardingCase.tenant_id == tenant_id)
    cases = q.order_by(OffboardingCase.updated_at.desc()).limit(limit).all()
    conflicts = 0
    for case in cases:
        conflicts += len(detect_conflicts_for_case(db, case))
    return {"cases_scanned": len(cases), "conflicts": conflicts}
