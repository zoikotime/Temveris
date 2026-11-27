"""
Refresh-token engine for Temveris's persistent "Remember for 30 days" login.

Design (matches Google / Slack / Auth0):
  • Access token  → short-lived JWT (see app/auth/service.create_access_token).
  • Refresh token → long-lived, OPAQUE, single-use, rotating credential stored
                    here. We never persist the raw value — only its SHA-256.

Rotation + reuse detection
  Every successful /auth/refresh mints a brand-new refresh token row sharing
  the previous one's `family_id` and marks the old row `rotated`. If a token
  that has already been rotated (or revoked) is presented again — the classic
  signature of a stolen/replayed token — we revoke the ENTIRE family so the
  attacker and the victim are both forced to re-authenticate.

Delivery
  • Web     → HttpOnly + SameSite cookie (JS can't read it → XSS-safe).
  • Desktop → returned in the JSON body so Electron's main process can seal it
              with OS-level safeStorage (DPAPI / Keychain / libsecret).

This module intentionally imports nothing from service.py so it can be used by
service.py without a circular import.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import Request, Response
from sqlalchemy.orm import Session

from database.models import RefreshToken, UserSession

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
REFRESH_COOKIE_NAME = os.getenv("REFRESH_COOKIE_NAME", "zt_refresh")

# "Remember for 30 days" lifetime, and the shorter lifetime when the box is
# left unchecked (treated as a transient session).
REFRESH_REMEMBER_DAYS  = int(os.getenv("REFRESH_TOKEN_REMEMBER_DAYS", "30"))
REFRESH_SESSION_HOURS  = int(os.getenv("REFRESH_TOKEN_SESSION_HOURS", "12"))

# Cookie attributes. In production with a cross-site frontend/api split you set
# COOKIE_SAMESITE=none + COOKIE_SECURE=1. For same-site (or localhost dev) the
# defaults below work without HTTPS.
_COOKIE_SAMESITE = (os.getenv("COOKIE_SAMESITE", "lax") or "lax").lower()
_COOKIE_SECURE   = (os.getenv("COOKIE_SECURE", "0") or "0").strip().lower() in ("1", "true", "yes")
_COOKIE_DOMAIN   = os.getenv("COOKIE_DOMAIN") or None
# Scope the cookie to the auth surface — it's only ever needed by /auth/refresh
# and /auth/logout, so nothing else on the API ever sees it.
_COOKIE_PATH = "/auth"

# SameSite=None is meaningless (and rejected by modern browsers) without Secure.
if _COOKIE_SAMESITE == "none":
    _COOKIE_SECURE = True


# ──────────────────────────────────────────────────────────────────────────────
# PRIMITIVES
# ──────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_raw_token() -> str:
    """48 bytes of CSPRNG entropy, URL-safe — far beyond brute-force range."""
    return secrets.token_urlsafe(48)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_family_id() -> str:
    return str(uuid.uuid4())


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _lifetime(remember: bool) -> timedelta:
    return (
        timedelta(days=REFRESH_REMEMBER_DAYS)
        if remember
        else timedelta(hours=REFRESH_SESSION_HOURS)
    )


def is_desktop_client(request: Optional[Request]) -> bool:
    """Electron sends X-Client-Type: desktop so we deliver the refresh token in
    the response body (safeStorage) instead of a cookie."""
    if request is None:
        return False
    val = (request.headers.get("x-client-type") or "").strip().lower()
    if val == "desktop":
        return True
    ua = request.headers.get("user-agent", "") or ""
    return "Temveris" in ua or "Electron" in ua


# ──────────────────────────────────────────────────────────────────────────────
# ISSUE / ROTATE / REVOKE
# ──────────────────────────────────────────────────────────────────────────────
def issue_refresh_token(
    db: Session,
    *,
    user_id: int,
    session_id: Optional[int],
    remember: bool,
    family_id: Optional[str] = None,
) -> Tuple[str, RefreshToken]:
    """Create a fresh refresh-token row and return (raw_token, row).

    Caller is responsible for committing — we flush so the row gets an id but
    leave the transaction open so login/refresh can commit everything atomically.
    """
    raw = generate_raw_token()
    row = RefreshToken(
        user_id    = user_id,
        session_id = session_id,
        token_hash = hash_token(raw),
        family_id  = family_id or new_family_id(),
        remember   = bool(remember),
        issued_at  = _now(),
        expires_at = _now() + _lifetime(remember),
    )
    db.add(row)
    db.flush()
    return raw, row


def revoke_family(db: Session, family_id: str, reason: str) -> int:
    """Hard-revoke every live token in a family. Returns the count revoked."""
    now = _now()
    rows = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.family_id == family_id,
            RefreshToken.is_revoked.is_(False),
        )
        .all()
    )
    for r in rows:
        r.is_revoked     = True
        r.revoked_at     = now
        r.revoked_reason = reason
    return len(rows)


def revoke_all_for_user(db: Session, user_id: int, reason: str = "logout_all") -> int:
    now = _now()
    rows = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.user_id == user_id,
            RefreshToken.is_revoked.is_(False),
        )
        .all()
    )
    for r in rows:
        r.is_revoked     = True
        r.revoked_at     = now
        r.revoked_reason = reason
    return len(rows)


def revoke_raw(db: Session, raw: Optional[str], reason: str = "logout") -> bool:
    """Revoke the single token matching `raw` (used on logout). Returns True
    if a live row was found and revoked."""
    if not raw:
        return False
    row = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == hash_token(raw))
        .first()
    )
    if row is None or row.is_revoked:
        return False
    row.is_revoked     = True
    row.revoked_at     = _now()
    row.revoked_reason = reason
    return True


class RefreshError(Exception):
    """Raised by rotate() with a machine-readable reason."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def rotate(db: Session, raw: str) -> Tuple[str, RefreshToken]:
    """Validate `raw`, rotate it, and return (new_raw, new_row).

    Raises RefreshError on any failure:
      • "invalid"        — no such token
      • "reuse_detected" — already-rotated/revoked token replayed (family killed)
      • "expired"        — past expiry
    The caller commits.
    """
    if not raw:
        raise RefreshError("invalid")

    row = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == hash_token(raw))
        .first()
    )
    if row is None:
        raise RefreshError("invalid")

    # Replay of a token that was already spent or killed → assume theft and
    # nuke the whole family so neither party can keep using it.
    if row.is_revoked or row.rotated_at is not None:
        revoke_family(db, row.family_id, "reuse_detected")
        db.commit()
        raise RefreshError("reuse_detected")

    if _now() > _as_utc(row.expires_at):
        row.is_revoked     = True
        row.revoked_at     = _now()
        row.revoked_reason = "expired"
        db.commit()
        raise RefreshError("expired")

    # Happy path: mint the successor in the same family/session, retire this one.
    new_raw, new_row = issue_refresh_token(
        db,
        user_id    = row.user_id,
        session_id = row.session_id,
        remember   = row.remember,
        family_id  = row.family_id,
    )
    row.rotated_at   = _now()
    row.last_used_at = _now()

    # Keep the linked device row warm so the Profile "Active devices" list
    # shows an accurate last-seen time as the access token rotates.
    if row.session_id:
        sess = db.query(UserSession).filter(UserSession.id == row.session_id).first()
        if sess and not sess.is_revoked:
            sess.last_active_at = _now()

    return new_raw, new_row


# ──────────────────────────────────────────────────────────────────────────────
# COOKIE HELPERS  (web only)
# ──────────────────────────────────────────────────────────────────────────────
def set_refresh_cookie(response: Response, raw: str, remember: bool) -> None:
    """Write the HttpOnly refresh cookie. When `remember` is False we omit
    Max-Age so the browser drops it at the end of the session."""
    max_age = int(_lifetime(True).total_seconds()) if remember else None
    response.set_cookie(
        key      = REFRESH_COOKIE_NAME,
        value    = raw,
        max_age  = max_age,
        httponly = True,
        secure   = _COOKIE_SECURE,
        samesite = _COOKIE_SAMESITE,
        domain   = _COOKIE_DOMAIN,
        path     = _COOKIE_PATH,
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key      = REFRESH_COOKIE_NAME,
        domain   = _COOKIE_DOMAIN,
        path     = _COOKIE_PATH,
        samesite = _COOKIE_SAMESITE,
        secure   = _COOKIE_SECURE,
        httponly = True,
    )


def read_refresh_cookie(request: Request) -> Optional[str]:
    return request.cookies.get(REFRESH_COOKIE_NAME)
