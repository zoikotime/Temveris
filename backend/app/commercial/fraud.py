"""Fraud & Abuse service (§17).

Inputs: signup payload + client context (IP, UA hash, optional device fp).
Outputs: a FraudDecision with a score, contributing signals, and a
recommended action (allow / challenge / review / block).

Threshold values come from constants.py. The real fraud signals (device
fingerprint clustering, ML scoring, behavioural patterns) are best done
by a dedicated vendor; we provide a deterministic, audit-friendly engine
that runs on every signup and produces the audit trail.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.commercial import constants
from app.commercial.models import FraudSignal, SignupRateBucket

_log = logging.getLogger("zoiko.commercial.fraud")


# ──────────────────────────────────────────────────────────────────────────
# DECISION OUTPUTS
# ──────────────────────────────────────────────────────────────────────────
ACTION_ALLOW     = "allow"
ACTION_CHALLENGE = "challenge"
ACTION_REVIEW    = "review"
ACTION_BLOCK     = "block"


@dataclass
class FraudSignalRecord:
    type: str
    severity: str            # low|medium|high|critical
    score_delta: float
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FraudDecision:
    action: str              # allow|challenge|review|block
    score: float             # 0.0 (clean) → 100.0 (definite abuse)
    signals: List[FraudSignalRecord] = field(default_factory=list)
    reason: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return self.action == ACTION_BLOCK


# ──────────────────────────────────────────────────────────────────────────
# RATE-LIMIT BUCKETS (DB-backed, hourly windows)
# ──────────────────────────────────────────────────────────────────────────
def _hour_window(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def _bump_bucket(db: Session, key: str) -> int:
    """Increment the bucket for the current hour and return the new count."""
    window = _hour_window()
    row = (
        db.query(SignupRateBucket)
          .filter_by(bucket_key=key, window_start=window)
          .one_or_none()
    )
    if row is None:
        row = SignupRateBucket(bucket_key=key, window_start=window, count=1)
        db.add(row)
        db.flush()
        return 1
    row.count = (row.count or 0) + 1
    db.flush()
    return row.count


def _read_bucket(db: Session, key: str) -> int:
    window = _hour_window()
    row = (
        db.query(SignupRateBucket)
          .filter_by(bucket_key=key, window_start=window)
          .one_or_none()
    )
    return int(row.count) if row else 0


# ──────────────────────────────────────────────────────────────────────────
# INDIVIDUAL CHECKS
# ──────────────────────────────────────────────────────────────────────────
def _check_disposable_email(email: str, signals: List[FraudSignalRecord]) -> None:
    domain = email.split("@", 1)[-1].lower().strip()
    if not domain:
        return
    if domain in constants.DISPOSABLE_EMAIL_DOMAINS:
        signals.append(FraudSignalRecord(
            type="disposable_email",
            severity="critical",
            score_delta=100.0,
            details={"domain": domain},
        ))
    elif domain in constants.FREE_WEBMAIL_DOMAINS:
        signals.append(FraudSignalRecord(
            type="free_webmail",
            severity="low",
            score_delta=15.0,
            details={"domain": domain},
        ))


def _check_sanctioned_country(country: Optional[str], signals: List[FraudSignalRecord]) -> None:
    if country and country.upper() in constants.SANCTIONED_COUNTRIES:
        signals.append(FraudSignalRecord(
            type="sanctioned_country",
            severity="critical",
            score_delta=100.0,
            details={"country": country.upper()},
        ))


def _check_ip_velocity(db: Session, ip: Optional[str], signals: List[FraudSignalRecord]) -> None:
    if not ip:
        return
    count = _read_bucket(db, f"ip:{ip}")
    if count >= constants.FRAUD_SAME_IP_HOURLY_CHALLENGE:
        signals.append(FraudSignalRecord(
            type="high_velocity_ip",
            severity="high",
            score_delta=40.0,
            details={"ip_hash": _hash_ip(ip), "count": count},
        ))


def _check_device_velocity(db: Session, device_fp: Optional[str], signals: List[FraudSignalRecord]) -> None:
    if not device_fp:
        return
    count = _read_bucket(db, f"device:{device_fp}")
    if count >= constants.FRAUD_SAME_DEVICE_HOURLY_REVIEW:
        signals.append(FraudSignalRecord(
            type="high_velocity_device",
            severity="high",
            score_delta=45.0,
            details={"device": device_fp, "count": count},
        ))


def _check_domain_velocity(db: Session, email: str, signals: List[FraudSignalRecord]) -> None:
    domain = email.split("@", 1)[-1].lower().strip()
    if not domain:
        return
    count = _read_bucket(db, f"domain:{domain}")
    if count >= constants.FRAUD_SAME_DOMAIN_HOURLY_REVIEW:
        signals.append(FraudSignalRecord(
            type="high_velocity_domain",
            severity="medium",
            score_delta=25.0,
            details={"domain": domain, "count": count},
        ))


def _hash_ip(ip: str) -> str:
    # Hashed IP for storage — PII minimization.
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────
def evaluate(
    db: Session,
    *,
    email: str,
    country: Optional[str] = None,
    ip_address: Optional[str] = None,
    device_fingerprint: Optional[str] = None,
) -> FraudDecision:
    """Run every check and return a decision.

    The caller MUST call ``record_attempt`` afterwards (whether the
    signup completes or not) so velocity counters increment.
    """
    signals: List[FraudSignalRecord] = []

    _check_disposable_email(email, signals)
    _check_sanctioned_country(country, signals)
    _check_ip_velocity(db, ip_address, signals)
    _check_device_velocity(db, device_fingerprint, signals)
    _check_domain_velocity(db, email, signals)

    score = sum(s.score_delta for s in signals)

    # Decision rules (§7.2 + §17.2):
    if any(s.type == "disposable_email" for s in signals):
        return FraudDecision(action=ACTION_BLOCK, score=100.0, signals=signals,
                             reason="Disposable email domain blocked")
    if any(s.type == "sanctioned_country" for s in signals):
        return FraudDecision(action=ACTION_BLOCK, score=100.0, signals=signals,
                             reason="Sanctioned jurisdiction")
    if score >= 80:
        return FraudDecision(action=ACTION_REVIEW, score=score, signals=signals,
                             reason="Multiple fraud signals exceeded review threshold")
    if score >= 40:
        return FraudDecision(action=ACTION_CHALLENGE, score=score, signals=signals,
                             reason="Velocity / pattern challenge")
    return FraudDecision(action=ACTION_ALLOW, score=score, signals=signals)


def record_attempt(
    db: Session,
    *,
    email: str,
    ip_address: Optional[str] = None,
    device_fingerprint: Optional[str] = None,
) -> None:
    """Bump the rate-limit buckets. Always call after a signup attempt."""
    domain = email.split("@", 1)[-1].lower().strip()
    if domain:
        _bump_bucket(db, f"domain:{domain}")
    if ip_address:
        _bump_bucket(db, f"ip:{ip_address}")
    if device_fingerprint:
        _bump_bucket(db, f"device:{device_fingerprint}")


def record_failed_payment(
    db: Session,
    *,
    organization_id: Optional[str],
    ip_address: Optional[str] = None,
    failure_code: Optional[str] = None,
) -> Optional[FraudSignal]:
    """§17.2 — bump the failed-payment counter for this org. After the
    configured threshold (default 3 in the last hour) a `payment.fraud_flagged`
    signal is persisted and a manual_review case is opened so risk/ops sees it.

    Idempotent at the bucket level (one bucket bump per call); the review
    case is only opened the first time the threshold is crossed in a given
    hour window.
    """
    if not organization_id:
        return None
    bucket_key = f"failed_payment:{organization_id}"
    count = _bump_bucket(db, bucket_key)
    threshold = constants.FRAUD_FAILED_PAYMENT_REVIEW
    if count < threshold:
        return None

    # Only persist the signal + open the case the first time we cross the
    # line in this hourly window. The bucket key is hourly so the next hour
    # resets and the next threshold crossing fires fresh.
    already = (
        db.query(FraudSignal)
          .filter(FraudSignal.organization_id == organization_id,
                  FraudSignal.signal_type == "failed_payment_velocity",
                  FraudSignal.created_at >= _hour_window())
          .first()
    )
    if already:
        return already

    row = FraudSignal(
        organization_id=organization_id,
        signal_type="failed_payment_velocity",
        severity="high",
        score_delta=40.0,
        details={
            "count_in_hour": count, "threshold": threshold,
            "ip_address": _hash_ip(ip_address) if ip_address else None,
            "failure_code": failure_code,
        },
    )
    db.add(row)
    db.flush()

    # Open a manual review case so the admin console surfaces it.
    try:
        from app.commercial import ids as _ids
        from app.commercial.models import ManualReviewCase
        db.add(ManualReviewCase(
            case_id=_ids.case_id(),
            organization_id=organization_id,
            reason="failed_payment_velocity",
            severity="high",
            fraud_score=40.0,
            status="pending",
            details={"count_in_hour": count, "threshold": threshold,
                     "failure_code": failure_code},
        ))
        db.flush()
    except Exception:
        pass
    return row


def persist_signals(
    db: Session,
    *,
    signup_id: Optional[str],
    organization_id: Optional[str],
    decision: FraudDecision,
) -> None:
    """Write each contributing signal to commercial_fraud_signals."""
    for s in decision.signals:
        db.add(FraudSignal(
            signup_id=signup_id,
            organization_id=organization_id,
            signal_type=s.type,
            severity=s.severity,
            score_delta=s.score_delta,
            details=s.details,
        ))
    db.flush()
