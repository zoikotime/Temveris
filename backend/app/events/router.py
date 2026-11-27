"""
Temveris – Event Bus Router
============================
Endpoints:
  POST /events   – Ingest a versioned work event; idempotency-safe
  GET  /events   – List ingested work events

Supported event_type values:
  USER_LOGIN | USER_LOGOUT
  BREAK_STARTED | BREAK_ENDED
  ACTIVITY_CAPTURED | SCREENSHOT_CAPTURED
  CLOCK_IN | CLOCK_OUT
  (any custom type is accepted and stored; WEC entry is only created for known types)
"""

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Dict, Any

from database.database import get_db
from database import models
from app.auth.service import get_current_user
from app.wec import service as wec_service

router = APIRouter(prefix="/events", tags=["Event Bus"])

# Mapping from event_type → WEC action_type
# Only events in this map produce a WEC chain entry.
_EVENT_TO_WEC_ACTION: Dict[str, str] = {
    "USER_LOGIN":          "login",
    "USER_LOGOUT":         "logout",
    "CLOCK_IN":            "login",
    "CLOCK_OUT":           "logout",
    "BREAK_STARTED":       "break_started",
    "BREAK_ENDED":         "break_ended",
    "ACTIVITY_CAPTURED":   "activity",
    "SCREENSHOT_CAPTURED": "screenshot",
}


# ══════════════════════════════════════════════
# POST /events
# ══════════════════════════════════════════════
class EventPayload(BaseModel):
    event_type:      str
    event_version:   str                    = "1.0"
    session_id:      Optional[int]          = None
    payload:         Dict[str, Any]         = {}
    idempotency_key: Optional[str]          = None


@router.post("")
def ingest_event(
    body:    EventPayload,
    request: Request,
    db:      Session     = Depends(get_db),
    user:    models.User = Depends(get_current_user),
):
    """
    Ingest a work event.
    - Idempotency: if idempotency_key already exists, returns the existing event.
    - WEC entry: created for known event types (see _EVENT_TO_WEC_ACTION map).
    - Device info (IP, user-agent) is captured and stored in the WEC payload.
    """
    # ── Idempotency guard ──────────────────────
    if body.idempotency_key:
        existing = (
            db.query(models.WorkEvent)
            .filter(models.WorkEvent.idempotency_key == body.idempotency_key)
            .first()
        )
        if existing:
            return {
                "success":        True,
                "event_id":       existing.event_id,
                "duplicate":      True,
                "wec_record_id":  existing.wec_id,
            }

    device_info = {
        "ip_address": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown"),
    }

    # ── WEC entry (only for mapped event types) ─
    wec_action = _EVENT_TO_WEC_ACTION.get(body.event_type)
    wec_entry  = None

    if wec_action:
        wec_entry = wec_service.create_wec_entry(
            db,
            user_id         = user.id,
            organization_id = user.organization_id,
            action_type     = wec_action,
            session_id      = body.session_id,
            device_info     = device_info,
            payload         = body.payload,
        )

    # ── Emit versioned event ────────────────────
    event = wec_service.emit_work_event(
        db,
        event_type      = body.event_type,
        user_id         = user.id,
        organization_id = user.organization_id,
        session_id      = body.session_id,
        payload         = body.payload,
        idempotency_key = body.idempotency_key,
        wec_id          = wec_entry.work_record_id if wec_entry else None,
    )

    db.commit()

    return {
        "success":       True,
        "event_id":      event.event_id,
        "duplicate":     False,
        "wec_record_id": wec_entry.work_record_id if wec_entry else None,
        "current_hash":  wec_entry.current_hash   if wec_entry else None,
    }


# ══════════════════════════════════════════════
# GET /events
# ══════════════════════════════════════════════
@router.get("")
def list_events(
    user_id:    Optional[int] = Query(None),
    event_type: Optional[str] = Query(None),
    limit:      int           = Query(100, ge=1, le=500),
    skip:       int           = Query(0,   ge=0),
    db:         Session       = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """List work events. Supports filtering by user_id and event_type."""
    org_id = current_user.organization_id
    org_user_ids = [
        uid for (uid,) in db.query(models.User.id).filter(
            models.User.organization_id == org_id
        ).all()
    ]

    if user_id is not None:
        if user_id not in org_user_ids:
            raise HTTPException(status_code=404, detail="User not found")

    q = db.query(models.WorkEvent).filter(
        models.WorkEvent.user_id.in_(org_user_ids)
    ).order_by(models.WorkEvent.created_at.desc())

    if user_id is not None:
        q = q.filter(models.WorkEvent.user_id == user_id)
    if event_type:
        q = q.filter(models.WorkEvent.event_type == event_type)

    events = q.offset(skip).limit(limit).all()
    return [
        {
            "event_id":      e.event_id,
            "event_type":    e.event_type,
            "event_version": e.event_version,
            "user_id":       e.user_id,
            "session_id":    e.session_id,
            "payload":       e.payload,
            "processed":     e.processed,
            "wec_id":        e.wec_id,
            "created_at":    e.created_at.isoformat(),
        }
        for e in events
    ]
