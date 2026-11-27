"""Identity service (§5 — required service #1).

Responsibilities:
  - Provisional user creation (self-serve signup)
  - Email verification token lifecycle
  - Password hashing (delegated to the existing auth.service helpers)
  - Existence checks (EMAIL_ALREADY_EXISTS handling)

This is the *commercial-layer* identity service. It writes to the
existing `users` table so login still works through the existing
auth.service.login endpoint after signup.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func

from database.models import User
from app.auth.service import hash_password
from app.commercial.models import EmailVerificationToken
from app.commercial import constants


def email_exists(db: Session, email: str) -> bool:
    return db.query(User.id).filter(func.lower(User.email) == email.lower()).first() is not None


def create_provisional_user(
    db: Session,
    *,
    first_name: str,
    last_name: str,
    email: str,
    password: str,
    locale: Optional[str] = None,
    timezone_name: Optional[str] = None,
) -> User:
    """Create a User row in 'invited / unverified' state.

    Per I-6 (provisional access), the user can access a restricted
    workspace immediately. Expansion actions are gated on email
    verification via the entitlements resolver.
    """
    user = User(
        name=f"{first_name} {last_name}".strip() or email.split("@", 1)[0],
        email=email.strip().lower(),
        password=hash_password(password),
        role="admin",            # the signup user becomes the org owner / admin
        is_active=True,
        is_invited=False,
        is_password_set=True,
    )
    db.add(user)
    db.flush()
    return user


# ──────────────────────────────────────────────────────────────────────────
# Email verification tokens
# ──────────────────────────────────────────────────────────────────────────
def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_code() -> str:
    """A zero-padded verification code drawn from the OS CSPRNG.

    `secrets` rather than `random` because this is a credential, and
    zero-padded so "004821" is a possible code — trimming leading zeros would
    quietly shrink the keyspace and make short codes distinguishable.
    """
    upper = 10 ** constants.VERIFICATION_CODE_LENGTH
    return str(secrets.randbelow(upper)).zfill(constants.VERIFICATION_CODE_LENGTH)


def _hash_code(code: str) -> str:
    """Keyed HMAC of the code, matching how the platform hashes its OTPs.

    Keyed rather than a bare digest because the code has only a million
    possible values: a plain SHA-256 of every one of them can be tabulated in
    seconds, so a database read would be equivalent to reading the code. The
    key is SECRET_KEY, the same input `app.auth.service._hash_otp` uses.
    """
    from app.auth.service import SECRET_KEY

    key = (SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, (code or "").encode("utf-8"), hashlib.sha256).hexdigest()


def issue_verification_token(
    db: Session,
    *,
    user_id: int,
    organization_id: Optional[str],
    ttl_hours: int = 48,
) -> Tuple[str, EmailVerificationToken]:
    """Mint a new verification token. Returns (raw_token, db_row).

    The raw token is what we email to the user; only the SHA-256 hash is
    stored. The token has an explicit TTL (48h default) so phishing /
    leaks become time-bounded.

    The row also carries a short 6-digit code for the Verify step — see
    :func:`issue_verification_credentials` for the pair, and the notes on
    ``EmailVerificationToken.code_hash`` for why both live on one row. The
    return shape is unchanged so every existing caller keeps working; the code
    is read off ``row.code_hash``'s sibling by the newer helper.
    """
    raw = secrets.token_urlsafe(32)
    row = EmailVerificationToken(
        user_id=user_id,
        organization_id=organization_id,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        last_sent_at=datetime.now(timezone.utc),
        resend_count=0,
    )
    db.add(row)
    db.flush()
    return raw, row


def issue_verification_credentials(
    db: Session,
    *,
    user_id: int,
    organization_id: Optional[str],
    ttl_hours: int = 48,
) -> Tuple[str, str, EmailVerificationToken]:
    """Mint a verification row carrying BOTH credentials.

    Returns ``(raw_token, raw_code, row)``. The token is the 48-hour emailed
    link; the code is the short-lived digits the Verify step accepts. Neither
    is stored in the clear.

    Two credentials, one row, one ``consumed_at``: whichever the visitor uses
    first completes the verification and the other becomes ALREADY_CONSUMED.
    """
    raw_token, row = issue_verification_token(
        db, user_id=user_id, organization_id=organization_id, ttl_hours=ttl_hours,
    )
    raw_code = _generate_code()
    row.code_hash = _hash_code(raw_code)
    row.code_attempts = 0
    db.flush()
    return raw_token, raw_code, row


def resend_verification_token(
    db: Session, *, user_id: int,
) -> Tuple[Optional[str], Optional[str]]:
    """Issue a new token if the cooldown has elapsed.

    Returns (new_raw_token, error_code). Error codes:
      - "RATE_LIMITED" if cooldown not elapsed.
    """
    last = (
        db.query(EmailVerificationToken)
          .filter(EmailVerificationToken.user_id == user_id)
          .order_by(EmailVerificationToken.id.desc())
          .first()
    )
    now = datetime.now(timezone.utc)
    if last and last.last_sent_at:
        last_sent = last.last_sent_at if last.last_sent_at.tzinfo else last.last_sent_at.replace(tzinfo=timezone.utc)
        if (now - last_sent).total_seconds() < constants.VERIFICATION_RESEND_COOLDOWN_SECONDS:
            return None, constants.ERR_RATE_LIMITED

    org_id = last.organization_id if last else None
    raw, _ = issue_verification_token(db, user_id=user_id, organization_id=org_id)
    return raw, None


def resend_verification_credentials(
    db: Session, *, user_id: int,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Cooldown-guarded resend that mints both credentials.

    Returns ``(raw_token, raw_code, error_code)``. Mirrors
    :func:`resend_verification_token` exactly — same cooldown, same
    ``RATE_LIMITED`` code — so the two cannot drift; it differs only in also
    producing the digits.
    """
    last = (
        db.query(EmailVerificationToken)
          .filter(EmailVerificationToken.user_id == user_id)
          .order_by(EmailVerificationToken.id.desc())
          .first()
    )
    now = datetime.now(timezone.utc)
    if last and last.last_sent_at:
        last_sent = (last.last_sent_at if last.last_sent_at.tzinfo
                     else last.last_sent_at.replace(tzinfo=timezone.utc))
        if (now - last_sent).total_seconds() < constants.VERIFICATION_RESEND_COOLDOWN_SECONDS:
            return None, None, constants.ERR_RATE_LIMITED

    org_id = last.organization_id if last else None
    raw_token, raw_code, _ = issue_verification_credentials(
        db, user_id=user_id, organization_id=org_id,
    )
    return raw_token, raw_code, None


