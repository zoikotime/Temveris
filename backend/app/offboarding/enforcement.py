"""Server-side access & telemetry enforcement gate (§2, §3).

This is the single authority that answers, at request time and from
*server-side lifecycle state alone*, two questions about a worker:

    • may this worker hold/exercise access right now?   (login / API / device)
    • may this worker's telemetry be accepted right now? (screenshots / activity
      / idle / url / productivity / offline backfill)

The doctrine it enforces — verbatim from the spec — is **zero post-exit access**
and **zero post-exit tracking**, decided here and never delegated to a client
logout. A desktop or mobile agent that keeps a stale token (or replays an
offline queue) is rejected because the *server* knows the worker's case has
passed access closure / the final-access cutover, not because the client
behaved.

Decision model
--------------
A worker is BLOCKED if any of their offboarding cases is either:
  • in an access-closed state (``ACCESS_CLOSED_STATES``) — closure has run; or
  • past its ``final_access_at`` cutover while still pre-cutover
    (``PENDING_CUTOVER_STATES``) — the instant elapsed even if the closure job
    is late or the agent crashed mid-exit.
Reversed / cancelled cases never block: reversal restores access.

When telemetry is blocked, the payload METADATA (never the bytes, never a
credential) is parked in ``offboarding_telemetry_quarantine`` and a CRITICAL
incident is opened — append-only proof that the stop was enforced server-side.

The gate is intentionally framework-free: it takes a plain ``Session`` and
returns a decision. Callers (the ingest endpoints, the auth gate, the
background jobs) decide how to react (403, quarantine, sweep).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from . import events, ids, incidents
from .constants import (
    ACCESS_CLOSED_STATES, PENDING_CUTOVER_STATES, AlertType, CaseState,
    EventType, QuarantineReason, QuarantineStatus, TelemetryKind,
)
from .models import DEFAULT_TENANT, OffboardingCase, TelemetryQuarantineRecord

_log = logging.getLogger("zoiko.offboarding.enforcement")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# Cases in these states are closed/inert and never constrain access.
_INERT_STATES = frozenset({
    CaseState.DRAFT.value, CaseState.PENDING_APPROVAL.value,
    CaseState.REVERSED.value, CaseState.CANCELLED.value,
})


@dataclass
class AccessDecision:
    """Outcome of an access/telemetry check for a worker."""
    allowed: bool
    reason: str = "no_active_offboarding"
    case_id: Optional[str] = None
    tenant_id: Optional[str] = None
    state: Optional[str] = None
    final_access_at: Optional[datetime] = None
    blocking_cases: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "case_id": self.case_id,
            "state": self.state,
            "final_access_at": self.final_access_at.isoformat() if self.final_access_at else None,
            "blocking_cases": self.blocking_cases,
        }


# ════════════════════════════════════════════════════════════════════════════
# DECISION  (pure, read-only)
# ════════════════════════════════════════════════════════════════════════════
def evaluate_worker(db: Session, worker_id: Optional[int], *,
                    tenant_id: Optional[str] = None) -> AccessDecision:
    """Most-restrictive access decision for ``worker_id`` across all their
    cases. Pure and read-only — never writes. ``tenant_id`` scopes the lookup
    (omit for the single-tenant 'default' deployment; cross-tenant rows are
    simply not considered)."""
    if worker_id is None:
        return AccessDecision(allowed=True)

    q = db.query(OffboardingCase).filter(OffboardingCase.worker_id == worker_id)
    if tenant_id is not None:
        q = q.filter(OffboardingCase.tenant_id == tenant_id)
    cases = q.all()

    now = _now()
    closed_hit: Optional[OffboardingCase] = None
    cutover_hit: Optional[OffboardingCase] = None
    blocking: List[str] = []

    for c in cases:
        if c.status in _INERT_STATES:
            continue
        if c.status in ACCESS_CLOSED_STATES:
            closed_hit = closed_hit or c
            blocking.append(c.id)
        elif c.status in PENDING_CUTOVER_STATES:
            fa = _as_utc(c.final_access_at)
            if fa is not None and now >= fa:
                cutover_hit = cutover_hit or c
                blocking.append(c.id)

    # Access-closed beats cutover (a stronger, already-executed state).
    hit = closed_hit or cutover_hit
    if hit is None:
        return AccessDecision(allowed=True)

    reason = (QuarantineReason.POST_EXIT_ACCESS_CLOSED.value
              if hit is closed_hit
              else QuarantineReason.PAST_FINAL_ACCESS.value)
    return AccessDecision(
        allowed=False, reason=reason, case_id=hit.id, tenant_id=hit.tenant_id,
        state=hit.status, final_access_at=_as_utc(hit.final_access_at),
        blocking_cases=blocking,
    )


def is_login_allowed(db: Session, worker_id: Optional[int], *,
                     tenant_id: Optional[str] = None) -> AccessDecision:
    """Whether the worker may authenticate / hold a live session right now.
    Same lifecycle rule as telemetry — once access is closed, both are denied."""
    return evaluate_worker(db, worker_id, tenant_id=tenant_id)


def is_telemetry_allowed(db: Session, worker_id: Optional[int], *,
                         tenant_id: Optional[str] = None) -> AccessDecision:
    """Whether new tracking telemetry from the worker may be persisted."""
    return evaluate_worker(db, worker_id, tenant_id=tenant_id)


# ════════════════════════════════════════════════════════════════════════════
# QUARANTINE  (side-effecting — records the blocked telemetry + opens incident)
# ════════════════════════════════════════════════════════════════════════════
def record_quarantine(
    db: Session,
    *,
    worker_id: Optional[int],
    telemetry_kind: str,
    decision: AccessDecision,
    payload_meta: Optional[dict] = None,
    source_ip: Optional[str] = None,
    client_timestamp: Optional[datetime] = None,
    correlation_id: Optional[str] = None,
) -> TelemetryQuarantineRecord:
    """Park a blocked telemetry payload's metadata and open a CRITICAL incident.
    Append-only. Never writes to the live activities/screenshots tables. The
    caller owns the commit (or passes a dedicated session)."""
    tenant_id = decision.tenant_id or DEFAULT_TENANT
    corr = correlation_id or ids.correlation_id()

    # Offline backfill is its own reason: a late upload replayed from the
    # agent's disk queue after the worker had already exited. We do not trust
    # the client timestamp to wave it through.
    reason = decision.reason
    if telemetry_kind == TelemetryKind.OFFLINE_UPLOAD.value:
        reason = QuarantineReason.OFFLINE_BACKFILL.value

    incident = incidents.open_incident(
        db,
        tenant_id=tenant_id,
        incident_type=AlertType.TRACKING_AFTER_OFFBOARDING.value,
        title=f"Post-exit telemetry rejected for worker {worker_id}",
        worker_id=worker_id,
        case_id=decision.case_id,
        diagnostics={
            "telemetry_kind": telemetry_kind,
            "reason": reason,
            "case_state": decision.state,
            "source_ip": source_ip,
            "final_access_at": decision.final_access_at.isoformat()
            if decision.final_access_at else None,
        },
        correlation_id=corr,
    )

    rec = TelemetryQuarantineRecord(
        id=ids.quarantine_id(),
        tenant_id=tenant_id,
        worker_id=worker_id,
        case_id=decision.case_id,
        telemetry_kind=telemetry_kind,
        reason=reason,
        status=QuarantineStatus.QUARANTINED.value,
        payload_meta=_safe_meta(payload_meta),
        client_timestamp=_as_utc(client_timestamp),
        source_ip=source_ip,
        incident_id=incident.id,
    )
    db.add(rec)
    db.flush()

    events.append_audit(
        db, tenant_id=tenant_id, action="PostExitTelemetryQuarantined",
        entity="TelemetryQuarantineRecord", case_id=decision.case_id,
        worker_id=worker_id, payload={"kind": telemetry_kind, "reason": reason,
                                      "quarantine_id": rec.id, "incident": incident.reference},
        correlation_id=corr, ip_address=source_ip,
    )
    events.emit_event(
        db, tenant_id=tenant_id,
        event_type=EventType.POST_EXIT_TELEMETRY_QUARANTINED.value,
        case_id=decision.case_id, payload={"kind": telemetry_kind, "reason": reason,
                                           "quarantine_id": rec.id},
        correlation_id=corr,
    )
    return rec


# Metadata keys we keep on a quarantine row (everything else is dropped — no
# image bytes, no tokens). Counts + app/window context are enough to prove what
# the agent tried to send without becoming a data sink.
_META_ALLOW = frozenset({
    "app_name", "window_title", "mouse_clicks", "key_presses", "idle_seconds",
    "is_active", "url", "domain", "productivity_score", "source", "client_ts",
})


def _safe_meta(meta: Optional[dict]) -> dict:
    if not meta:
        return {}
    out = {}
    for k, v in meta.items():
        if k in _META_ALLOW:
            if isinstance(v, str) and len(v) > 300:
                v = v[:300]
            out[k] = v
    return out


# ════════════════════════════════════════════════════════════════════════════
# INGEST GUARD  (high-level helper for telemetry endpoints)
# ════════════════════════════════════════════════════════════════════════════
def check_and_quarantine_telemetry(
    db: Session,
    *,
    worker_id: Optional[int],
    telemetry_kind: str,
    payload_meta: Optional[dict] = None,
    source_ip: Optional[str] = None,
    client_timestamp: Optional[datetime] = None,
) -> AccessDecision:
    """The one call an ingest endpoint makes. Returns an ``AccessDecision``.

    If allowed, the endpoint proceeds normally. If blocked, the blocked
    payload's metadata is quarantined and a CRITICAL incident opened *on an
    isolated session* (so the evidence persists even though the endpoint will
    reject the request, and so it never entangles the request's own
    transaction). The endpoint then returns 403 — the desktop/mobile agent's
    "next sync" is refused, exactly as §3 requires.
    """
    # Kill-switch (§8): if the gate is disabled during an incident, allow but
    # never silently — the disabled state is observable on /metrics and this is
    # the documented break-glass path. DEFAULT is ON.
    from . import flags
    if not flags.telemetry_gate_enabled():
        _log.warning("telemetry gate DISABLED via flag — allowing worker=%s kind=%s",
                     worker_id, telemetry_kind)
        return AccessDecision(allowed=True, reason="gate_disabled")

    decision = evaluate_worker(db, worker_id)
    if decision.allowed:
        return decision

    # Record on a dedicated session so the rejection (and any rollback of the
    # request transaction) cannot lose the quarantine evidence.
    try:
        from database.database import SessionLocal
        rec_db = SessionLocal()
        try:
            record_quarantine(
                rec_db, worker_id=worker_id, telemetry_kind=telemetry_kind,
                decision=decision, payload_meta=payload_meta, source_ip=source_ip,
                client_timestamp=client_timestamp,
            )
            rec_db.commit()
        finally:
            rec_db.close()
    except Exception:  # never let evidence-recording failure mask the block
        _log.exception("failed to record telemetry quarantine for worker=%s", worker_id)

    return decision
