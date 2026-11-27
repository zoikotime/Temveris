"""
My Data module router.

Worker-facing transparency surface — five sub-pages:
  GET  /my-data/capture-config   What's Recorded — capture types + retention + policy refs
  GET  /my-data/who-viewed       Who Viewed My Data — human access events targeting this user
  GET  /my-data/corrections      Corrections & Disputes — list this user's correction requests
  POST /my-data/corrections      Submit a correction request
  GET  /my-data/exports          My Data Export — list past export requests
  POST /my-data/exports          Request a new export
  POST /my-data/dsar             Submit a Data Subject Access Request
  POST /my-data/deletion         Submit a deletion request
  GET  /my-data/policy-acks      List policy acknowledgements (with required ones marked)
  POST /my-data/policy-acks      Record an acknowledgement

Implementation note: this is a v1 surface. The full persistence layer
(correction_request, data_export_request, dsar_request, etc. tables)
has not yet shipped, so write-paths log to AuditLog as a stopgap, and
read-paths return either AuditLog-derived data or empty lists. The
frontend handles empty lists cleanly. When the dedicated models land,
swap the stub bodies below for real queries — the wire shape stays the same.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from app.email_service import send_data_request_received
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user


router = APIRouter(prefix="/my-data", tags=["My Data"])


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for", "") if request else ""
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _audit(
    db: Session,
    *,
    actor_id: Optional[int],
    target_id: Optional[int],
    entity: str,
    action: str,
    payload: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    org_id: Optional[int] = None,
) -> models.AuditLog:
    row = models.AuditLog(
        actor_id=actor_id,
        target_id=target_id,
        organization_id=org_id,
        entity=entity,
        action=action,
        payload=payload or {},
        ip_address=_client_ip(request),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# 1. CAPTURE CONFIG  (What's Recorded)
# Reads workspace settings and projects them into the capture-row shape the
# frontend expects. The frontend already has a sensible default if the
# workspace hasn't configured one — this endpoint refines that default.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/capture-config")
def get_capture_config(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # AppSettings has no boolean columns matching capture keys — always use defaults.
    settings = None

    def _bool(attr: str, default: bool) -> bool:
        if settings is None:
            return default
        return bool(getattr(settings, attr, default))

    captures = [
        {
            "key": "time_tracking", "icon": "clock",
            "label": "Time tracking",
            "state": "on",
            "detail": "Captures clock-in, clock-out, breaks, work sessions, and idle periods where enabled.",
            "why": "Required to compute pay, attendance, and compliance with working-time rules.",
            "retention_days": 2555,
            "retention_label": "7 years",
            "who_can_view": ["Direct manager", "HR Admin", "Payroll", "You"],
        },
        {
            "key": "app_usage", "icon": "monitor",
            "label": "App and website usage",
            "state": "on" if _bool("track_apps", True) else "off",
            "detail": "Records app and site categories during work sessions, based on workspace policy.",
            "why": "Productivity insights and compliance with acceptable-use policies.",
            "retention_days": 180,
            "retention_label": "180 days",
            "who_can_view": ["Direct manager", "You"],
        },
        {
            "key": "screenshots", "icon": "camera",
            "label": "Screenshots",
            "state": "on" if _bool("perm_emp_see_screenshot", True) else "off",
            "detail": "Periodic screenshots taken while you're clocked in. Capture frequency and blur rules follow workspace policy.",
            "why": "Proof-of-work for client billing, audit, and dispute resolution.",
            "retention_days": 90,
            "retention_label": "90 days",
            "who_can_view": ["Direct manager", "HR Admin", "You"],
            "meta": [
                "Frequency: every 10 min",
                "Sensitive-app blur: enabled",
                "Worker preview: available",
            ],
        },
        {
            "key": "location", "icon": "map",
            "label": "Location",
            "state": "off",
            "detail": "Captured only where configured and legally permitted, while clocked in.",
            "why": "Field-work attendance and safety compliance.",
            "retention_days": 30,
            "retention_label": "30 days",
            "who_can_view": ["Direct manager", "You"],
        },
        {
            "key": "keyboard", "icon": "keyboard",
            "label": "Keyboard activity",
            "state": "activity_only",
            "detail": "Records typing presence only. No keystroke content is stored.",
            "why": "Distinguishing active work from idle time.",
            "retention_days": 90,
            "retention_label": "90 days",
            "who_can_view": ["Direct manager", "You"],
        },
        {
            "key": "mouse", "icon": "mouse",
            "label": "Mouse activity",
            "state": "on",
            "detail": "Records movement presence. Cursor position and clicks are not stored as content.",
            "why": "Distinguishing active work from idle time.",
            "retention_days": 90,
            "retention_label": "90 days",
            "who_can_view": ["Direct manager", "You"],
        },
    ]

    return {
        "policy": {
            "name": "Workforce Monitoring Policy",
            "version": "1.0",
            "last_updated": settings.updated_at.isoformat() if settings and getattr(settings, "updated_at", None) else None,
            "updated_by": None,
            "summary": (
                "This policy defines how your workspace records work activity, how long records are kept, "
                "and who can access them. Acknowledging it confirms you've read it — it does not constitute "
                "consent for any specific form of processing where consent is legally required."
            ),
        },
        "captures": captures,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. WHO VIEWED MY DATA
# Returns AuditLog rows where target_id == current_user.id, projected into the
# shape the frontend expects. Anomaly flag is heuristic for now: any view that
# isn't a routine timesheet read by a direct manager is flagged.
# ─────────────────────────────────────────────────────────────────────────────
VIEW_ACTIONS = {
    "ProfileViewed", "TimesheetViewed", "ScreenshotViewed",
    "ActivityViewed", "ReportExported", "DataAccessed",
}

@router.get("/who-viewed")
def who_viewed_my_data(
    days: Optional[str] = "30",
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    q = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.target_id == current_user.id)
        .filter(models.AuditLog.actor_id != current_user.id)  # exclude self-events
    )

    # Optional time window
    if days and days != "all":
        try:
            n = max(1, int(days))
            q = q.filter(models.AuditLog.created_at >= datetime.now(timezone.utc) - timedelta(days=n))
        except ValueError:
            pass

    rows = (
        q.order_by(models.AuditLog.created_at.desc())
        .limit(max(1, min(int(limit or 100), 500)))
        .all()
    )

    # Hydrate accessor names in a single round-trip
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    actors: Dict[int, models.User] = {
        u.id: u
        for u in db.query(models.User).filter(models.User.id.in_(actor_ids)).all()
    } if actor_ids else {}

    out = []
    for r in rows:
        actor = actors.get(r.actor_id) if r.actor_id else None
        action = r.action or ""
        is_routine = (
            action == "TimesheetViewed"
            and (r.payload or {}).get("relationship") == "direct_manager"
        )
        out.append({
            "id": r.id,
            "accessed_at":      r.created_at.isoformat() if r.created_at else None,
            "accessor_name":    actor.name if actor else "System",
            "accessor_role":    (actor.role if actor else None) or "—",
            "surface_accessed": (r.payload or {}).get("surface") or action or "—",
            "reason":           (r.payload or {}).get("reason") or "Routine review",
            "anomaly_flag":     not is_routine and action in VIEW_ACTIONS,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 3. CORRECTIONS & DISPUTES
# v1: writes go to AuditLog with action="CorrectionRequested"; reads return the
# same audit rows projected into the correction-request shape. Replace with a
# dedicated correction_request table when models.py grows the entity.
# ─────────────────────────────────────────────────────────────────────────────
class CorrectionIn(BaseModel):
    request_type: str = Field(..., description="time_entry | idle_time | screenshot | app_classification | other")
    period_from:  Optional[str] = None
    period_to:    Optional[str] = None
    correction:   str = Field(..., min_length=1, max_length=4000)
    reason:       Optional[str] = Field(None, max_length=4000)
    evidence_filename: Optional[str] = Field(None, max_length=255)


@router.get("/corrections")
def list_corrections(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    rows = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == current_user.id,
            models.AuditLog.entity == "CorrectionRequest",
        )
        .order_by(models.AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id":              r.id,
            "request_type":    (r.payload or {}).get("request_type"),
            "submitted_at":    r.created_at.isoformat() if r.created_at else None,
            "status":          (r.payload or {}).get("status", "submitted"),
            "resolved_at":     None,
            "outcome_reason":  (r.payload or {}).get("outcome_reason"),
            "reviewer_name":   (r.payload or {}).get("reviewer_name"),
            "correction":      (r.payload or {}).get("correction"),
            "reason":          (r.payload or {}).get("reason"),
        }
        for r in rows
    ]


@router.post("/corrections", status_code=201)
def submit_correction(
    body: CorrectionIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="CorrectionRequest",
        action="CorrectionRequested",
        payload={
            "request_type": body.request_type,
            "period_from":  body.period_from,
            "period_to":    body.period_to,
            "correction":   body.correction,
            "reason":       body.reason,
            "evidence_filename": body.evidence_filename,
            "status":       "submitted",
        },
        request=request,
        org_id=current_user.organization_id,
    )
    # ZT-DAT-01 request-received acknowledgment (Critical; non-suppressible).
    # Background task — a mail failure can never block the request. Carries only
    # reference / type / status (no request details or personal data).
    background_tasks.add_task(
        send_data_request_received,
        current_user.email, current_user.name or "", row.id,
        f"Data correction ({body.request_type})", "submitted", None,
    )
    return {
        "id":           row.id,
        "request_type": body.request_type,
        "submitted_at": row.created_at.isoformat() if row.created_at else None,
        "status":       "submitted",
        "correction":   body.correction,
        "reason":       body.reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. EXPORTS  (Standard data export)
# v1: writes log to AuditLog with the requested format/scope. Real exports
# (PDF/JSON/CSV generation, signed URLs, expiry) ship in a follow-up.
# ─────────────────────────────────────────────────────────────────────────────
class ExportIn(BaseModel):
    format: str = Field(..., description="pdf | json | csv")
    scope:  Optional[Dict[str, bool]] = None


@router.get("/exports")
def list_exports(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    rows = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == current_user.id,
            models.AuditLog.entity == "DataExport",
        )
        .order_by(models.AuditLog.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id":           r.id,
            "format":       (r.payload or {}).get("format"),
            "scope":        (r.payload or {}).get("scope"),
            "requested_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": (r.payload or {}).get("completed_at"),
            "download_url": (r.payload or {}).get("download_url"),
            "expires_at":   (r.payload or {}).get("expires_at"),
        }
        for r in rows
    ]


@router.post("/exports", status_code=202)
def request_export(
    body: ExportIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    fmt = body.format.lower()
    if fmt not in ("pdf", "json", "csv"):
        raise HTTPException(400, detail="format must be one of: pdf, json, csv")
    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="DataExport",
        action="DataExportRequested",
        payload={
            "format": fmt,
            "scope":  body.scope or {},
            "status": "queued",
        },
        request=request,
        org_id=current_user.organization_id,
    )
    # ZT-DAT-01 request-received acknowledgment (Critical; non-suppressible).
    # Background task — a mail failure can never block the request. Carries only
    # reference / type / status (no request details or personal data).
    background_tasks.add_task(
        send_data_request_received,
        current_user.email, current_user.name or "", row.id,
        f"Data export ({fmt})", "queued", None,
    )
    return {
        "id":           row.id,
        "format":       fmt,
        "requested_at": row.created_at.isoformat() if row.created_at else None,
        "status":       "queued",
        "message":      "Your export is being prepared. You'll see it in the history below when ready.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. DSAR (Data Subject Access Request)
# ─────────────────────────────────────────────────────────────────────────────
class DSARIn(BaseModel):
    type:    str = Field(..., description="access | rectification | erasure | portability")
    details: Optional[str] = Field(None, max_length=4000)


@router.post("/dsar", status_code=201)
def submit_dsar(
    body: DSARIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if body.type not in ("access", "rectification", "erasure", "portability"):
        raise HTTPException(400, detail="invalid DSAR type")
    now = datetime.now(timezone.utc)
    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="DSARRequest",
        action="DSARSubmitted",
        payload={
            "type":         body.type,
            "details":      body.details,
            "submitted_at": now.isoformat(),
            "due_at":       (now + timedelta(days=30)).isoformat(),
            "status":       "submitted",
        },
        request=request,
        org_id=current_user.organization_id,
    )
    # ZT-DAT-01 request-received acknowledgment (Critical; non-suppressible).
    # Background task — a mail failure can never block the request. Carries only
    # reference / type / status / due date (no request details or personal data).
    background_tasks.add_task(
        send_data_request_received,
        current_user.email, current_user.name or "", row.id,
        f"Data access request ({body.type})", "submitted",
        (now + timedelta(days=30)).isoformat(),
    )
    return {
        "id":           row.id,
        "type":         body.type,
        "submitted_at": now.isoformat(),
        "due_at":       (now + timedelta(days=30)).isoformat(),
        "status":       "submitted",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. DELETION REQUESTS
# ─────────────────────────────────────────────────────────────────────────────
class DeletionIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=4000)


@router.post("/deletion", status_code=201)
def submit_deletion(
    body: DeletionIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    row = _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="DataDeletionRequest",
        action="DataDeletionRequested",
        payload={
            "reason": body.reason,
            "status": "submitted",
        },
        request=request,
        org_id=current_user.organization_id,
    )
    # ZT-DAT-01 request-received acknowledgment (Critical; non-suppressible).
    # Background task — a mail failure can never block the request. Carries only
    # reference / type / status (no request details or personal data).
    background_tasks.add_task(
        send_data_request_received,
        current_user.email, current_user.name or "", row.id,
        "Data deletion", "submitted", None,
    )
    return {
        "id":           row.id,
        "submitted_at": row.created_at.isoformat() if row.created_at else None,
        "status":       "submitted",
        "message":      "Your deletion request has been logged for review. You will receive a response listing what was deleted and what was retained, with the legal basis.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. POLICY ACKNOWLEDGEMENTS
# ─────────────────────────────────────────────────────────────────────────────
class PolicyAckIn(BaseModel):
    policy_id:      str = Field(..., max_length=128)
    policy_version: str = Field(..., max_length=32)


@router.get("/policy-acks")
def list_policy_acks(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Return acknowledgements for this user's workspace policies.
    v1: returns a curated list, marking each as Acknowledged or Required based
    on AuditLog rows of action=PolicyAcknowledged.
    """
    acked_rows = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == current_user.id,
            models.AuditLog.entity == "Policy",
            models.AuditLog.action == "PolicyAcknowledged",
        )
        .all()
    )
    acked = {
        ((r.payload or {}).get("policy_id"), (r.payload or {}).get("policy_version")): r.created_at
        for r in acked_rows
    }

    base = [
        ("workforce_monitoring", "Workforce Monitoring Policy", "3.2"),
        ("privacy_notice",       "Privacy Notice",              "2.1"),
        ("screenshot_policy",    "Screenshot Policy",           "1.4"),
        ("data_retention",       "Data Retention Policy",       "2.0"),
    ]
    out = []
    for pid, name, version in base:
        ack_at = acked.get((pid, version))
        out.append({
            "policy_id":          pid,
            "policy_name":        name,
            "policy_version":     version,
            "acknowledged_at":    ack_at.isoformat() if ack_at else None,
            "acknowledgement_method": "click_through" if ack_at else None,
        })
    return out


@router.post("/policy-acks", status_code=201)
def acknowledge_policy(
    body: PolicyAckIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _audit(
        db,
        actor_id=current_user.id,
        target_id=current_user.id,
        entity="Policy",
        action="PolicyAcknowledged",
        payload={
            "policy_id":      body.policy_id,
            "policy_version": body.policy_version,
            "method":         "click_through",
        },
        request=request,
        org_id=current_user.organization_id,
    )
    return {"acknowledged": True, "policy_id": body.policy_id, "policy_version": body.policy_version}