def resend_cooldown_remaining(db: Session, *, user_id: int) -> int:
    """Whole seconds until this user may request another code. 0 when ready.

    Read-only. Lets an authenticated caller be told the real remaining time
    instead of the client guessing at it from a hardcoded constant.
    """
    last = (
        db.query(EmailVerificationToken)
          .filter(EmailVerificationToken.user_id == user_id)
          .order_by(EmailVerificationToken.id.desc())
          .first()
    )
    if last is None or last.last_sent_at is None:
        return 0
    last_sent = (last.last_sent_at if last.last_sent_at.tzinfo
                 else last.last_sent_at.replace(tzinfo=timezone.utc))
    elapsed = (datetime.now(timezone.utc) - last_sent).total_seconds()
    remaining = constants.VERIFICATION_RESEND_COOLDOWN_SECONDS - elapsed
    return max(0, int(remaining + 0.999))


def consume_verification_token(
    db: Session, *, raw_token: str,
) -> Tuple[Optional[User], Optional[str]]:
    """Mark the token as consumed and return the user it belonged to.

    Error codes: "INVALID_TOKEN", "TOKEN_EXPIRED", "ALREADY_CONSUMED".
    """
    th = _hash_token(raw_token)
    row = db.query(EmailVerificationToken).filter_by(token_hash=th).one_or_none()
    if row is None:
        return None, "INVALID_TOKEN"
    if row.consumed_at is not None:
        return None, "ALREADY_CONSUMED"
    exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return None, "TOKEN_EXPIRED"
    row.consumed_at = datetime.now(timezone.utc)
    db.flush()
    user = db.query(User).filter_by(id=row.user_id).one_or_none()
    return user, None


def consume_verification_code(
    db: Session, *, user_id: int, code: str,
) -> Tuple[Optional[User], Optional[str]]:
    """Verify a typed code for a KNOWN user and consume the row.

    The user id comes from the caller's session, never from the request body —
    that is what stops this becoming an oracle anyone can point at any address.

    Error codes: ``VERIFICATION_CODE_INVALID``, ``VERIFICATION_CODE_EXPIRED``,
    ``VERIFICATION_CODE_LOCKED``, ``ALREADY_CONSUMED``.

    Every wrong guess is counted and the row locks at
    ``VERIFICATION_CODE_MAX_ATTEMPTS``; a locked row stays locked until a
    resend mints a new one. The failure code is identical for a wrong digit and
    a wrong length, so nothing reveals how close a guess was.
    """
    submitted = (code or "").strip()
    now = datetime.now(timezone.utc)

    row = (
        db.query(EmailVerificationToken)
          .filter(EmailVerificationToken.user_id == user_id)
          .filter(EmailVerificationToken.code_hash.isnot(None))
          .order_by(EmailVerificationToken.id.desc())
          .first()
    )
    if row is None:
        return None, constants.ERR_VERIFICATION_CODE_INVALID

    if row.consumed_at is not None:
        return None, "ALREADY_CONSUMED"

    if (row.code_attempts or 0) >= constants.VERIFICATION_CODE_MAX_ATTEMPTS:
        return None, constants.ERR_VERIFICATION_CODE_LOCKED

    # The code expires long before the link does; both are bounded by the row.
    created = row.created_at or now
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    code_expiry = created + timedelta(seconds=constants.VERIFICATION_CODE_TTL_SECONDS)
    row_expiry = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if now > code_expiry or now > row_expiry:
        return None, constants.ERR_VERIFICATION_CODE_EXPIRED

    # Shape-check first, then compare in constant time. Both paths return the
    # same code so a malformed guess is indistinguishable from a wrong one.
    ok = (
        len(submitted) == constants.VERIFICATION_CODE_LENGTH
        and submitted.isdigit()
        and hmac.compare_digest(row.code_hash, _hash_code(submitted))
    )
    if not ok:
        row.code_attempts = (row.code_attempts or 0) + 1
        db.flush()
        locked = row.code_attempts >= constants.VERIFICATION_CODE_MAX_ATTEMPTS
        return None, (constants.ERR_VERIFICATION_CODE_LOCKED if locked
                      else constants.ERR_VERIFICATION_CODE_INVALID)

    row.consumed_at = now
    db.flush()
    user = db.query(User).filter_by(id=row.user_id).one_or_none()
    return user, None


def is_email_verified(db: Session, user_id: int) -> bool:
    """A user is 'verified' if any verification token row for them is consumed."""
    return (
        db.query(EmailVerificationToken.id)
          .filter(EmailVerificationToken.user_id == user_id)
          .filter(EmailVerificationToken.consumed_at.isnot(None))
          .first()
        is not None
    )
