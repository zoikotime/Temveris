"""
Temveris - Async Validation Pipeline
======================================
Flow:
  1. User submits work evidence  → POST /pow/records
  2. Record saved immediately with status="pending"
  3. Background task triggers validation engine (WAE)
  4. WAE scores record, creates fraud events, updates status

Uses FastAPI BackgroundTasks initially.
Designed for future migration to Redis/Celery queue.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from database import models
from database.database import SessionLocal
from . import validation_service


def compute_submission_hash(
    user_id: int,
    timestamp: datetime,
    device_id: Optional[str],
    artifact_url: Optional[str],
) -> str:
    """
    Replay-prevention fingerprint for an incoming work submission.

    Unlike the tamper-evident chain hash (which links each record to its
    predecessor), this signature uniquely identifies the submission itself,
    so the WAE can detect when the exact same evidence bundle is re-POSTed
    (classic replay attack or desktop-tracker retry storm).
    """
    seed = "|".join([
        str(user_id),
        timestamp.isoformat() if timestamp else "",
        device_id or "",
        artifact_url or "",
    ])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()

logger = logging.getLogger("zoiko.pipeline")


def create_work_record(
    db: Session,
    user_id: int,
    timestamp: datetime,
    duration: int = 0,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    device_id: Optional[str] = None,
    task_id: Optional[str] = None,
    artifact_url: Optional[str] = None,
    artifact_hash: Optional[str] = None,
    keyboard_activity: int = 0,
    mouse_activity: int = 0,
    policy_context: Optional[Dict[str, Any]] = None,
    session_id: Optional[int] = None,
) -> models.WorkRecord:
    """
    Create a new WorkRecord with status=pending and a valid hash chain link.
    The caller is responsible for calling db.commit().
    Returns the new record (not yet validated).
    """
    record_id = str(uuid.uuid4())
    prev_hash = validation_service.get_previous_hash(db, user_id)

    payload = {
        "duration": duration,
        "device_id": device_id or "",
        "artifact_hash": artifact_hash or "",
        "keyboard_activity": keyboard_activity,
        "mouse_activity": mouse_activity,
    }
    current_hash = validation_service.compute_record_hash(
        record_id,
        user_id,
        timestamp.isoformat(),
        prev_hash,
        payload,
    )

    # Compute the replay-prevention fingerprint and stash it into policy_context
    # (using the existing JSON column avoids a schema migration). Downstream
    # validation then looks this up via the JSON operator to catch exact replays.
    submission_hash = compute_submission_hash(user_id, timestamp, device_id, artifact_url)
    merged_ctx: Dict[str, Any] = dict(policy_context or {})
    merged_ctx["submission_hash"] = submission_hash

    record = models.WorkRecord(
        record_id=record_id,
        user_id=user_id,
        session_id=session_id,
        timestamp=timestamp,
        duration=duration,
        latitude=latitude,
        longitude=longitude,
        device_id=device_id,
        task_id=task_id,
        artifact_url=artifact_url,
        artifact_hash=artifact_hash,
        keyboard_activity=keyboard_activity,
        mouse_activity=mouse_activity,
        policy_context=merged_ctx,
        validation_status="pending",
        confidence_score=100,
        version=1,
        parent_record_id=None,
        previous_hash=prev_hash,
        current_hash=current_hash,
    )
    db.add(record)
    return record


def run_validation_background(record_id: str) -> None:
    """
    Background task: opens its own DB session, runs the WAE pipeline,
    then closes the session. Designed for FastAPI BackgroundTasks.

    Future: replace with Celery task or Redis queue consumer.
    """
    db = SessionLocal()
    try:
        result = validation_service.validate_work_record(db, record_id)
        logger.info(
            "Validated %s → score=%d status=%s fraud_events=%d",
            record_id,
            result["confidence_score"],
            result["validation_status"],
            result["fraud_events_created"],
        )
    except Exception:
        logger.exception("Validation failed for record %s", record_id)
        # Mark as pending so it can be retried
        record = (
            db.query(models.WorkRecord)
            .filter(models.WorkRecord.record_id == record_id)
            .first()
        )
        if record and record.validation_status == "pending":
            record.validation_status = "pending"
            db.commit()
    finally:
        db.close()
