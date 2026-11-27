"""
Temveris – Work Evidence Chain (WEC) Router
============================================
Endpoints:
  POST /break-log              – Record a break with activity description into WEC
  GET  /evidence               – Query the immutable Work Evidence Chain
  GET  /wec/audit-trail        – WEC + AuditLog combined view
  GET  /wec/integrity-scores   – View computed per-session integrity scores
  POST /wec/compute-integrity  – Trigger integrity computation for a session
  GET  /wec/verify-chain       – Verify hash chain integrity for a user
"""

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime, timezone

from database.database import get_db
from database import models
from app.auth.service import get_current_user
from . import service
from app.commercial.enforcement import shadow_capability as _shadow

router = APIRouter(tags=["Work Evidence Chain"])


# ══════════════════════════════════════════════
# POST /break-log
# Called by frontend popup when user resumes from break and submits
# activity description.
# ══════════════════════════════════════════════
class BreakLogPayload(BaseModel):
    break_start_time: str   # ISO 8601
    break_end_time:   str   # ISO 8601
    duration_seconds: int
    activity:         str   # what the employee did during break
    description:      str   # additional notes


@router.post("/break-log")
def log_break(
    payload: BreakLogPayload,
    request: Request,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """
    Record a completed break with activity evidence into the Work Evidence Chain.
    Call this from the popup that appears when the employee resumes work.
    """
    device_info = {
        "ip_address": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown"),
    }

    # Link to active clock session if present
    session = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_out_time == None,
        )
    ).first()

    wec_payload = {
        "break_start_time": payload.break_start_time,
        "break_end_time":   payload.break_end_time,
        "duration_seconds": payload.duration_seconds,
        "activity":         payload.activity,
        "description":      payload.description,
    }

    wec_entry = service.create_wec_entry(
        db,
        user_id         = user.id,
        organization_id = user.organization_id,
        action_type     = "break_ended",
        session_id      = session.id if session else None,
        device_info     = device_info,
        payload         = wec_payload,
    )

    event = service.emit_work_event(
        db,
        event_type      = "BREAK_ENDED",
        user_id         = user.id,
        organization_id = user.organization_id,
        session_id      = session.id if session else None,
        payload         = wec_payload,
        idempotency_key = f"break_ended:{user.id}:{payload.break_end_time}",
        wec_id          = wec_entry.work_record_id,
    )

    db.commit()

    return {
        "success":        True,
        "work_record_id": wec_entry.work_record_id,
        "current_hash":   wec_entry.current_hash,
        "event_id":       event.event_id,
    }


