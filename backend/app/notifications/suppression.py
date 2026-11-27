"""Temveris — Email Suppression / One-Click Unsubscribe (leaf primitive)
=========================================================================
Reference: "Commercial Email Communications Estate" — global footer components
("one-click unsubscribe for marketing or subscribed messages only") and
ZT-WEB-04 ("unsubscribe at any time without affecting essential account,
security, attendance, approval, or billing communications").

This is a DEPENDENCY-LIGHT LEAF: it imports only stdlib + the ORM model. It
never imports the email transport or the auth-heavy notifications router, so
``email_service`` can lazily import it (Phase 4B) without any circular-import
risk.

Responsibilities:
  * Mint a signed, non-forgeable unsubscribe URL (HMAC over the recipient email).
  * Persist / read the suppression list (``email_suppressions`` table).

Scope semantics (per the document — marketing/subscribed only):
  * scope="marketing" (default) suppresses MARKETING + CUSTOMER_SUCCESS mail.
  * scope="all" additionally suppresses OPERATIONAL mail.
  * Critical / Essential / Commercial-account mail NEVER consults this list.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from sqlalchemy.orm import Session

from database.models import EmailSuppression

_log = logging.getLogger("zoiko.notifications.suppression")

# Signing secret for the unsubscribe token. Reuses an existing app secret when
# present so links stay valid across restarts; a dev fallback keeps local runs
# working. NEVER weakens existing mail — only signs the unsubscribe link.
_SECRET = (
    os.getenv("UNSUBSCRIBE_SECRET")
    or os.getenv("SECRET_KEY")
    or os.getenv("JWT_SECRET")
    or "temveris-unsubscribe-dev-secret"
).encode("utf-8")

# Public base URL that serves the backend one-click endpoint. Defaults to the
# same host that already serves /email-assets (see EMAIL_LOGO_URL default in
# email_service). Override with UNSUBSCRIBE_URL_BASE.
_BASE = os.getenv("UNSUBSCRIBE_URL_BASE", "https://app.temveris.example").rstrip("/")

# Classes a marketing-scope unsubscribe applies to (mirrors classification.py,
# duplicated as bare strings to keep this a zero-import leaf).
_MARKETING_LIKE = {"marketing", "customer_success"}


def _norm(email: str) -> str:
    return (email or "").strip().lower()


# ── Signed token ─────────────────────────────────────────────────────────────
def make_token(email: str) -> str:
    """Deterministic, non-forgeable token for an email address."""
    return hmac.new(_SECRET, _norm(email).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_token(email: str, token: str) -> bool:
    """Constant-time comparison; rejects tampered/absent tokens."""
    try:
        return hmac.compare_digest(make_token(email), (token or ""))
    except Exception:
        return False


def unsubscribe_url(email: str) -> str:
    """Signed one-click unsubscribe URL for the List-Unsubscribe header (4B)."""
    e = _norm(email)
    return f"{_BASE}/notifications/unsubscribe?email={quote(e)}&token={make_token(e)}"


# ── Suppression store ────────────────────────────────────────────────────────
def is_suppressed(db: Optional[Session], email: str, classification: Optional[str] = None) -> bool:
    """True if this address has unsubscribed from mail of the given class.

    scope="all"       -> suppresses marketing, customer_success, and operational.
    scope="marketing" -> suppresses marketing + customer_success only.
    Fails OPEN (returns False) on any lookup error.
    """
    if db is None or not email:
        return False
    try:
        row = (
            db.query(EmailSuppression)
            .filter(EmailSuppression.email == _norm(email))
            .one_or_none()
        )
    except Exception:
        _log.warning("suppression lookup failed — failing open", exc_info=True)
        return False
    if not row or not row.suppressed:
        return False
    if row.scope == "all":
        return True
    # scope == "marketing"
    return (classification or "").lower() in _MARKETING_LIKE


def is_unsubscribed_any(db: Optional[Session], email: str) -> bool:
    """True if the address is currently suppressed under any scope."""
    if db is None or not email:
        return False
    row = (
        db.query(EmailSuppression)
        .filter(EmailSuppression.email == _norm(email))
        .one_or_none()
    )
    return bool(row and row.suppressed)


def suppress(db: Session, email: str, *, scope: str = "marketing", source: str = "one_click") -> None:
    """Record (or refresh) an unsubscribe. Idempotent per email."""
    e = _norm(email)
    if not e:
        return
    scope = scope if scope in ("marketing", "all") else "marketing"
    row = db.query(EmailSuppression).filter(EmailSuppression.email == e).one_or_none()
    if row:
        row.suppressed = True
        row.scope = scope
        row.source = source
        row.updated_at = datetime.utcnow()
    else:
        db.add(EmailSuppression(email=e, suppressed=True, scope=scope, source=source))
    db.commit()
    _log.info("email suppressed: %s (scope=%s, source=%s)", e, scope, source)


def resubscribe(db: Session, email: str) -> None:
    """Undo a suppression (re-opt-in)."""
    e = _norm(email)
    row = db.query(EmailSuppression).filter(EmailSuppression.email == e).one_or_none()
    if row and row.suppressed:
        row.suppressed = False
        row.updated_at = datetime.utcnow()
        db.commit()
        _log.info("email resubscribed: %s", e)
