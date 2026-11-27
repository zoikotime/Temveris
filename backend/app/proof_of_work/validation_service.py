"""
Temveris - Work Authenticity Engine (WAE)
==========================================
Validates whether a WorkRecord represents genuine work.

Pipeline:
  1. Metadata Verification   – artifact_hash exists & screenshot integrity
  2. Behavior Analysis        – keyboard/mouse activity thresholds
  3. Device Validation        – known vs. unknown device
  4. Location Validation      – present & plausible coordinates
  5. Replay Detection         – duplicate artifact_hash across records
  6. Tamper Detection         – hash chain integrity check

Each check produces fraud events when violations are found.
The scoring engine is called at the end to set confidence_score + validation_status.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from database import models
from database.models import ValidationStatus, FraudSeverity
from . import scoring_service


# Replay scan window. A 24-hour lookback is enough to catch every realistic
# replay attempt (network retries, malicious resubmission, stale queue flush)
# while keeping the scan cheap — one user rarely generates >3k records/day.
_REPLAY_WINDOW = timedelta(hours=24)

logger = logging.getLogger("zoiko.wae")


# ──────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────

def validate_work_record(
    db: Session,
    record_id: str,
) -> dict:
    """
    Full validation pipeline for a single WorkRecord.
    Returns a dict with score, status, deductions, and fraud_events created.
    """
    record = (
        db.query(models.WorkRecord)
        .filter(models.WorkRecord.record_id == record_id)
        .first()
    )
    if not record:
        raise ValueError(f"WorkRecord not found: {record_id}")

    fraud_events: List[dict] = []

    # --- Run all validation checks ---
    _verify_metadata(db, record, fraud_events)
    _analyze_behavior(record, fraud_events)
    _validate_device(db, record, fraud_events)
    _validate_location(record, fraud_events)
    _detect_replay(db, record, fraud_events)
    _detect_tamper(db, record, fraud_events)

    # --- Persist fraud events ---
    for fe in fraud_events:
        db.add(models.FraudEvent(
            wec_id=record.record_id,
            reason=fe["reason"],
            severity=fe["severity"],
            details=fe.get("details"),
        ))

    # --- Run scoring engine ---
    score_result = scoring_service.compute_confidence(db, record)

    # --- Update record ---
    record.confidence_score = score_result.score
    record.validation_status = score_result.status

    # --- Summary fraud alert: triggered when record is flagged or score < 50 ---
    # This is the high-level "risk" alert consumed by the admin dashboard.
    # It is separate from the per-check fraud_events emitted above so that the
    # admin UI can show one alert per risky record instead of 6 technical rows.
    summary_alert = None
    if score_result.status == "flagged" or score_result.score < 50:
        kb = int(record.keyboard_activity or 0)
        ms = int(record.mouse_activity or 0)

        if kb == 0 and ms == 0:
            alert_reason = "No activity detected — possible idle or fake work"
            alert_category = "no_activity"
        elif kb < 10 and ms < 5:
            alert_reason = f"Very low activity (kb={kb}, mouse={ms}) — likely idle"
            alert_category = "low_activity"
        elif kb <= 50 or ms <= 20:
            alert_reason = f"Low activity (kb={kb}, mouse={ms}) below expected levels"
            alert_category = "low_activity"
        else:
            alert_reason = "Suspicious work record — multiple validation checks failed"
            alert_category = "suspicious"

        summary_alert = models.FraudEvent(
            wec_id=record.record_id,
            reason=alert_reason,
            severity=FraudSeverity.HIGH,
            details={
                "check": "summary_alert",
                "category": alert_category,
                "user_id": record.user_id,
                "record_id": record.record_id,
                "confidence_score": score_result.score,
                "validation_status": score_result.status,
                "keyboard_activity": kb,
                "mouse_activity": ms,
                "timestamp": (record.timestamp or datetime.now(timezone.utc)).isoformat(),
            },
        )
        db.add(summary_alert)
        fraud_events.append({
            "reason": alert_reason,
            "severity": FraudSeverity.HIGH,
            "details": summary_alert.details,
        })

    db.commit()

    return {
        "record_id": record.record_id,
        "confidence_score": score_result.score,
        "validation_status": score_result.status,
        "deductions": score_result.deductions,
        "fraud_events_created": len(fraud_events),
        "fraud_events": fraud_events,
        "summary_alert": bool(summary_alert),
    }


# ──────────────────────────────────────────────
# 1. METADATA VERIFICATION
# ──────────────────────────────────────────────

def _verify_metadata(
    db: Session,
    record: models.WorkRecord,
    fraud_events: List[dict],
) -> None:
    """
    Ensure the record carries verifiable artifact metadata.
    Missing hash / URL is treated as a tampered_data signal because a genuine
    desktop-tracker submission always includes both.
    """
    if not record.artifact_hash:
        fraud_events.append({
            "reason": "tampered_data: missing artifact hash — no verifiable screenshot evidence",
            "severity": FraudSeverity.MEDIUM,
            "details": {
                "check": "metadata_verification",
                "field": "artifact_hash",
                "category": "tampered_data",
            },
        })
        return

    if not record.artifact_url:
        fraud_events.append({
            "reason": "tampered_data: artifact hash present but artifact URL missing — possible data corruption",
            "severity": FraudSeverity.LOW,
            "details": {
                "check": "metadata_verification",
                "field": "artifact_url",
                "category": "tampered_data",
            },
        })


# ──────────────────────────────────────────────
# 2. BEHAVIOR ANALYSIS
# ──────────────────────────────────────────────

def _analyze_behavior(
    record: models.WorkRecord,
    fraud_events: List[dict],
) -> None:
    """Flag suspiciously low keyboard/mouse activity."""
    kb = record.keyboard_activity or 0
    ms = record.mouse_activity or 0
    total = kb + ms

    if total == 0 and (record.duration or 0) > 60:
        fraud_events.append({
            "reason": "Zero keyboard and mouse activity during non-trivial work period",
            "severity": FraudSeverity.HIGH,
            "details": {
                "check": "behavior_analysis",
                "keyboard": kb,
                "mouse": ms,
                "duration_seconds": record.duration,
            },
        })
    elif total < 5 and (record.duration or 0) > 300:
        fraud_events.append({
            "reason": f"Very low activity (total={total}) over {record.duration}s work period",
            "severity": FraudSeverity.MEDIUM,
            "details": {
                "check": "behavior_analysis",
                "keyboard": kb,
                "mouse": ms,
                "duration_seconds": record.duration,
            },
        })


# ──────────────────────────────────────────────
# 3. DEVICE VALIDATION
# ──────────────────────────────────────────────

def _validate_device(
    db: Session,
    record: models.WorkRecord,
    fraud_events: List[dict],
) -> None:
    """Flag missing device or first-time-seen device."""
    if not record.device_id:
        fraud_events.append({
            "reason": "No device identifier - cannot verify work origin",
            "severity": FraudSeverity.MEDIUM,
            "details": {"check": "device_validation"},
        })
        return

    prior_count = (
        db.query(models.WorkRecord)
        .filter(
            models.WorkRecord.user_id == record.user_id,
            models.WorkRecord.device_id == record.device_id,
            models.WorkRecord.id != record.id,
        )
        .count()
    )
    if prior_count == 0:
        fraud_events.append({
            "reason": f"New/unknown device detected: {record.device_id}",
            "severity": FraudSeverity.LOW,
            "details": {
                "check": "device_validation",
                "device_id": record.device_id,
                "prior_records": 0,
            },
        })


# ──────────────────────────────────────────────
# 4. LOCATION VALIDATION
# ──────────────────────────────────────────────

def _validate_location(
    record: models.WorkRecord,
    fraud_events: List[dict],
) -> None:
    """Flag missing or impossible coordinates."""
    lat, lon = record.latitude, record.longitude

    if lat is None or lon is None:
        fraud_events.append({
            "reason": "No location data provided with work evidence",
            "severity": FraudSeverity.LOW,
            "details": {"check": "location_validation"},
        })
        return

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        fraud_events.append({
            "reason": f"Invalid GPS coordinates: ({lat}, {lon})",
            "severity": FraudSeverity.HIGH,
            "details": {
                "check": "location_validation",
                "latitude": lat,
                "longitude": lon,
            },
        })


# ──────────────────────────────────────────────
# 5. REPLAY DETECTION
# ──────────────────────────────────────────────

def _detect_replay(
    db: Session,
    record: models.WorkRecord,
    fraud_events: List[dict],
) -> None:
    """
    Two complementary replay checks:

      (a) Submission replay — SHA256(user+timestamp+device+url) already seen.
          This catches a verbatim re-POST of a prior submission (network retry
          or deliberate replay). Looked up via policy_context.submission_hash.

      (b) Artifact replay — the same screenshot hash is attached to a different
          record. This catches "reuse the same evidence file" attacks where
          the timestamp/device differ but the captured image is identical.
    """
    # ── (a) SUBMISSION REPLAY ─────────────────────────────────────────
    submission_hash = None
    if isinstance(record.policy_context, dict):
        submission_hash = record.policy_context.get("submission_hash")

    if submission_hash:
        window_start = None
        if record.timestamp:
            window_start = record.timestamp - _REPLAY_WINDOW

        q = db.query(models.WorkRecord).filter(
            models.WorkRecord.user_id == record.user_id,
            models.WorkRecord.id != record.id,
        )
        if window_start:
            q = q.filter(models.WorkRecord.timestamp >= window_start)

        # Scan in Python — JSON column operators vary across DB backends and
        # the candidate set for a single user in 24h is small (<3k rows).
        for prior in q.all():
            prior_ctx = prior.policy_context if isinstance(prior.policy_context, dict) else {}
            if prior_ctx.get("submission_hash") == submission_hash:
                fraud_events.append({
                    "reason": (
                        f"replay_attack: duplicate/replay detected — identical submission "
                        f"signature already exists (record {prior.record_id})"
                    ),
                    "severity": FraudSeverity.CRITICAL,
                    "details": {
                        "check": "replay_detection",
                        "category": "replay_attack",
                        "replay_type": "submission_hash",
                        "submission_hash": submission_hash,
                        "duplicate_record_id": prior.record_id,
                        "duplicate_user_id": prior.user_id,
                    },
                })
                break

    # ── (b) ARTIFACT REPLAY (same screenshot reused) ──────────────────
    if not record.artifact_hash:
        return

    duplicate = (
        db.query(models.WorkRecord)
        .filter(
            models.WorkRecord.artifact_hash == record.artifact_hash,
            models.WorkRecord.id != record.id,
        )
        .first()
    )
    if duplicate:
        fraud_events.append({
            "reason": (
                f"tampered_data: duplicate artifact hash — same screenshot reused in "
                f"record {duplicate.record_id} by user {duplicate.user_id}"
            ),
            "severity": FraudSeverity.CRITICAL,
            "details": {
                "check": "replay_detection",
                "category": "tampered_data",
                "replay_type": "artifact_hash",
                "artifact_hash": record.artifact_hash,
                "duplicate_record_id": duplicate.record_id,
                "duplicate_user_id": duplicate.user_id,
            },
        })


# ──────────────────────────────────────────────
# 6. TAMPER DETECTION
# ──────────────────────────────────────────────

def _detect_tamper(
    db: Session,
    record: models.WorkRecord,
    fraud_events: List[dict],
) -> None:
    """Verify the hash chain link is intact."""
    expected_hash = _recompute_hash(record)

    if record.current_hash != expected_hash:
        fraud_events.append({
            "reason": "Hash mismatch — record data may have been tampered with",
            "severity": FraudSeverity.CRITICAL,
            "details": {
                "check": "tamper_detection",
                "stored_hash": record.current_hash,
                "expected_hash": expected_hash,
            },
        })

    # Verify chain link
    if record.previous_hash is not None:
        predecessor = (
            db.query(models.WorkRecord)
            .filter(
                models.WorkRecord.user_id == record.user_id,
                models.WorkRecord.current_hash == record.previous_hash,
            )
            .first()
        )
        if not predecessor:
            fraud_events.append({
                "reason": "Broken hash chain — predecessor record not found",
                "severity": FraudSeverity.HIGH,
                "details": {
                    "check": "tamper_detection",
                    "expected_previous_hash": record.previous_hash,
                },
            })


# ──────────────────────────────────────────────
# HASH HELPERS
# ──────────────────────────────────────────────

def compute_record_hash(
    record_id: str,
    user_id: int,
    timestamp: str,
    previous_hash: Optional[str],
    payload: dict,
) -> str:
    """Deterministic SHA-256 hash for a WorkRecord."""
    raw = ":".join([
        record_id,
        str(user_id),
        timestamp,
        previous_hash or "",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _recompute_hash(record: models.WorkRecord) -> str:
    """Recompute the hash for an existing record to check integrity."""
    payload = {
        "duration": record.duration or 0,
        "device_id": record.device_id or "",
        "artifact_hash": record.artifact_hash or "",
        "keyboard_activity": record.keyboard_activity or 0,
        "mouse_activity": record.mouse_activity or 0,
    }
    return compute_record_hash(
        record.record_id,
        record.user_id,
        record.timestamp.isoformat() if record.timestamp else "",
        record.previous_hash,
        payload,
    )


def get_previous_hash(db: Session, user_id: int) -> Optional[str]:
    """Return current_hash of the most recent WorkRecord for this user."""
    last = (
        db.query(models.WorkRecord)
        .filter(models.WorkRecord.user_id == user_id)
        .order_by(models.WorkRecord.id.desc())
        .first()
    )
    return last.current_hash if last else None
