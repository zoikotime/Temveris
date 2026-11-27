"""
Temveris – Work Evidence Chain (WEC) Service
=============================================
Handles:
  * Hash-chained immutable audit records (WorkEvidenceChain)
  * Versioned, idempotent event emission (WorkEvent)
  * Per-session integrity scoring + fraud detection (IntegrityScore)
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session
from sqlalchemy import and_

from database import models


# ──────────────────────────────────────────────
# HASH CHAIN HELPERS
# ──────────────────────────────────────────────

def _compute_hash(
    record_id: str,
    user_id: int,
    timestamp: str,
    action_type: str,
    previous_hash: str,
    payload: dict,
) -> str:
    """SHA-256 hash over the core WEC fields to form a tamper-evident chain."""
    raw = ":".join([
        record_id,
        str(user_id),
        timestamp,
        action_type,
        previous_hash or "",
        json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_previous_hash(db: Session, user_id: int) -> Optional[str]:
    """Return the current_hash of the most recent WEC entry for this user."""
    last = (
        db.query(models.WorkEvidenceChain)
        .filter(models.WorkEvidenceChain.user_id == user_id)
        .order_by(models.WorkEvidenceChain.id.desc())
        .first()
    )
    return last.current_hash if last else None


# ──────────────────────────────────────────────
# WEC ENTRY CREATION
# ──────────────────────────────────────────────

def create_wec_entry(
    db: Session,
    user_id: int,
    action_type: str,
    session_id: Optional[int] = None,
    device_info: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    organization_id: Optional[int] = None,
) -> models.WorkEvidenceChain:
    """
    Create one append-only, hash-chained Work Evidence Chain record.
    The caller is responsible for calling db.commit() after this.
    """
    record_id    = str(uuid.uuid4())
    now          = datetime.now(timezone.utc)
    prev_hash    = _get_previous_hash(db, user_id)
    current_hash = _compute_hash(
        record_id, user_id, now.isoformat(), action_type, prev_hash or "", payload or {}
    )

    entry = models.WorkEvidenceChain(
        work_record_id    = record_id,
        user_id           = user_id,
        organization_id   = organization_id,
        session_id        = session_id,
        timestamp         = now,
        action_type       = action_type,
        device_info       = device_info or {},
        previous_hash     = prev_hash,
        current_hash      = current_hash,
        validation_status = "valid",
        integrity_score   = 1.0,
        payload           = payload or {},
    )
    db.add(entry)
    return entry


# ──────────────────────────────────────────────
# EVENT EMISSION
# ──────────────────────────────────────────────

def emit_work_event(
    db: Session,
    event_type: str,
    user_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    session_id: Optional[int] = None,
    payload: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    wec_id: Optional[str] = None,
) -> models.WorkEvent:
    """
    Emit a versioned work event.
    If idempotency_key is provided and already exists, returns the existing event (no duplicate).
    The caller is responsible for calling db.commit() after this.
    """
    if idempotency_key:
        existing = (
            db.query(models.WorkEvent)
            .filter(models.WorkEvent.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing

    event = models.WorkEvent(
        event_id        = str(uuid.uuid4()),
        event_type      = event_type,
        event_version   = "1.0",
        user_id         = user_id,
        organization_id = organization_id,
        session_id      = session_id,
        payload         = payload or {},
        idempotency_key = idempotency_key,
        processed       = True,
        wec_id          = wec_id,
    )
    db.add(event)
    return event


# ──────────────────────────────────────────────
# INTEGRITY ENGINE  (Fraud Detection)
# ──────────────────────────────────────────────

def compute_integrity_score(
    db: Session,
    user_id: int,
    session_id: int,
) -> Dict[str, Any]:
    """
    Analyse a clock session for fraud patterns and return an integrity score dict.
    Score: 1.0 = clean, 0.0 = high fraud risk.

    Checks:
      1. Break pattern anomalies (excessive or suspiciously short breaks)
      2. Activity gap detection (> 2 h silence while clocked in)
      3. Idle spoofing (active=True with zero input events)
      4. Time inflation (logged work time > wall-clock time)
    """
    anomalies: List[str] = []
    score          = 1.0
    idle_risk      = 0.0
    break_risk     = 0.0
    inflation_risk = 0.0
    gap_risk       = 0.0

    session = db.query(models.ClockSession).filter(
        models.ClockSession.id == session_id
    ).first()

    if not session:
        return {
            "score": 0.5,
            "anomaly_flags": ["Session not found"],
            "idle_spoofing_risk":  0.0,
            "fake_break_risk":    0.0,
            "time_inflation_risk": 0.0,
            "unusual_gaps_risk":  0.0,
        }

    # ── 1. Break pattern analysis ──────────────
    breaks = db.query(models.BreakSession).filter(
        and_(
            models.BreakSession.user_id    == user_id,
            models.BreakSession.session_id == session_id,
        )
    ).all()

    if len(breaks) > 6:
        anomalies.append(f"Excessive breaks: {len(breaks)} in one session (threshold: 6)")
        break_risk += 0.30
        score      -= 0.10

    for b in breaks:
        if b.break_end and b.break_start:
            dur_s = (b.break_end - b.break_start).total_seconds()
            if dur_s < 60:
                anomalies.append(f"Suspiciously short break detected ({int(dur_s)}s < 60s threshold)")
                break_risk += 0.20
                score      -= 0.05

    # ── 2. Activity gap + idle spoofing detection ──
    activities = (
        db.query(models.Activity)
        .filter(models.Activity.session_id == session_id)
        .order_by(models.Activity.timestamp.asc())
        .all()
    )

    if activities:
        prev_ts = None
        active_count      = 0
        zero_input_active = 0

        for act in activities:
            # Gap check
            if prev_ts is not None:
                gap_s = (act.timestamp - prev_ts).total_seconds()
                if gap_s > 7200:  # > 2 hours with no screenshot
                    anomalies.append(
                        f"Unusual activity gap: {int(gap_s / 60)} min with no activity recorded"
                    )
                    gap_risk += 0.30
                    score    -= 0.10
            prev_ts = act.timestamp

            # Idle spoofing check: marked active but zero clicks + keys
            if act.is_active:
                active_count += 1
                if (act.mouse_clicks or 0) == 0 and (act.key_presses or 0) == 0:
                    zero_input_active += 1

        if active_count > 0:
            spoof_ratio = zero_input_active / active_count
            if spoof_ratio > 0.50:
                anomalies.append(
                    f"Possible idle spoofing: {int(spoof_ratio*100)}% of 'active' records have zero input"
                )
                idle_risk += 0.40
                score     -= 0.15

    # ── 3. Time inflation check ─────────────────
    if session.clock_out_time and session.clock_in_time:
        wall_time_s  = (session.clock_out_time - session.clock_in_time).total_seconds()
        logged_work  = session.total_seconds or 0
        # total_seconds = wall_time - break_time, so it should never exceed wall_time
        if wall_time_s > 0 and logged_work > wall_time_s * 1.05:
            anomalies.append(
                f"Time inflation: logged {int(logged_work)}s > wall-clock {int(wall_time_s)}s"
            )
            inflation_risk += 0.50
            score          -= 0.20

    # ── Clamp score ─────────────────────────────
    score = max(0.0, min(1.0, round(score, 4)))

    return {
        "score":               score,
        "anomaly_flags":       anomalies,
        "idle_spoofing_risk":  min(1.0, round(idle_risk,      4)),
        "fake_break_risk":     min(1.0, round(break_risk,     4)),
        "time_inflation_risk": min(1.0, round(inflation_risk, 4)),
        "unusual_gaps_risk":   min(1.0, round(gap_risk,       4)),
    }


# ──────────────────────────────────────────────
# CHAIN VERIFICATION
# ──────────────────────────────────────────────

def verify_chain(db: Session, user_id: int) -> Dict[str, Any]:
    """
    Walk the entire WEC for a user and verify every hash link is intact.
    Returns a summary of chain integrity.
    """
    entries = (
        db.query(models.WorkEvidenceChain)
        .filter(models.WorkEvidenceChain.user_id == user_id)
        .order_by(models.WorkEvidenceChain.id.asc())
        .all()
    )

    if not entries:
        return {"valid": True, "total": 0, "broken_links": []}

    broken: List[Dict[str, Any]] = []
    prev_hash: Optional[str] = None

    for entry in entries:
        expected = _compute_hash(
            entry.work_record_id,
            entry.user_id,
            entry.timestamp.isoformat(),
            entry.action_type,
            entry.previous_hash or "",
            entry.payload or {},
        )
        # Verify stored hash matches recomputed hash
        if entry.current_hash != expected:
            broken.append({
                "work_record_id": entry.work_record_id,
                "reason": "hash_mismatch",
                "stored":   entry.current_hash,
                "expected": expected,
            })
        # Verify chain link (previous_hash pointer)
        if prev_hash is not None and entry.previous_hash != prev_hash:
            broken.append({
                "work_record_id": entry.work_record_id,
                "reason": "broken_chain_link",
                "expected_prev": prev_hash,
                "stored_prev":   entry.previous_hash,
            })
        prev_hash = entry.current_hash

    return {
        "valid":        len(broken) == 0,
        "total":        len(entries),
        "broken_links": broken,
    }
