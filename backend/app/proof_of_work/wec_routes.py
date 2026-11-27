"""
Temveris - Proof-of-Work: Work Evidence Routes
================================================
POST /pow/records           – Submit work evidence (triggers async validation)
GET  /pow/records/{id}      – Fetch single record with score
GET  /pow/records/user/{uid} – List all records for a user
POST /pow/records/{id}/validate – Manually re-trigger validation
"""

from fastapi import APIRouter, Depends, Query, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date, datetime, timezone

from database.database import get_db
from database import models
from app.auth.service import get_current_user

from . import pipeline, validation_service
from .schemas import (
    CreateWorkRecordRequest,
    WorkRecordResponse,
    WorkRecordCreatedResponse,
    ValidationResultResponse,
)

router = APIRouter(prefix="/pow", tags=["Proof of Work"])


# ══════════════════════════════════════════════
# POST /pow/records  –  Create work evidence
# ══════════════════════════════════════════════
@router.post("/records", response_model=WorkRecordCreatedResponse)
def create_record(
    payload: CreateWorkRecordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """
    Submit a new work evidence record.
    Record is saved immediately (status=pending), then validated asynchronously.
    """
    record = pipeline.create_work_record(
        db=db,
        user_id=user.id,
        timestamp=payload.timestamp,
        duration=payload.duration,
        latitude=payload.latitude,
        longitude=payload.longitude,
        device_id=payload.device_id,
        task_id=payload.task_id,
        artifact_url=payload.artifact_url,
        artifact_hash=payload.artifact_hash,
        keyboard_activity=payload.keyboard_activity,
        mouse_activity=payload.mouse_activity,
        policy_context=payload.policy_context,
        session_id=payload.session_id,
    )
    db.commit()

    # Trigger background validation
    background_tasks.add_task(pipeline.run_validation_background, record.record_id)

    return WorkRecordCreatedResponse(
        record_id=record.record_id,
        current_hash=record.current_hash,
        validation_status=record.validation_status,
    )


# ══════════════════════════════════════════════
# GET /pow/records/{record_id}  –  Fetch one record
# ══════════════════════════════════════════════
@router.get("/records/{record_id}", response_model=WorkRecordResponse)
def get_record(
    record_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Fetch a single work evidence record with its score and status."""
    record = (
        db.query(models.WorkRecord)
        .filter(models.WorkRecord.record_id == record_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


# ══════════════════════════════════════════════
# GET /pow/records/user/{user_id}  –  List user records
# ══════════════════════════════════════════════
@router.get("/records/user/{user_id}")
def list_user_records(
    user_id: int,
    status: Optional[str] = Query(None, description="Filter: pending|validated|warning|flagged"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """List all work evidence records for a user, with optional filters."""
    q = (
        db.query(models.WorkRecord)
        .filter(models.WorkRecord.user_id == user_id)
        .order_by(models.WorkRecord.timestamp.desc())
    )

    if status:
        q = q.filter(models.WorkRecord.validation_status == status)
    if date_from:
        q = q.filter(models.WorkRecord.timestamp >= datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc))
    if date_to:
        from datetime import timedelta
        q = q.filter(models.WorkRecord.timestamp < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))

    total = q.count()
    records = q.offset(skip).limit(limit).all()

    return {
        "total": total,
        "records": [
            {
                "record_id": r.record_id,
                "user_id": r.user_id,
                "timestamp": r.timestamp.isoformat(),
                "duration": r.duration,
                "device_id": r.device_id,
                "artifact_url": r.artifact_url,
                "validation_status": r.validation_status,
                "confidence_score": r.confidence_score,
                "keyboard_activity": r.keyboard_activity,
                "mouse_activity": r.mouse_activity,
                "version": r.version,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ],
    }


# ══════════════════════════════════════════════
# GET /pow/records/user/{uid}/windows  –  Aggregated work-authenticity windows
# ══════════════════════════════════════════════
#
# Why a separate aggregated endpoint:
#   The desktop tracker creates one WorkRecord per screenshot (every 30s by
#   default). Judging each 30-second sample in isolation produces a TON of
#   "warning"/"flagged" rows for normal work patterns — e.g. a worker
#   reading a long doc registers low keyboard activity for 60 seconds and
#   gets a flagged record, even though they're actively working.
#
#   This endpoint rolls those 30s samples into 15-minute "authenticity
#   windows". A window is only flagged when activity is sustained-low
#   across the FULL window (≈ system genuinely idle for 10–15 min), which
#   matches the policy intent. The per-screenshot WorkRecord stays
#   authoritative for fraud/replay checks; this is purely a display layer.
#
# Aggregation rules (per window):
#   keyboard_total = sum of keyboard_activity across samples
#   mouse_total    = sum of mouse_activity across samples
#   sample_count   = how many WorkRecords landed in the window
#   avg_score      = mean confidence_score
#   validation_status (the displayed badge) is computed from totals:
#     • flagged   if keyboard_total < 30  AND mouse_total < 15
#                 (≈ < 1 keystroke per 30s AND < 1 click per minute,
#                 sustained for the full 15 min)
#     • warning   if keyboard_total < 100 OR mouse_total < 50
#     • validated otherwise
#   These thresholds correspond roughly to "system idle for 10–15 min"
#   per the workforce policy spec, and dramatically reduce false-positive
#   flags from short reading/thinking pauses inside an active session.
# ══════════════════════════════════════════════
from datetime import timedelta as _timedelta

_DEFAULT_WINDOW_MINUTES = 15
_FLAG_KEYBOARD_TOTAL    = 30
_FLAG_MOUSE_TOTAL       = 15
_WARN_KEYBOARD_TOTAL    = 100
_WARN_MOUSE_TOTAL       = 50


def _classify_window(kb_total: int, mouse_total: int) -> str:
    """Translate per-window aggregate activity into a status badge."""
    if kb_total < _FLAG_KEYBOARD_TOTAL and mouse_total < _FLAG_MOUSE_TOTAL:
        return "flagged"
    if kb_total < _WARN_KEYBOARD_TOTAL or mouse_total < _WARN_MOUSE_TOTAL:
        return "warning"
    return "validated"


@router.get("/records/user/{user_id}/windows")
def list_user_record_windows(
    user_id: int,
    window_minutes: int = Query(_DEFAULT_WINDOW_MINUTES, ge=5, le=60,
                                 description="Aggregation bucket size in minutes"),
    limit:          int = Query(48, ge=1, le=200,
                                 description="How many recent windows to return"),
    date_from: Optional[date] = Query(None),
    date_to:   Optional[date] = Query(None),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Return per-employee work authenticity rolled up into N-minute windows.

    Authorisation: caller must be the worker themselves OR an admin/HR.
    """
    if user.id != user_id and user.role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Cannot read another user's records")

    # Pull enough back-history that `limit` windows can usually be filled.
    # Each window is `window_minutes`, so request at most window_minutes *
    # limit minutes back. Bound the SQL fetch generously so a quiet day
    # still returns the requested number of windows.
    horizon_minutes = max(window_minutes * limit, 60 * 24)
    horizon_start   = datetime.now(timezone.utc) - _timedelta(minutes=horizon_minutes)

    q = (
        db.query(models.WorkRecord)
        .filter(models.WorkRecord.user_id == user_id)
        .filter(models.WorkRecord.timestamp >= horizon_start)
    )
    if date_from:
        q = q.filter(models.WorkRecord.timestamp >= datetime.combine(
            date_from, datetime.min.time(), tzinfo=timezone.utc))
    if date_to:
        q = q.filter(models.WorkRecord.timestamp < datetime.combine(
            date_to + _timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    q = q.order_by(models.WorkRecord.timestamp.desc())

    rows = q.all()

    # Bucket by floor(timestamp / window_minutes). Using a single integer
    # key keeps the dict tight even for thousands of rows. Anchored to
    # epoch UTC so consecutive identical windows match across server
    # restarts and timezone changes don't shift bucket boundaries.
    win_seconds = window_minutes * 60
    buckets: dict = {}
    for r in rows:
        ts = r.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bucket_idx = int(ts.timestamp() // win_seconds)
        b = buckets.get(bucket_idx)
        if b is None:
            bucket_start = datetime.fromtimestamp(bucket_idx * win_seconds, tz=timezone.utc)
            b = {
                "bucket_idx":     bucket_idx,
                # Floor-aligned boundary (kept for the analytical axis).
                "bucket_start":   bucket_start,
                "bucket_end":     bucket_start + _timedelta(seconds=win_seconds),
                # First / last *actual* activity timestamps inside this bucket.
                # The UI displays these as the window's start/end so an admin
                # never sees "activity started 09:00" for a worker whose first
                # screenshot landed at 09:01 (a wall-clock rounding artifact of
                # the old behaviour that was being read as a clock-skew bug
                # and falsely flagging legitimate clock-ins).
                "first_ts":       ts,
                "last_ts":        ts,
                "sample_count":   0,
                "duration_total": 0,
                "keyboard_total": 0,
                "mouse_total":    0,
                "score_sum":      0,
                "score_max":      0,
                "first_record":   None,
                "last_record":    None,
                "any_pending":    False,
            }
            buckets[bucket_idx] = b
        # Track the true min/max timestamps observed within the bucket.
        if ts < b["first_ts"]:
            b["first_ts"] = ts
        if ts > b["last_ts"]:
            b["last_ts"] = ts
        b["sample_count"]   += 1
        b["duration_total"] += int(r.duration or 0)
        b["keyboard_total"] += int(r.keyboard_activity or 0)
        b["mouse_total"]    += int(r.mouse_activity or 0)
        b["score_sum"]      += int(r.confidence_score or 0)
        b["score_max"]       = max(b["score_max"], int(r.confidence_score or 0))
        if r.validation_status == "pending":
            b["any_pending"] = True
        # Track newest record id for drill-down — rows are already sorted
        # newest-first, so the first hit is the latest.
        if b["last_record"] is None:
            b["last_record"] = r.record_id
        b["first_record"] = r.record_id

    # Sort newest first, take `limit`, then build the response payload.
    sorted_buckets = sorted(buckets.values(), key=lambda x: x["bucket_idx"], reverse=True)[:limit]

    windows = []
    counts = {"validated": 0, "warning": 0, "flagged": 0, "pending": 0}
    score_total = 0
    for b in sorted_buckets:
        # Pending dominates: if ANY sample in the window is still mid-
        # validation we surface "pending" rather than guess. Otherwise
        # use the activity-aggregate classification.
        if b["any_pending"]:
            status = "pending"
        else:
            status = _classify_window(b["keyboard_total"], b["mouse_total"])
        avg_score = round(b["score_sum"] / max(1, b["sample_count"]))
        counts[status] = counts.get(status, 0) + 1
        score_total += avg_score
        windows.append({
            # `window_start` / `window_end` now reflect ACTUAL first/last
            # activity timestamps inside the bucket — what the UI shows as
            # "session started at". The floor-aligned bucket boundary is
            # available separately as `bucket_start` / `bucket_end` for
            # callers that need it for an aligned timeline.
            "window_start":      b["first_ts"].isoformat(),
            "window_end":        b["last_ts"].isoformat(),
            "bucket_start":      b["bucket_start"].isoformat(),
            "bucket_end":        b["bucket_end"].isoformat(),
            "sample_count":      b["sample_count"],
            "duration_seconds":  b["duration_total"],
            "keyboard_total":    b["keyboard_total"],
            "mouse_total":       b["mouse_total"],
            "avg_score":         avg_score,
            "max_score":         b["score_max"],
            "validation_status": status,
            "first_record_id":   b["first_record"],
            "last_record_id":    b["last_record"],
        })

    total = len(windows)
    return {
        "window_minutes": window_minutes,
        "total":          total,
        "stats": {
            "total":     total,
            "validated": counts.get("validated", 0),
            "warning":   counts.get("warning", 0),
            "flagged":   counts.get("flagged", 0),
            "pending":   counts.get("pending", 0),
            "avg":       round(score_total / total) if total else 0,
        },
        "thresholds": {
            "flag_keyboard_total": _FLAG_KEYBOARD_TOTAL,
            "flag_mouse_total":    _FLAG_MOUSE_TOTAL,
            "warn_keyboard_total": _WARN_KEYBOARD_TOTAL,
            "warn_mouse_total":    _WARN_MOUSE_TOTAL,
        },
        "windows": windows,
    }


# ══════════════════════════════════════════════
# POST /pow/records/{record_id}/validate  –  Re-validate
# ══════════════════════════════════════════════
@router.post("/records/{record_id}/validate", response_model=ValidationResultResponse)
def validate_record(
    record_id: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Manually trigger (re-)validation for a record. Returns full result synchronously."""
    record = (
        db.query(models.WorkRecord)
        .filter(models.WorkRecord.record_id == record_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    # Only admins can re-validate other users' records
    if record.user_id != user.id and user.role not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Not authorized to validate this record")

    result = validation_service.validate_work_record(db, record_id)
    return result


# ══════════════════════════════════════════════
# GET /pow/dashboard  –  Summary dashboard for admins
# ══════════════════════════════════════════════
@router.get("/dashboard")
def pow_dashboard(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Admin dashboard: aggregate stats across all work records."""
    from sqlalchemy import func

    total = db.query(func.count(models.WorkRecord.id)).scalar() or 0
    validated = db.query(func.count(models.WorkRecord.id)).filter(
        models.WorkRecord.validation_status == "validated"
    ).scalar() or 0
    warnings = db.query(func.count(models.WorkRecord.id)).filter(
        models.WorkRecord.validation_status == "warning"
    ).scalar() or 0
    flagged = db.query(func.count(models.WorkRecord.id)).filter(
        models.WorkRecord.validation_status == "flagged"
    ).scalar() or 0
    pending = db.query(func.count(models.WorkRecord.id)).filter(
        models.WorkRecord.validation_status == "pending"
    ).scalar() or 0

    avg_score = db.query(func.avg(models.WorkRecord.confidence_score)).scalar()

    fraud_count = db.query(func.count(models.FraudEvent.id)).scalar() or 0

    return {
        "total_records": total,
        "validated": validated,
        "warnings": warnings,
        "flagged": flagged,
        "pending": pending,
        "average_confidence_score": round(avg_score, 1) if avg_score else 0,
        "total_fraud_events": fraud_count,
    }
