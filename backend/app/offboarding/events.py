"""Event plumbing for the offboarding domain (§6).

Two append-only streams, written transactionally alongside state changes:

  • DomainEvent  — the business event log + transactional outbox. Every event
    carries correlation_id (ties one user action's whole fan-out together),
    causation_id (the event that directly caused this one), and an
    idempotency_key (a UNIQUE column → re-emitting the same logical event is a
    no-op, which is what makes replays safe). Status moves pending → published.

  • AuditEvent   — the tamper-evident audit trail. Hash-chained per
    (tenant_id, case_id): each row's current_hash folds in the previous row's
    hash, so altering or deleting any historical row breaks every hash after it
    (the §1 "append-only / immutable" requirement). Mirrors the existing
    WorkEvidenceChain design.

Nothing here commits — callers own the transaction so the state mutation, the
domain event, and the audit row all land atomically (or all roll back).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from . import ids
from .constants import SOURCE_SERVICE
from .models import AuditEvent, DomainEvent

_log = logging.getLogger("zoiko.offboarding.events")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(obj: Any) -> str:
    """Stable JSON for hashing — sorted keys, str() fallback for datetimes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ════════════════════════════════════════════════════════════════════════════
# DOMAIN EVENTS  (idempotent, replay-safe outbox)
# ════════════════════════════════════════════════════════════════════════════
def emit_event(
    db: Session,
    *,
    tenant_id: str,
    event_type: str,
    aggregate_id: Optional[str] = None,
    case_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    state_from: Optional[str] = None,
    state_to: Optional[str] = None,
) -> DomainEvent:
    """Append one domain event. Idempotent on ``idempotency_key``.

    If an event with the same idempotency_key already exists we return it
    untouched — so a retried request, a redelivered queue message, or a replayed
    command all converge on exactly one stored event.
    """
    key = idempotency_key or ids.idempotency_key()

    existing = (
        db.query(DomainEvent)
        .filter(DomainEvent.idempotency_key == key)
        .first()
    )
    if existing is not None:
        return existing  # replay / retry — drop the duplicate

    row = DomainEvent(
        id=ids.event_id(),
        tenant_id=tenant_id,
        case_id=case_id,
        aggregate_id=aggregate_id or case_id,
        event_type=event_type,
        payload=payload or {},
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=key,
        state_from=state_from,
        state_to=state_to,
        status="pending",
        occurred_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


# ── In-process handler registry (synchronous, replay-safe) ───────────────────
# Lets one event trigger downstream side-effects without coupling services to
# each other. Handlers MUST be idempotent (they may run again on replay).
_HANDLERS: Dict[str, List[Callable[[Session, DomainEvent], None]]] = {}


def register_handler(event_type: str, fn: Callable[[Session, DomainEvent], None]) -> None:
    _HANDLERS.setdefault(event_type, []).append(fn)


def dispatch_pending(db: Session, *, tenant_id: Optional[str] = None, limit: int = 200) -> int:
    """Publish pending domain events: run handlers, mark published. Returns the
    number published. Safe to call repeatedly — already-published rows are
    skipped, and handlers are expected to be idempotent."""
    q = db.query(DomainEvent).filter(DomainEvent.status == "pending")
    if tenant_id is not None:
        q = q.filter(DomainEvent.tenant_id == tenant_id)
    rows = q.order_by(DomainEvent.occurred_at.asc()).limit(limit).all()

    published = 0
    for row in rows:
        try:
            for fn in _HANDLERS.get(row.event_type, []):
                fn(db, row)
            row.status = "published"
            row.published_at = _now()
            published += 1
        except Exception as exc:  # never let one bad handler stall the stream
            row.attempts = (row.attempts or 0) + 1
            row.error = str(exc)[:500]
            if row.attempts >= 5:
                row.status = "failed"
            _log.exception("offboarding event handler failed: %s", row.event_type)
    db.flush()
    return published


# ════════════════════════════════════════════════════════════════════════════
# AUDIT EVENTS  (append-only, hash-chained)
# ════════════════════════════════════════════════════════════════════════════
def _chain_body(*, tenant_id, case_id, worker_id, actor_id, action, entity, payload, seq) -> str:
    """The canonical, round-trip-stable body folded into a chain hash. Uses the
    monotonic ``seq`` (not a timestamp) so it survives SQLite's tz-lossy
    datetime storage identically to Postgres."""
    return _canonical({
        "tenant_id": tenant_id,
        "case_id": case_id,
        "worker_id": worker_id,
        "actor_id": actor_id,
        "action": action,
        "entity": entity,
        "payload": payload or {},
        "seq": seq,
    })


def _last_link(db: Session, tenant_id: str, case_id: Optional[str]):
    """Return (last_hash, last_seq) for the (tenant, case) chain, or (None, 0)."""
    q = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant_id)
    # Chain per case; case-less audit rows (e.g. cross-tenant access attempts)
    # chain on the tenant-wide null-case stream.
    q = q.filter(AuditEvent.case_id == case_id) if case_id else q.filter(AuditEvent.case_id.is_(None))
    last = q.order_by(AuditEvent.seq.desc()).first()
    if last is None:
        return None, 0
    return last.current_hash, last.seq


def append_audit(
    db: Session,
    *,
    tenant_id: str,
    action: str,
    entity: str,
    case_id: Optional[str] = None,
    worker_id: Optional[int] = None,
    engagement_id: Optional[str] = None,
    actor_id: Optional[int] = None,
    actor_role: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    causation_id: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> AuditEvent:
    """Append one immutable, hash-chained audit row. Never updates or deletes."""
    prev, last_seq = _last_link(db, tenant_id, case_id)
    seq = last_seq + 1
    body = _chain_body(tenant_id=tenant_id, case_id=case_id, worker_id=worker_id,
                       actor_id=actor_id, action=action, entity=entity,
                       payload=payload or {}, seq=seq)
    current = hashlib.sha256(f"{prev or ''}|{body}".encode("utf-8")).hexdigest()

    row = AuditEvent(
        id=ids.audit_id(),
        tenant_id=tenant_id,
        case_id=case_id,
        worker_id=worker_id,
        engagement_id=engagement_id,
        seq=seq,
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        entity=entity,
        payload=payload or {},
        prev_hash=prev,
        current_hash=current,
        correlation_id=correlation_id,
        causation_id=causation_id,
        ip_address=ip_address,
        source_service=SOURCE_SERVICE,
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def verify_chain(db: Session, tenant_id: str, case_id: Optional[str]) -> bool:
    """Re-walk the audit chain for a case and confirm every link is intact.
    Returns False at the first broken hash. Used by integrity checks / tests."""
    q = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant_id)
    q = q.filter(AuditEvent.case_id == case_id) if case_id else q.filter(AuditEvent.case_id.is_(None))
    rows = q.order_by(AuditEvent.seq.asc()).all()

    prev = None
    for row in rows:
        body = _chain_body(tenant_id=row.tenant_id, case_id=row.case_id, worker_id=row.worker_id,
                           actor_id=row.actor_id, action=row.action, entity=row.entity,
                           payload=row.payload or {}, seq=row.seq)
        expected = hashlib.sha256(f"{prev or ''}|{body}".encode("utf-8")).hexdigest()
        if row.prev_hash != prev or row.current_hash != expected:
            return False
        prev = row.current_hash
    return True
