"""Background jobs (§5).

Five async-friendly processors, each a plain ``run_once`` over a fresh DB
session so they can be driven by the FastAPI lifespan scheduler, a cron, or a
test — with no shared mutable state:

  1. scheduled_final_access   — SCHEDULED cases whose final_access_at has passed
                                are driven through begin → access closure.
  2. revocation_retries       — ACCESS_REVOCATION_FAILED cases are retried
                                (with an attempt cap that escalates to a human).
  3. scim_reconciliation      — the reconciliation queue is drained (retry +
                                dead-letter) for every tenant.
  4. tracking_stop_enforcement— a safety-net sweep that closes any open clock
                                session for an offboarded worker and flags any
                                telemetry that slipped past the gate.
  5. identity_conflict_detector — the five SSO/SCIM drift scenarios are scanned.

Each processor isolates failure per unit of work (commit per case/item, roll
back and log on error) so one bad row can never stall a whole tick. The
combined ``run_once_sync`` runs all five; ``run_forever`` is the lifespan loop,
matching the commercial schedulers' contract.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import Activity, ClockSession, User

# §45.D-15 — session-boundary provenance for the offboarding closure sweep.
from app.clock_session_boundary import (
    BOUNDARY_CLOCK_OUT,
    Provenance,
    SYSTEM_ACTOR_LABEL,
    append_boundary_event,
)

from . import ids, incidents, reconciliation
from .constants import (
    ACCESS_CLOSED_STATES, RECONCILIATION_MAX_ATTEMPTS, AlertType, CaseState,
    RevocationStatus,
)
from .models import AccessRevocationRecord, OffboardingCase
from .rbac import AuthzContext, Role
from .services import OffboardingContext, OffboardingService

_log = logging.getLogger("zoiko.offboarding.jobs")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _system_ctx(db: Session, tenant_id: str, correlation: Optional[str] = None) -> OffboardingContext:
    """A super-admin system actor for unattended lifecycle moves. actor_id is
    None (the audit row records the system role, never a fake user)."""
    authz = AuthzContext(user_id=None, tenant_id=tenant_id,
                         roles=frozenset({Role.SUPER_ADMIN}))
    return OffboardingContext(db=db, authz=authz,
                              correlation_id=correlation or ids.correlation_id())


def _tenants(db: Session) -> List[str]:
    return [row[0] for row in db.query(OffboardingCase.tenant_id).distinct().all()]


# ════════════════════════════════════════════════════════════════════════════
# 1. SCHEDULED FINAL-ACCESS PROCESSOR
# ════════════════════════════════════════════════════════════════════════════
def process_scheduled_final_access(db: Session, *, now: Optional[datetime] = None) -> int:
    """Drive every SCHEDULED case whose final_access_at has elapsed through
    begin → access closure. Commits per case."""
    now = now or _now()
    moved = 0
    for tenant in _tenants(db):
        cases = (db.query(OffboardingCase)
                 .filter(OffboardingCase.tenant_id == tenant,
                         OffboardingCase.status == CaseState.SCHEDULED.value,
                         OffboardingCase.final_access_at.isnot(None))
                 .all())
        for case in cases:
            fa = _as_utc(case.final_access_at)
            if fa is None or now < fa:
                continue
            try:
                svc = OffboardingService(_system_ctx(db, tenant))
                svc.begin_offboarding(case.id)
                svc.execute_access_closure(case.id)
                db.commit()
                moved += 1
            except Exception:
                db.rollback()
                _log.exception("scheduled final-access processing failed case=%s", case.id)
    return moved


# ════════════════════════════════════════════════════════════════════════════
# 2. REVOCATION RETRY PROCESSOR
# ════════════════════════════════════════════════════════════════════════════
def process_revocation_retries(db: Session) -> int:
    """Retry cases stuck in ACCESS_REVOCATION_FAILED. Once a component has burned
    through the attempt cap we stop auto-retrying and escalate to a human (a
    HIGH incident) instead of hammering forever."""
    retried = 0
    for tenant in _tenants(db):
        cases = (db.query(OffboardingCase)
                 .filter(OffboardingCase.tenant_id == tenant,
                         OffboardingCase.status == CaseState.ACCESS_REVOCATION_FAILED.value)
                 .all())
        for case in cases:
            recs = (db.query(AccessRevocationRecord)
                    .filter(AccessRevocationRecord.case_id == case.id,
                            AccessRevocationRecord.status == RevocationStatus.FAILED.value)
                    .all())
            exhausted = [r for r in recs if (r.attempts or 0) >= RECONCILIATION_MAX_ATTEMPTS]
            try:
                if exhausted:
                    incidents.open_incident(
                        db, tenant_id=tenant,
                        incident_type=AlertType.SESSION_REVOCATION_FAILURE.value,
                        title=f"Revocation stuck after {RECONCILIATION_MAX_ATTEMPTS} attempts "
                              f"on case {case.id}",
                        worker_id=case.worker_id, case_id=case.id,
                        diagnostics={"components": [r.component for r in exhausted]},
                    )
                    db.commit()
                    continue
                svc = OffboardingService(_system_ctx(db, tenant))
                svc.retry_access_closure(case.id)
                db.commit()
                retried += 1
            except Exception:
                db.rollback()
                _log.exception("revocation retry failed case=%s", case.id)
    return retried


# ════════════════════════════════════════════════════════════════════════════
# 3. SCIM / SSO RECONCILIATION PROCESSOR
# ════════════════════════════════════════════════════════════════════════════
def process_scim_reconciliation(db: Session) -> dict:
    """Drain the reconciliation queue (retry + dead-letter) across tenants."""
    try:
        counts = reconciliation.process_queue(db, limit=200)
        db.commit()
        return counts
    except Exception:
        db.rollback()
        _log.exception("reconciliation queue processing failed")
        return {"error": True}


# ════════════════════════════════════════════════════════════════════════════
# 4. TRACKING-STOP ENFORCEMENT SWEEP
# ════════════════════════════════════════════════════════════════════════════
def enforce_tracking_stop(db: Session) -> dict:
    """Safety net for the runtime gate: close any open clock session belonging to
    a worker whose case has passed access closure, and flag any activity row that
    landed after the cutover (which should be impossible once the ingest gate is
    wired — its presence means a bypass and is a CRITICAL incident)."""
    closed_sessions = 0
    drift_flags = 0
    for tenant in _tenants(db):
        cases = (db.query(OffboardingCase)
                 .filter(OffboardingCase.tenant_id == tenant,
                         OffboardingCase.status.in_(list(ACCESS_CLOSED_STATES)),
                         OffboardingCase.worker_id.isnot(None))
                 .all())
        for case in cases:
            try:
                changed = False
                open_sessions = (db.query(ClockSession)
                                 .filter(ClockSession.user_id == case.worker_id,
                                         ClockSession.clock_out_time.is_(None))
                                 .all())
                for s in open_sessions:
                    _closed_at = _now()
                    s.clock_out_time = _closed_at
                    s.is_active = False
                    closed_sessions += 1
                    changed = True
                    # §45.D-15 — a synthesized closure caused by offboarding.
                    # This is the unattended safety net for the runtime gate,
                    # so there is no acting person: SYSTEM is the actor label.
                    # The provenance stays OFFBOARDING — SYSTEM is never a
                    # provenance value.
                    append_boundary_event(
                        db,
                        session_id  = s.id,
                        user_id     = s.user_id,
                        boundary    = BOUNDARY_CLOCK_OUT,
                        boundary_at = _closed_at,
                        provenance  = Provenance.OFFBOARDING,
                        actor_label = SYSTEM_ACTOR_LABEL,
                    )

                # Count any activity rows that landed AFTER access closure as a
                # bypass signal. Without a cutover timestamp we can only flag the
                # open-session drift; the count branch is skipped honestly.
                cutoff = _as_utc(case.final_access_at) or _as_utc(case.completed_at)
                leaked = 0
                if cutoff is not None:
                    leaked = (db.query(Activity)
                              .filter(Activity.user_id == case.worker_id,
                                      Activity.timestamp > cutoff)
                              .count())
                if leaked > 0 or open_sessions:
                    incidents.open_incident(
                        db, tenant_id=tenant,
                        incident_type=AlertType.TRACKING_AFTER_OFFBOARDING.value,
                        title=f"Post-exit tracking drift for worker {case.worker_id}",
                        worker_id=case.worker_id, case_id=case.id,
                        diagnostics={"leaked_activity_rows": leaked,
                                     "open_sessions_closed": len(open_sessions),
                                     "cutoff": cutoff.isoformat() if cutoff else None},
                    )
                    drift_flags += 1
                    changed = True
                if changed:
                    db.commit()
            except Exception:
                db.rollback()
                _log.exception("tracking-stop sweep failed case=%s", case.id)
    return {"sessions_closed": closed_sessions, "drift_flagged": drift_flags}


# ════════════════════════════════════════════════════════════════════════════
# 5. IDENTITY CONFLICT DETECTOR
# ════════════════════════════════════════════════════════════════════════════
def detect_identity_conflicts(db: Session) -> dict:
    try:
        result = reconciliation.detect_conflicts(db, limit=200)
        db.commit()
        return result
    except Exception:
        db.rollback()
        _log.exception("identity conflict detection failed")
        return {"error": True}


# ════════════════════════════════════════════════════════════════════════════
# COMBINED TICK + SCHEDULER
# ════════════════════════════════════════════════════════════════════════════
def run_once_sync() -> dict:
    """Run all five processors once. Returns a per-processor summary. Each
    processor owns its own commits, so a failure in one never aborts another."""
    db = SessionLocal()
    summary: dict = {}
    try:
        summary["scheduled_final_access"] = process_scheduled_final_access(db)
        summary["revocation_retries"] = process_revocation_retries(db)
        summary["scim_reconciliation"] = process_scim_reconciliation(db)
        summary["tracking_stop"] = enforce_tracking_stop(db)
        summary["identity_conflicts"] = detect_identity_conflicts(db)
    except Exception:
        _log.exception("offboarding job tick failed")
    finally:
        db.close()
    return summary


async def run_forever(interval_seconds: int = 300) -> None:
    """Async loop for the FastAPI lifespan. Runs the tick on a worker thread so
    the event loop is never blocked by DB I/O (matches trial_lifecycle/dunning).

    Honors the OFFBOARDING_SCHEDULER_ENABLED kill-switch (§8): when disabled the
    loop idles instead of exiting, so flipping the flag back on (rolling
    restart) resumes processing without re-wiring the lifespan."""
    from . import flags
    _log.info("Offboarding enforcement scheduler started (interval=%ss)", interval_seconds)
    while True:
        try:
            if not flags.scheduler_enabled():
                _log.info("Offboarding scheduler DISABLED via flag — skipping tick")
            else:
                summary = await asyncio.to_thread(run_once_sync)
                if any(summary.values()):
                    _log.info("Offboarding enforcement tick: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Offboarding enforcement tick error")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
