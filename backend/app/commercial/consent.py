"""Consent & Legal Acceptance Model (§18) — immutable.

Every signup writes at minimum: terms.accepted, privacy.acknowledged, and
(when applicable) trial_terms.accepted. Marketing consent is opt-in.

Rows are append-only. Withdrawal is a *new* row with granted=False,
never a mutation of the original.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict

from sqlalchemy.orm import Session

from app.commercial import constants
from app.commercial.models import CommercialConsent
from app.commercial.audit import emit_event


def _ua_hash(user_agent: Optional[str]) -> Optional[str]:
    if not user_agent:
        return None
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]


def record(
    db: Session,
    *,
    consent_type: str,
    granted: bool = True,
    user_id: Optional[int] = None,
    organization_id: Optional[str] = None,
    signup_id: Optional[str] = None,
    version: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    country: Optional[str] = None,
    locale: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> CommercialConsent:
    """Append a single consent record + a matching audit event."""
    row = CommercialConsent(
        user_id=user_id,
        organization_id=organization_id,
        signup_id=signup_id,
        consent_type=consent_type,
        version=version,
        granted=granted,
        ip_address=ip_address,
        user_agent_hash=_ua_hash(user_agent),
        country=country,
        locale=locale,
        occurred_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()

    if consent_type in constants.EVENT_CONSENT:
        emit_event(
            db,
            event_type=consent_type,
            source_service="identity-service",
            payload={
                "version": version,
                "granted": granted,
                "signup_id": signup_id,
                "country": country,
            },
            organization_id=organization_id,
            user_id=user_id,
            correlation_id=correlation_id,
        )
    return row


def record_signup_bundle(
    db: Session,
    *,
    user_id: int,
    signup_id: str,
    organization_id: Optional[str],
    terms_version: str,
    privacy_version: str,
    marketing_consent: bool,
    ip_address: Optional[str],
    user_agent: Optional[str],
    country: Optional[str],
    locale: Optional[str],
    correlation_id: str,
) -> List[CommercialConsent]:
    """Write the full set of consents emitted by a signup form."""
    out: List[CommercialConsent] = []
    common = dict(
        user_id=user_id, organization_id=organization_id, signup_id=signup_id,
        ip_address=ip_address, user_agent=user_agent, country=country,
        locale=locale, correlation_id=correlation_id,
    )
    out.append(record(db, consent_type="terms.accepted",
                      version=terms_version, granted=True, **common))
    out.append(record(db, consent_type="privacy.acknowledged",
                      version=privacy_version, granted=True, **common))
    out.append(record(db, consent_type="trial_terms.accepted",
                      version=constants.TRIAL_TERMS_VERSION_CURRENT, granted=True, **common))
    if marketing_consent:
        out.append(record(db, consent_type="marketing_consent.granted",
                          granted=True, **common))
    return out


def list_for_user(db: Session, user_id: int) -> List[Dict]:
    rows = (
        db.query(CommercialConsent)
          .filter(CommercialConsent.user_id == user_id)
          .order_by(CommercialConsent.occurred_at.desc())
          .all()
    )
    return [
        {
            "consent_type": r.consent_type,
            "version": r.version,
            "granted": r.granted,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "country": r.country,
            "locale": r.locale,
        }
        for r in rows
    ]
