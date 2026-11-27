"""Operational metrics (§3 — monitoring & alerting).

Pure read-only aggregates the ops dashboard and alerting pipeline consume.
Two surfaces:

  • ``compute_metrics`` — a JSON snapshot (per-tenant or cross-tenant) of the
    gauges the spec calls out: blocked cases, revocation failures, SLA
    breaches, former-worker access count, plus open incidents by severity,
    quarantine volume, and reconciliation backlog.
  • ``prometheus_text`` — the same gauges in Prometheus text-exposition format
    so an existing scrape config can ingest them at /metrics without a new
    pipeline.

Alert *thresholds* live in monitoring/alerts.yaml; this module only measures.
Keeping measurement and alerting separate means ops can retune thresholds
without a code deploy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .constants import (
    CaseState, FormerAccessStatus, IncidentStatus, ReconciliationStatus,
    RevocationStatus,
)
from .models import (
    AccessRevocationRecord, FormerWorkerAccess, IntegrationReconciliationItem,
    OffboardingCase, OffboardingTask, SecurityIncident,
    TelemetryQuarantineRecord,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_metrics(db: Session, *, tenant_id: Optional[str] = None) -> Dict:
    """Aggregate the §3 gauges. ``tenant_id=None`` aggregates across tenants
    (platform-ops view); a value scopes to one tenant."""
    def scoped(q, model):
        return q.filter(model.tenant_id == tenant_id) if tenant_id is not None else q

    # ── Cases ────────────────────────────────────────────────────────────
    blocked_cases = scoped(
        db.query(func.count(OffboardingCase.id))
          .filter(OffboardingCase.status == CaseState.BLOCKED.value),
        OffboardingCase).scalar() or 0

    revocation_failed_cases = scoped(
        db.query(func.count(OffboardingCase.id))
          .filter(OffboardingCase.status == CaseState.ACCESS_REVOCATION_FAILED.value),
        OffboardingCase).scalar() or 0

    active_cases = scoped(
        db.query(func.count(OffboardingCase.id))
          .filter(OffboardingCase.status == CaseState.IN_PROGRESS.value),
        OffboardingCase).scalar() or 0

    completed_cases = scoped(
        db.query(func.count(OffboardingCase.id))
          .filter(OffboardingCase.status == CaseState.COMPLETED.value),
        OffboardingCase).scalar() or 0

    # ── Revocation surfaces still failed ─────────────────────────────────
    revocation_failures = scoped(
        db.query(func.count(AccessRevocationRecord.id))
          .filter(AccessRevocationRecord.status == RevocationStatus.FAILED.value),
        AccessRevocationRecord).scalar() or 0

    # ── SLA breaches (open tasks past due) ───────────────────────────────
    sla_breaches = scoped(
        db.query(func.count(OffboardingTask.id))
          .filter(OffboardingTask.sla_breached.is_(True),
                  OffboardingTask.status.notin_(["completed", "waived"])),
        OffboardingTask).scalar() or 0

    # ── Former-worker access live now ────────────────────────────────────
    former_worker_access_count = scoped(
        db.query(func.count(FormerWorkerAccess.id))
          .filter(FormerWorkerAccess.status == FormerAccessStatus.ACTIVE.value),
        FormerWorkerAccess).scalar() or 0

    # ── Incidents by severity (open + escalated) ─────────────────────────
    open_states = [IncidentStatus.OPEN.value, IncidentStatus.ESCALATED.value]
    sev_rows = (scoped(
        db.query(SecurityIncident.severity, func.count(SecurityIncident.id))
          .filter(SecurityIncident.status.in_(open_states)),
        SecurityIncident).group_by(SecurityIncident.severity).all())
    incidents_by_severity = {sev: n for sev, n in sev_rows}
    open_incidents = sum(incidents_by_severity.values())
    critical_incidents = incidents_by_severity.get("critical", 0)

    # ── Quarantine volume (post-exit telemetry rejected, last 24h) ───────
    horizon = _now() - timedelta(hours=24)
    quarantine_24h = scoped(
        db.query(func.count(TelemetryQuarantineRecord.id))
          .filter(TelemetryQuarantineRecord.created_at >= horizon),
        TelemetryQuarantineRecord).scalar() or 0

    # ── Reconciliation backlog (pending/failed/dead-letter) ──────────────
    recon_pending = scoped(
        db.query(func.count(IntegrationReconciliationItem.id))
          .filter(IntegrationReconciliationItem.status.in_([
              ReconciliationStatus.PENDING.value, ReconciliationStatus.FAILED.value])),
        IntegrationReconciliationItem).scalar() or 0
    recon_dead_letter = scoped(
        db.query(func.count(IntegrationReconciliationItem.id))
          .filter(IntegrationReconciliationItem.status == ReconciliationStatus.DEAD_LETTER.value),
        IntegrationReconciliationItem).scalar() or 0

    return {
        "tenant_id": tenant_id or "*",
        "generated_at": _now().isoformat(),
        "cases": {
            "active": active_cases,
            "blocked": blocked_cases,
            "revocation_failed": revocation_failed_cases,
            "completed": completed_cases,
        },
        "revocation_failures": revocation_failures,
        "sla_breaches": sla_breaches,
        "former_worker_access_count": former_worker_access_count,
        "incidents": {
            "open": open_incidents,
            "critical": critical_incidents,
            "by_severity": incidents_by_severity,
        },
        "quarantine_24h": quarantine_24h,
        "reconciliation": {
            "backlog": recon_pending,
            "dead_letter": recon_dead_letter,
        },
    }


def prometheus_text(db: Session, *, tenant_id: Optional[str] = None) -> str:
    """Render the gauges in Prometheus text-exposition format. A tenant label
    is attached so a single scrape can fan out across tenants over time."""
    m = compute_metrics(db, tenant_id=tenant_id)
    label = f'{{tenant="{m["tenant_id"]}"}}'
    lines = [
        "# HELP offboarding_blocked_cases Cases currently in the BLOCKED state.",
        "# TYPE offboarding_blocked_cases gauge",
        f'offboarding_blocked_cases{label} {m["cases"]["blocked"]}',
        "# HELP offboarding_revocation_failures Access-revocation records in FAILED state.",
        "# TYPE offboarding_revocation_failures gauge",
        f'offboarding_revocation_failures{label} {m["revocation_failures"]}',
        "# HELP offboarding_sla_breaches Open tasks past their SLA.",
        "# TYPE offboarding_sla_breaches gauge",
        f'offboarding_sla_breaches{label} {m["sla_breaches"]}',
        "# HELP offboarding_former_worker_access Active former-worker portal grants.",
        "# TYPE offboarding_former_worker_access gauge",
        f'offboarding_former_worker_access{label} {m["former_worker_access_count"]}',
        "# HELP offboarding_open_incidents Open + escalated security incidents.",
        "# TYPE offboarding_open_incidents gauge",
        f'offboarding_open_incidents{label} {m["incidents"]["open"]}',
        "# HELP offboarding_critical_incidents Open CRITICAL security incidents.",
        "# TYPE offboarding_critical_incidents gauge",
        f'offboarding_critical_incidents{label} {m["incidents"]["critical"]}',
        "# HELP offboarding_quarantine_24h Post-exit telemetry rejections in the last 24h.",
        "# TYPE offboarding_quarantine_24h gauge",
        f'offboarding_quarantine_24h{label} {m["quarantine_24h"]}',
        "# HELP offboarding_reconciliation_backlog Pending/failed reconciliation items.",
        "# TYPE offboarding_reconciliation_backlog gauge",
        f'offboarding_reconciliation_backlog{label} {m["reconciliation"]["backlog"]}',
        "# HELP offboarding_reconciliation_dead_letter Dead-lettered reconciliation items.",
        "# TYPE offboarding_reconciliation_dead_letter gauge",
        f'offboarding_reconciliation_dead_letter{label} {m["reconciliation"]["dead_letter"]}',
    ]
    return "\n".join(lines) + "\n"