# ══════════════════════════════════════════════
# GET /evidence
# ══════════════════════════════════════════════
@router.get("/evidence")
def get_evidence(
    user_id:     Optional[int]  = Query(None, description="Filter by employee ID"),
    action_type: Optional[str]  = Query(None, description="login|logout|break_started|break_ended|screenshot|activity"),
    date_from:   Optional[date] = Query(None),
    date_to:     Optional[date] = Query(None),
    limit:       int            = Query(100, ge=1, le=500),
    skip:        int            = Query(0,   ge=0),
    db:          Session        = Depends(get_db),
    current_user: models.User   = Depends(get_current_user),
):
    """Query the Work Evidence Chain. All records are immutable append-only proof."""
    org_id = current_user.organization_id

    q = db.query(models.WorkEvidenceChain).filter(
        models.WorkEvidenceChain.organization_id == org_id
    ).order_by(models.WorkEvidenceChain.timestamp.desc())

    if user_id is not None:
        target = db.query(models.User).filter(
            models.User.id == user_id,
            models.User.organization_id == org_id,
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        q = q.filter(models.WorkEvidenceChain.user_id == user_id)
    if action_type:
        q = q.filter(models.WorkEvidenceChain.action_type == action_type)
    if date_from:
        q = q.filter(func.date(models.WorkEvidenceChain.timestamp) >= date_from)
    if date_to:
        q = q.filter(func.date(models.WorkEvidenceChain.timestamp) <= date_to)

    total   = q.count()
    entries = q.offset(skip).limit(limit).all()

    return {
        "total": total,
        "entries": [
            {
                "work_record_id":    e.work_record_id,
                "user_id":           e.user_id,
                "session_id":        e.session_id,
                "timestamp":         e.timestamp.isoformat(),
                "action_type":       e.action_type,
                "device_info":       e.device_info,
                "previous_hash":     e.previous_hash,
                "current_hash":      e.current_hash,
                "validation_status": e.validation_status,
                "integrity_score":   e.integrity_score,
                "payload":           e.payload,
            }
            for e in entries
        ],
    }


# ══════════════════════════════════════════════
# GET /wec/audit-trail
# ══════════════════════════════════════════════
@router.get("/wec/audit-trail")
def get_wec_audit_trail(
    user_id: Optional[int] = Query(None),
    limit:   int           = Query(100, ge=1, le=500),
    db:      Session       = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Combined view: Work Evidence Chain records + system AuditLog entries."""
    # SHADOW ONLY - the compliance/audit reporting surface.
    _shadow(db, current_user, "reports.compliance",
            route="GET /wec/audit-trail")
    org_id = current_user.organization_id
    half = max(1, limit // 2)

    if user_id is not None:
        target = db.query(models.User).filter(
            models.User.id == user_id,
            models.User.organization_id == org_id,
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

    wec_q = db.query(models.WorkEvidenceChain).filter(
        models.WorkEvidenceChain.organization_id == org_id
    ).order_by(models.WorkEvidenceChain.timestamp.desc())
    if user_id is not None:
        wec_q = wec_q.filter(models.WorkEvidenceChain.user_id == user_id)
    wec_entries = wec_q.limit(half).all()

    audit_q = db.query(models.AuditLog).filter(
        models.AuditLog.organization_id == org_id
    ).order_by(models.AuditLog.created_at.desc())
    if user_id is not None:
        audit_q = audit_q.filter(models.AuditLog.actor_id == user_id)
    audit_entries = audit_q.limit(half).all()

    return {
        "work_evidence_chain": [
            {
                "record_id":   e.work_record_id,
                "user_id":     e.user_id,
                "timestamp":   e.timestamp.isoformat(),
                "action":      e.action_type,
                "hash":        e.current_hash,
                "score":       e.integrity_score,
                "status":      e.validation_status,
                "payload":     e.payload,
            }
            for e in wec_entries
        ],
        "audit_logs": [
            {
                "id":        l.id,
                "actor_id":  l.actor_id,
                "entity":    l.entity,
                "action":    l.action,
                "target_id": l.target_id,
                "timestamp": l.created_at.isoformat(),
                "payload":   l.payload,
            }
            for l in audit_entries
        ],
    }


# ══════════════════════════════════════════════
# GET /wec/integrity-scores
# ══════════════════════════════════════════════
@router.get("/wec/integrity-scores")
def list_integrity_scores(
    user_id:    Optional[int] = Query(None),
    session_id: Optional[int] = Query(None),
    limit:      int           = Query(50, ge=1, le=200),
    db:         Session       = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """View stored integrity scores. High idle_spoofing_risk or fake_break_risk signals fraud."""
    org_id = current_user.organization_id

    if user_id is not None:
        target = db.query(models.User).filter(
            models.User.id == user_id,
            models.User.organization_id == org_id,
        ).first()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

    q = db.query(models.IntegrityScore).filter(
        models.IntegrityScore.organization_id == org_id
    ).order_by(models.IntegrityScore.computed_at.desc())
    if user_id is not None:
        q = q.filter(models.IntegrityScore.user_id == user_id)
    if session_id is not None:
        q = q.filter(models.IntegrityScore.session_id == session_id)

    rows = q.limit(limit).all()
    return [
        {
            "user_id":             r.user_id,
            "session_id":          r.session_id,
            "date":                r.date.isoformat() if r.date else None,
            "score":               r.score,
            "anomaly_flags":       r.anomaly_flags,
            "idle_spoofing_risk":  r.idle_spoofing_risk,
            "fake_break_risk":     r.fake_break_risk,
            "time_inflation_risk": r.time_inflation_risk,
            "unusual_gaps_risk":   r.unusual_gaps_risk,
            "computed_at":         r.computed_at.isoformat(),
        }
        for r in rows
    ]


# ══════════════════════════════════════════════
# POST /wec/compute-integrity/{session_id}
# ══════════════════════════════════════════════
@router.post("/wec/compute-integrity/{session_id}")
def compute_and_store_integrity(
    session_id: int,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Compute and persist an IntegrityScore for a given session."""
    result = service.compute_integrity_score(db, user.id, session_id)

    existing = db.query(models.IntegrityScore).filter(
        and_(
            models.IntegrityScore.user_id    == user.id,
            models.IntegrityScore.session_id == session_id,
        )
    ).first()

    now = datetime.now(timezone.utc)
    if existing:
        existing.score               = result["score"]
        existing.anomaly_flags       = result["anomaly_flags"]
        existing.idle_spoofing_risk  = result["idle_spoofing_risk"]
        existing.fake_break_risk     = result["fake_break_risk"]
        existing.time_inflation_risk = result["time_inflation_risk"]
        existing.unusual_gaps_risk   = result["unusual_gaps_risk"]
        existing.computed_at         = now
    else:
        db.add(models.IntegrityScore(
            user_id              = user.id,
            organization_id      = user.organization_id,
            session_id           = session_id,
            date                 = now,
            score                = result["score"],
            anomaly_flags        = result["anomaly_flags"],
            idle_spoofing_risk   = result["idle_spoofing_risk"],
            fake_break_risk      = result["fake_break_risk"],
            time_inflation_risk  = result["time_inflation_risk"],
            unusual_gaps_risk    = result["unusual_gaps_risk"],
        ))

    db.commit()
    return result


# ══════════════════════════════════════════════
# GET /wec/verify-chain/{user_id}
# ══════════════════════════════════════════════
@router.get("/wec/verify-chain/{user_id}")
def verify_user_chain(
    user_id: int,
    db:      Session     = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Walk the entire WEC for a user and verify every SHA-256 link is intact.
    Any broken link indicates tampering or data corruption.
    """
    # SHADOW ONLY - chain-of-custody verification is the literal capability.
    _shadow(db, current_user, "governance.chain_of_custody",
            route="GET /wec/verify-chain/{user_id}")
    target = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == current_user.organization_id,
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    result = service.verify_chain(db, user_id)
    return result
