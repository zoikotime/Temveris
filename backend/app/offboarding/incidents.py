"""Incident & failure management (§6).

A security incident is opened whenever a Tier-0 invariant is violated or a
control fails. The lifecycle is deliberately small and append-friendly:

    open ──escalate──▶ escalated ──resolve──▶ resolved
      └────────────────resolve────────────────▶ resolved

Design rules baked in here:
  • Graded severity (Critical / High / Medium / Low). Critical means an
    invariant is *actively* being violated right now (a former worker is
    getting in, telemetry is being accepted, tenants are leaking) — those are
    flagged for immediate escalation. Lower severities queue for retry/review.
  • Deduplicated. Re-detecting the same condition for the same worker bumps an
    ``occurrences`` counter and refreshes ``last_seen_at`` instead of spamming a
    new row — so a flapping agent can't drown on-call in duplicates.
  • Append-only audit + domain event for every open/escalate/resolve, so the
    incident timeline is itself tamper-evident (reuses the hash-chained audit).
  • SAFE diagnostics only. ``_sanitize`` strips anything that looks like a
    credential or raw payload before it is persisted — incidents are read by a
    wide operator audience, so they must never become a secrets side-channel.

The module exposes plain ``Session``-level functions (callable from the ingest
gate and background jobs, which hold no OffboardingContext) and an
``IncidentService`` wrapper for the request/router path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from . import events, ids
from .constants import (
    CONFLICT_SEVERITY, INCIDENT_SEVERITY, EventType, IncidentSeverity,
    IncidentStatus,
)
from .models import DEFAULT_TENANT, SecurityIncident

_log = logging.getLogger("zoiko.offboarding.incidents")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Keys (case-insensitive substring match) that must never land in an incident's
# diagnostics blob. Diagnostics are operational breadcrumbs, never evidence of
# the secret itself — that stays in the credential store, not the incident log.
_UNSAFE_KEY_HINTS = (
    "token", "password", "secret", "authorization", "auth_header", "cookie",
    "credential", "api_key", "apikey", "private", "image", "bytes", "base64",
    "ssn", "dob",
)
_MAX_DIAG_STR = 500


def _sanitize(diagnostics: Optional[dict]) -> dict:
    """Return a copy of ``diagnostics`` with credential-shaped keys dropped and
    long strings truncated. Non-destructive; safe to call on untrusted input."""
    if not diagnostics:
        return {}
    safe: dict = {}
    for key, value in diagnostics.items():
        lk = str(key).lower()
        if any(h in lk for h in _UNSAFE_KEY_HINTS):
            safe[key] = "[redacted]"
            continue
        if isinstance(value, str) and len(value) > _MAX_DIAG_STR:
            safe[key] = value[:_MAX_DIAG_STR] + "…"
        elif isinstance(value, dict):
            safe[key] = _sanitize(value)
        else:
            safe[key] = value
    return safe


def severity_for(incident_type: str, default: str = IncidentSeverity.HIGH.value) -> str:
    """Map an incident/alert/conflict type to its default severity."""
    if incident_type in INCIDENT_SEVERITY:
        return INCIDENT_SEVERITY[incident_type]
    if incident_type in CONFLICT_SEVERITY:
        return CONFLICT_SEVERITY[incident_type]
    return default


def _find_open(db: Session, tenant_id: str, incident_type: str,
               worker_id: Optional[int]) -> Optional[SecurityIncident]:
    q = (db.query(SecurityIncident)
         .filter(SecurityIncident.tenant_id == tenant_id,
                 SecurityIncident.incident_type == incident_type,
                 SecurityIncident.status.in_(
                     [IncidentStatus.OPEN.value, IncidentStatus.ESCALATED.value])))
    q = q.filter(SecurityIncident.worker_id == worker_id) if worker_id is not None \
        else q.filter(SecurityIncident.worker_id.is_(None))
    return q.order_by(SecurityIncident.created_at.desc()).first()


# ════════════════════════════════════════════════════════════════════════════
# SESSION-LEVEL FUNCTIONS  (no OffboardingContext required)
# ════════════════════════════════════════════════════════════════════════════
def open_incident(
    db: Session,
    *,
    tenant_id: str,
    incident_type: str,
    title: str,
    severity: Optional[str] = None,
    worker_id: Optional[int] = None,
    case_id: Optional[str] = None,
    diagnostics: Optional[dict] = None,
    correlation_id: Optional[str] = None,
    actor_id: Optional[int] = None,
    actor_role: Optional[str] = None,
    auto_escalate: Optional[bool] = None,
) -> SecurityIncident:
    """Open (or dedupe-bump) a security incident. Idempotent for a repeating
    condition: an already-open incident of the same (tenant, type, worker) is
    reused — its ``occurrences`` increments and ``last_seen_at`` refreshes —
    rather than creating a duplicate. Appends audit + emits a domain event.

    ``auto_escalate`` defaults to True for CRITICAL incidents: a Tier-0
    invariant being actively violated should not wait for a human to notice.
    """
    sev = severity or severity_for(incident_type)
    safe_diag = _sanitize(diagnostics)

    existing = _find_open(db, tenant_id, incident_type, worker_id)
    if existing is not None:
        existing.occurrences = (existing.occurrences or 0) + 1
        existing.last_seen_at = _now()
        # Merge fresh breadcrumbs; never downgrade severity.
        merged = dict(existing.diagnostics or {})
        merged.update(safe_diag)
        existing.diagnostics = merged
        if _sev_rank(sev) > _sev_rank(existing.severity):
            existing.severity = sev
        db.flush()
        events.append_audit(
            db, tenant_id=tenant_id, action="SecurityIncidentRecurred",
            entity="SecurityIncident", case_id=case_id, worker_id=worker_id,
            actor_id=actor_id, actor_role=actor_role,
            payload={"incident": existing.reference, "occurrences": existing.occurrences},
            correlation_id=correlation_id,
        )
        return existing

    incident = SecurityIncident(
        id=ids.incident_id(),
        tenant_id=tenant_id or DEFAULT_TENANT,
        reference=ids.incident_reference(),
        case_id=case_id,
        worker_id=worker_id,
        incident_type=incident_type,
        severity=sev,
        status=IncidentStatus.OPEN.value,
        title=title[:200],
        diagnostics=safe_diag,
        occurrences=1,
        correlation_id=correlation_id,
        last_seen_at=_now(),
    )
    db.add(incident)
    db.flush()

    events.append_audit(
        db, tenant_id=tenant_id, action="SecurityIncidentOpened",
        entity="SecurityIncident", case_id=case_id, worker_id=worker_id,
        actor_id=actor_id, actor_role=actor_role,
        payload={"incident": incident.reference, "type": incident_type, "severity": sev},
        correlation_id=correlation_id,
    )
    events.emit_event(
        db, tenant_id=tenant_id, event_type=EventType.SECURITY_INCIDENT_OPENED.value,
        case_id=case_id, payload={"incident": incident.reference, "type": incident_type,
                                  "severity": sev},
        correlation_id=correlation_id,
    )
    _alert(incident)

    should_escalate = auto_escalate if auto_escalate is not None \
        else sev == IncidentSeverity.CRITICAL.value
    if should_escalate:
        escalate_incident(db, incident, reason="auto: critical severity",
                          correlation_id=correlation_id, actor_id=actor_id,
                          actor_role=actor_role)
    return incident


def escalate_incident(db: Session, incident: SecurityIncident, *, reason: str,
                      correlation_id: Optional[str] = None,
                      actor_id: Optional[int] = None,
                      actor_role: Optional[str] = None) -> SecurityIncident:
    if incident.status == IncidentStatus.RESOLVED.value:
        return incident  # don't re-escalate a resolved incident
    incident.status = IncidentStatus.ESCALATED.value
    incident.escalated_at = incident.escalated_at or _now()
    db.flush()
    events.append_audit(
        db, tenant_id=incident.tenant_id, action="SecurityIncidentEscalated",
        entity="SecurityIncident", case_id=incident.case_id, worker_id=incident.worker_id,
        actor_id=actor_id, actor_role=actor_role,
        payload={"incident": incident.reference, "reason": reason},
        correlation_id=correlation_id or incident.correlation_id,
    )
    events.emit_event(
        db, tenant_id=incident.tenant_id,
        event_type=EventType.SECURITY_INCIDENT_ESCALATED.value, case_id=incident.case_id,
        payload={"incident": incident.reference, "reason": reason},
        correlation_id=correlation_id or incident.correlation_id,
    )
    _alert(incident, escalated=True)
    return incident


def resolve_incident(db: Session, incident: SecurityIncident, *, actor_id: Optional[int],
                     note: Optional[str] = None, actor_role: Optional[str] = None,
                     correlation_id: Optional[str] = None) -> SecurityIncident:
    incident.status = IncidentStatus.RESOLVED.value
    incident.resolved_at = _now()
    incident.resolved_by_id = actor_id
    incident.resolution_note = note
    db.flush()
    events.append_audit(
        db, tenant_id=incident.tenant_id, action="SecurityIncidentResolved",
        entity="SecurityIncident", case_id=incident.case_id, worker_id=incident.worker_id,
        actor_id=actor_id, actor_role=actor_role,
        payload={"incident": incident.reference, "note": note},
        correlation_id=correlation_id or incident.correlation_id,
    )
    events.emit_event(
        db, tenant_id=incident.tenant_id,
        event_type=EventType.SECURITY_INCIDENT_RESOLVED.value, case_id=incident.case_id,
        payload={"incident": incident.reference},
        correlation_id=correlation_id or incident.correlation_id,
    )
    return incident


_SEV_RANK = {
    IncidentSeverity.LOW.value: 0,
    IncidentSeverity.MEDIUM.value: 1,
    IncidentSeverity.HIGH.value: 2,
    IncidentSeverity.CRITICAL.value: 3,
}


def _sev_rank(sev: Optional[str]) -> int:
    return _SEV_RANK.get(sev or "", 0)


def _alert(incident: SecurityIncident, *, escalated: bool = False) -> None:
    """Dispatch an alert for the incident. Kept side-effect-light (structured
    log) so opening an incident stays inside the caller's transaction and never
    fails the request. A full deployment routes CRITICAL/ESCALATED here to the
    existing app.notifications / pager transport."""
    level = logging.CRITICAL if incident.severity == IncidentSeverity.CRITICAL.value else logging.WARNING
    _log.log(
        level,
        "[offboarding.incident]%s ref=%s tenant=%s type=%s severity=%s worker=%s case=%s title=%s",
        " ESCALATED" if escalated else "", incident.reference, incident.tenant_id,
        incident.incident_type, incident.severity, incident.worker_id, incident.case_id,
        incident.title,
    )


# ════════════════════════════════════════════════════════════════════════════
# SERVICE WRAPPER  (request / router path — carries the actor + correlation id)
# ════════════════════════════════════════════════════════════════════════════
class IncidentService:
    def __init__(self, ctx):
        self.ctx = ctx

    def open(self, *, incident_type: str, title: str, severity: Optional[str] = None,
             worker_id: Optional[int] = None, case_id: Optional[str] = None,
             diagnostics: Optional[dict] = None,
             auto_escalate: Optional[bool] = None) -> SecurityIncident:
        return open_incident(
            self.ctx.db, tenant_id=self.ctx.tenant_id, incident_type=incident_type,
            title=title, severity=severity, worker_id=worker_id, case_id=case_id,
            diagnostics=diagnostics, correlation_id=self.ctx.correlation_id,
            actor_id=self.ctx.actor_id, actor_role=self.ctx.actor_role,
            auto_escalate=auto_escalate,
        )

    def resolve(self, incident: SecurityIncident, *, note: Optional[str] = None) -> SecurityIncident:
        return resolve_incident(self.ctx.db, incident, actor_id=self.ctx.actor_id,
                                note=note, actor_role=self.ctx.actor_role,
                                correlation_id=self.ctx.correlation_id)

    def list(self, *, status: Optional[str] = None, severity: Optional[str] = None,
             worker_id: Optional[int] = None, limit: int = 100,
             offset: int = 0) -> List[SecurityIncident]:
        q = (self.ctx.db.query(SecurityIncident)
             .filter(SecurityIncident.tenant_id == self.ctx.tenant_id))
        if status:
            q = q.filter(SecurityIncident.status == status)
        if severity:
            q = q.filter(SecurityIncident.severity == severity)
        if worker_id is not None:
            q = q.filter(SecurityIncident.worker_id == worker_id)
        return (q.order_by(SecurityIncident.created_at.desc())
                .limit(max(1, min(limit, 500))).offset(max(0, offset)).all())

    def get(self, incident_id: str) -> Optional[SecurityIncident]:
        return (self.ctx.db.query(SecurityIncident)
                .filter(SecurityIncident.id == incident_id,
                        SecurityIncident.tenant_id == self.ctx.tenant_id)
                .first())
