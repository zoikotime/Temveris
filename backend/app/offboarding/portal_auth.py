"""Former-worker portal authentication & security (§1, §2 — governance spec).

The portal is a SEPARATE authentication universe from active-worker auth:

  • A former-worker NEVER reuses their old refresh-token / session — those are
    revoked at exit. They authenticate against the FormerWorkerAccess grant
    using a one-time **portal token** delivered out-of-band (printed in the
    governance API response when the grant is issued).
  • A successful portal login mints a short-lived ``FormerWorkerPortalSession``
    bearer (PORTAL_SESSION_TTL_SECONDS — 30 min by default), held in
    ``status='pending_mfa'`` until an OTP delivered to the worker's verified
    **alternate email** is presented. Only then does the session flip to
    ``status='active'``.
  • Portal endpoints depend on ``get_portal_principal`` and never on
    ``get_current_user``; the active-worker JWT cannot satisfy this gate, and
    the portal bearer cannot satisfy ``get_current_user`` (it is not a JWT).
    That is the §1 "no inheritance from active-worker permissions" rule —
    enforced at the auth layer, not by UI hiding.

Hardening layered on top:
  • Per-token rate limit on login + OTP attempts.
  • Per-worker download throttle (per-minute + per-24h) with audit + 429.
  • Abnormal-activity heuristics (IP rotation, MFA-failure bursts) trip a HIGH
    SecurityIncident and revoke the session.
  • Every action — login attempts, MFA results, listings, downloads, support
    requests, DSAR opens, throttles, abnormal-activity — appends an immutable
    ``PortalAccessLog`` row and a mirror entry on the hash-chained audit chain.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from database.database import get_db

from . import events, ids, incidents
from .constants import (
    PORTAL_DAILY_DOWNLOAD_CAP, PORTAL_DISTINCT_IPS_PER_HOUR_LIMIT,
    PORTAL_DOWNLOAD_MAX_PER_WINDOW, PORTAL_DOWNLOAD_WINDOW_SECONDS,
    PORTAL_FAILED_MFA_BURST_LIMIT, PORTAL_LOGIN_MAX_PER_WINDOW,
    PORTAL_LOGIN_WINDOW_SECONDS, PORTAL_MAX_MFA_ATTEMPTS,
    PORTAL_MFA_CHALLENGE_TTL_SECONDS, PORTAL_OTP_LENGTH,
    PORTAL_SESSION_TTL_SECONDS, AlertType, FormerAccessStatus,
    MFAMethod, MFAStatus, PortalAction, PortalSessionStatus,
)
from .models import (
    DEFAULT_TENANT, FormerWorkerAccess, FormerWorkerMFA,
    FormerWorkerPortalSession, PortalAccessLog,
)

_log = logging.getLogger("zoiko.offboarding.portal_auth")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ════════════════════════════════════════════════════════════════════════════
# OTP DISPATCH  (test-overridable hook)
# ════════════════════════════════════════════════════════════════════════════
# In production this is wired to app.email_service. In tests it captures the
# raw OTP so the test can post it back. NEVER log the OTP in production.
_OTP_DISPATCHERS: List[Callable[[str, str], None]] = []


def register_otp_dispatcher(fn: Callable[[str, str], None]) -> None:
    """Add a dispatcher invoked with (email, raw_otp) for every OTP send. Used
    by the email backend (and by tests to capture the code)."""
    _OTP_DISPATCHERS.append(fn)


def clear_otp_dispatchers() -> None:
    _OTP_DISPATCHERS.clear()


def _dispatch_otp(email: str, otp: str) -> None:
    for fn in _OTP_DISPATCHERS:
        try:
            fn(email, otp)
        except Exception:
            _log.exception("OTP dispatcher failed (continuing — fail-open is safe here)")


def _new_otp() -> str:
    """Numeric OTP of PORTAL_OTP_LENGTH digits. SHA-256 is stored; the raw
    value is dispatched once and never persisted."""
    n = 10 ** PORTAL_OTP_LENGTH
    return str(secrets.randbelow(n)).zfill(PORTAL_OTP_LENGTH)


# ════════════════════════════════════════════════════════════════════════════
# IN-PROCESS LIMITERS  (login + download)
# ════════════════════════════════════════════════════════════════════════════
class _FixedWindowLimiter:
    def __init__(self, limit: int, window_s: int):
        self.limit = limit
        self.window_s = window_s
        self._hits: Dict[str, List[float]] = defaultdict(list)

    def check(self, key: str) -> Tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_s
        bucket = [t for t in self._hits[key] if t > cutoff]
        if len(bucket) >= self.limit:
            self._hits[key] = bucket
            return False, len(bucket)
        bucket.append(now)
        self._hits[key] = bucket
        return True, len(bucket)


_login_limiter    = _FixedWindowLimiter(PORTAL_LOGIN_MAX_PER_WINDOW, PORTAL_LOGIN_WINDOW_SECONDS)
_download_limiter = _FixedWindowLimiter(PORTAL_DOWNLOAD_MAX_PER_WINDOW, PORTAL_DOWNLOAD_WINDOW_SECONDS)


def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    try:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd
        return request.client.host if request.client else None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
# AUDIT (portal-side + hash-chained mirror)
# ════════════════════════════════════════════════════════════════════════════
def log_action(db: Session, *, fwa: Optional[FormerWorkerAccess], action: str,
               outcome: str = "ok", session: Optional[FormerWorkerPortalSession] = None,
               scope: Optional[str] = None, document_ref: Optional[str] = None,
               detail: Optional[dict] = None, ip: Optional[str] = None,
               user_agent: Optional[str] = None) -> PortalAccessLog:
    """Append a portal access log row AND a mirror entry on the hash-chained
    governance audit chain. Both are required: the portal log is the
    worker-facing view (§1 expiry visibility); the audit chain is the
    tamper-evident master copy."""
    row = PortalAccessLog(
        id=ids.portal_log_id(),
        tenant_id=fwa.tenant_id if fwa is not None else DEFAULT_TENANT,
        fwa_id=fwa.id if fwa is not None else None,
        worker_id=fwa.worker_id if fwa is not None else None,
        session_id=session.id if session is not None else None,
        action=action, outcome=outcome,
        scope=scope, document_ref=document_ref,
        detail=detail or {}, ip_address=ip, user_agent=(user_agent or "")[:512] or None,
    )
    db.add(row)
    db.flush()
    events.append_audit(
        db, tenant_id=row.tenant_id, action=f"Portal:{action}",
        entity="FormerWorkerPortalSession",
        case_id=fwa.case_id if fwa is not None else None,
        worker_id=row.worker_id,
        payload={"outcome": outcome, "scope": scope, "document_ref": document_ref,
                 "detail": detail or {}, "session_id": row.session_id},
        ip_address=ip,
    )
    return row


# ════════════════════════════════════════════════════════════════════════════
# ABNORMAL-ACTIVITY DETECTOR
# ════════════════════════════════════════════════════════════════════════════
def check_abnormal_activity(db: Session, fwa: FormerWorkerAccess, *,
                            session: Optional[FormerWorkerPortalSession] = None,
                            ip: Optional[str] = None) -> Optional[str]:
    """Inspect the last hour of portal log for the given FormerWorkerAccess:
      • too many distinct IPs (likely token sharing) → revoke session, incident.
      • too many MFA failures (brute force) → incident.
    Returns the trip reason if a trip fired, else None."""
    horizon = _now() - timedelta(hours=1)
    rows = (db.query(PortalAccessLog)
            .filter(PortalAccessLog.fwa_id == fwa.id,
                    PortalAccessLog.occurred_at >= horizon)
            .all())
    distinct_ips = {r.ip_address for r in rows if r.ip_address}
    failed_mfa = sum(1 for r in rows if r.action == PortalAction.MFA_FAILED.value)

    trip: Optional[str] = None
    if len(distinct_ips) > PORTAL_DISTINCT_IPS_PER_HOUR_LIMIT:
        trip = "ip_rotation"
    elif failed_mfa >= PORTAL_FAILED_MFA_BURST_LIMIT:
        trip = "mfa_brute_force"
    if trip is None:
        return None

    incidents.open_incident(
        db, tenant_id=fwa.tenant_id,
        incident_type=AlertType.FORMER_WORKER_ABUSE.value,
        title=f"Abnormal portal activity for FormerWorkerAccess {fwa.id} ({trip})",
        worker_id=fwa.worker_id, case_id=fwa.case_id,
        diagnostics={"trip": trip, "distinct_ips_1h": len(distinct_ips),
                     "failed_mfa_1h": failed_mfa, "current_ip": ip},
    )
    log_action(db, fwa=fwa, action=PortalAction.ABNORMAL_ACTIVITY.value,
               outcome="denied", session=session,
               detail={"trip": trip, "distinct_ips_1h": len(distinct_ips)},
               ip=ip)
    if trip == "ip_rotation" and session is not None:
        session.status = PortalSessionStatus.REVOKED.value
        session.revoked_at = _now()
        session.revoked_reason = "abnormal_ip_rotation"
        db.flush()
    return trip


# ════════════════════════════════════════════════════════════════════════════
# LOGIN  (step 1 — verify portal token, send OTP)
# ════════════════════════════════════════════════════════════════════════════
class PortalAuthError(HTTPException):
    """4xx error raised by the portal auth layer, kept distinct so the router
    can map it cleanly without leaking internal traces."""
    pass


def _enforce_rate_limit(key: str, limiter: _FixedWindowLimiter,
                        db: Session, fwa: Optional[FormerWorkerAccess],
                        action: str, ip: Optional[str]) -> None:
    ok, _count = limiter.check(key)
    if ok:
        return
    log_action(db, fwa=fwa, action=action, outcome="throttled",
               detail={"key": key[:80]}, ip=ip)
    raise PortalAuthError(status_code=429, detail="rate_limited")


def begin_login(db: Session, *, portal_token: str, request: Optional[Request] = None
                ) -> Tuple[str, FormerWorkerPortalSession]:
    """Verify a raw portal token; if the FormerWorkerAccess is active and not
    expired, mint a PENDING_MFA session and dispatch an OTP to the verified
    alternate email. Returns ``(session_token_raw, session_row)``. The raw
    token is shown ONCE in the response and never persisted."""
    ip = _client_ip(request)
    ua = (request.headers.get("user-agent") if request else None)

    # Rate-limit by IP first; preempts password-spray attacks on the portal
    # token itself before we look at the DB.
    if ip:
        _enforce_rate_limit(f"login_ip:{ip}", _login_limiter, db, None,
                            PortalAction.LOGIN_ATTEMPT.value, ip)

    fwa = (db.query(FormerWorkerAccess)
           .filter(FormerWorkerAccess.portal_token_hash == _hash(portal_token))
           .first())
    if fwa is None:
        log_action(db, fwa=None, action=PortalAction.LOGIN_ATTEMPT.value,
                   outcome="failed", detail={"reason": "unknown_token"}, ip=ip, user_agent=ua)
        raise PortalAuthError(status_code=401, detail="invalid_token")

    # Now rate-limit by the resolved access grant — protects a single former
    # worker from being locked out by attackers from other IPs.
    _enforce_rate_limit(f"login_fwa:{fwa.id}", _login_limiter, db, fwa,
                        PortalAction.LOGIN_ATTEMPT.value, ip)

    if fwa.status != FormerAccessStatus.ACTIVE.value:
        log_action(db, fwa=fwa, action=PortalAction.LOGIN_ATTEMPT.value,
                   outcome="denied", detail={"reason": "access_inactive"},
                   ip=ip, user_agent=ua)
        raise PortalAuthError(status_code=403, detail="access_inactive")

    expires_at = _as_utc(fwa.expires_at)
    if expires_at is not None and _now() >= expires_at:
        # The grant has run out — flip its status so the next admin view
        # reflects reality, then refuse.
        fwa.status = FormerAccessStatus.EXPIRED.value
        db.flush()
        log_action(db, fwa=fwa, action=PortalAction.LOGIN_ATTEMPT.value,
                   outcome="denied", detail={"reason": "access_expired"},
                   ip=ip, user_agent=ua)
        raise PortalAuthError(status_code=403, detail="access_expired")

    mfa = _mfa_row(db, fwa)
    if mfa is None or mfa.status != MFAStatus.ENROLLED.value or not mfa.alternate_email_verified:
        log_action(db, fwa=fwa, action=PortalAction.LOGIN_ATTEMPT.value,
                   outcome="denied", detail={"reason": "mfa_not_enrolled"},
                   ip=ip, user_agent=ua)
        raise PortalAuthError(status_code=409,
                              detail={"code": "mfa_enrollment_required",
                                      "message": "Enroll an alternate verified email first."})

    raw_session = secrets.token_urlsafe(32)
    otp = _new_otp()
    session = FormerWorkerPortalSession(
        id=ids.portal_session_id(), tenant_id=fwa.tenant_id, fwa_id=fwa.id,
        worker_id=fwa.worker_id, token_hash=_hash(raw_session),
        status=PortalSessionStatus.PENDING_MFA.value, mfa_passed=False, mfa_attempts=0,
        otp_hash=_hash(otp),
        otp_expires_at=_now() + timedelta(seconds=PORTAL_MFA_CHALLENGE_TTL_SECONDS),
        issued_at=_now(),
        expires_at=_now() + timedelta(seconds=PORTAL_SESSION_TTL_SECONDS),
        last_active_at=_now(), ip_address=ip, user_agent=(ua or "")[:512] or None,
    )
    db.add(session)
    db.flush()

    _dispatch_otp(mfa.alternate_email, otp)
    log_action(db, fwa=fwa, action=PortalAction.LOGIN_SUCCEEDED.value, outcome="ok",
               session=session, detail={"mfa_dispatched_to": _mask_email(mfa.alternate_email)},
               ip=ip, user_agent=ua)
    log_action(db, fwa=fwa, action=PortalAction.MFA_SENT.value, outcome="ok",
               session=session,
               detail={"channel": "email", "to": _mask_email(mfa.alternate_email)},
               ip=ip, user_agent=ua)

    check_abnormal_activity(db, fwa, session=session, ip=ip)
    return raw_session, session


def verify_mfa(db: Session, *, session_token: str, otp: str,
               request: Optional[Request] = None) -> FormerWorkerPortalSession:
    """Verify an OTP against the PENDING_MFA session. On success the session
    flips to ACTIVE; on too many failures it is REVOKED and an incident opens."""
    ip = _client_ip(request)
    session = _resolve_session(db, session_token, require_active=False)
    if session is None:
        raise PortalAuthError(status_code=401, detail="invalid_session")
    if session.status != PortalSessionStatus.PENDING_MFA.value:
        raise PortalAuthError(status_code=409, detail="session_not_pending_mfa")
    if _as_utc(session.otp_expires_at) is None or _now() > _as_utc(session.otp_expires_at):
        session.status = PortalSessionStatus.EXPIRED.value
        db.flush()
        fwa = db.query(FormerWorkerAccess).filter(FormerWorkerAccess.id == session.fwa_id).first()
        log_action(db, fwa=fwa, action=PortalAction.MFA_FAILED.value, outcome="failed",
                   session=session, detail={"reason": "otp_expired"}, ip=ip)
        raise PortalAuthError(status_code=401, detail="otp_expired")

    session.mfa_attempts = (session.mfa_attempts or 0) + 1
    fwa = db.query(FormerWorkerAccess).filter(FormerWorkerAccess.id == session.fwa_id).first()

    if _hash(otp) != session.otp_hash:
        if session.mfa_attempts >= PORTAL_MAX_MFA_ATTEMPTS:
            session.status = PortalSessionStatus.REVOKED.value
            session.revoked_at = _now()
            session.revoked_reason = "mfa_max_attempts"
        db.flush()
        log_action(db, fwa=fwa, action=PortalAction.MFA_FAILED.value, outcome="failed",
                   session=session, detail={"attempts": session.mfa_attempts}, ip=ip)
        check_abnormal_activity(db, fwa, session=session, ip=ip)
        raise PortalAuthError(status_code=401, detail="invalid_otp")

    session.status = PortalSessionStatus.ACTIVE.value
    session.mfa_passed = True
    session.last_active_at = _now()
    session.otp_hash = None
    session.otp_expires_at = None
    db.flush()
    if fwa is not None:
        mfa = _mfa_row(db, fwa)
        if mfa is not None:
            mfa.last_verified_at = _now()
            db.flush()
    log_action(db, fwa=fwa, action=PortalAction.MFA_VERIFIED.value, outcome="ok",
               session=session, ip=ip)
    return session


def logout(db: Session, *, session_token: str, request: Optional[Request] = None) -> None:
    ip = _client_ip(request)
    session = _resolve_session(db, session_token, require_active=False)
    if session is None:
        return
    session.status = PortalSessionStatus.REVOKED.value
    session.revoked_at = _now()
    session.revoked_reason = "logout"
    db.flush()
    fwa = db.query(FormerWorkerAccess).filter(FormerWorkerAccess.id == session.fwa_id).first()
    log_action(db, fwa=fwa, action=PortalAction.LOGOUT.value, outcome="ok",
               session=session, ip=ip)


# ════════════════════════════════════════════════════════════════════════════
# MFA ENROLLMENT  (two-step: alternate email → verify OTP)
# ════════════════════════════════════════════════════════════════════════════
def _mfa_row(db: Session, fwa: FormerWorkerAccess) -> Optional[FormerWorkerMFA]:
    return (db.query(FormerWorkerMFA)
            .filter(FormerWorkerMFA.fwa_id == fwa.id)
            .first())


def begin_mfa_enrollment(db: Session, *, fwa: FormerWorkerAccess, alternate_email: str
                         ) -> FormerWorkerMFA:
    """Capture the alternate email and dispatch a verification OTP. Status moves
    to PENDING_VERIFY; the worker confirms via ``verify_mfa_enrollment``."""
    if not alternate_email or "@" not in alternate_email or len(alternate_email) > 255:
        raise PortalAuthError(status_code=422, detail="invalid_email")
    row = _mfa_row(db, fwa)
    if row is None:
        row = FormerWorkerMFA(
            id=ids.mfa_id(), tenant_id=fwa.tenant_id, fwa_id=fwa.id,
            worker_id=fwa.worker_id, method=MFAMethod.EMAIL_OTP.value,
        )
        db.add(row)
    row.alternate_email = alternate_email
    row.alternate_email_verified = False
    row.status = MFAStatus.PENDING_VERIFY.value
    otp = _new_otp()
    row.enrollment_otp_hash = _hash(otp)
    row.enrollment_otp_expires_at = _now() + timedelta(seconds=PORTAL_MFA_CHALLENGE_TTL_SECONDS)
    row.enrollment_attempts = 0
    db.flush()
    _dispatch_otp(alternate_email, otp)
    log_action(db, fwa=fwa, action=PortalAction.MFA_SENT.value, outcome="ok",
               detail={"purpose": "enrollment", "to": _mask_email(alternate_email)})
    return row


def verify_mfa_enrollment(db: Session, *, fwa: FormerWorkerAccess, otp: str
                          ) -> Tuple[FormerWorkerMFA, Optional[str]]:
    """Verify the enrollment OTP. On success: status → ENROLLED, the alternate
    email is marked verified, and a one-time recovery code is generated +
    returned (raw, NEVER persisted)."""
    row = _mfa_row(db, fwa)
    if row is None or row.status != MFAStatus.PENDING_VERIFY.value:
        raise PortalAuthError(status_code=409, detail="enrollment_not_pending")
    if (_as_utc(row.enrollment_otp_expires_at) is None
            or _now() > _as_utc(row.enrollment_otp_expires_at)):
        raise PortalAuthError(status_code=401, detail="otp_expired")
    row.enrollment_attempts = (row.enrollment_attempts or 0) + 1
    if _hash(otp) != row.enrollment_otp_hash:
        db.flush()
        log_action(db, fwa=fwa, action=PortalAction.MFA_FAILED.value, outcome="failed",
                   detail={"purpose": "enrollment", "attempts": row.enrollment_attempts})
        raise PortalAuthError(status_code=401, detail="invalid_otp")
    row.alternate_email_verified = True
    row.status = MFAStatus.ENROLLED.value
    row.enrollment_otp_hash = None
    row.enrollment_otp_expires_at = None
    row.last_verified_at = _now()
    raw_recovery = secrets.token_urlsafe(16)
    row.recovery_code_hash = _hash(raw_recovery)
    db.flush()
    log_action(db, fwa=fwa, action=PortalAction.MFA_VERIFIED.value, outcome="ok",
               detail={"purpose": "enrollment"})
    return row, raw_recovery


# ════════════════════════════════════════════════════════════════════════════
# DOWNLOAD THROTTLING
# ════════════════════════════════════════════════════════════════════════════
def check_download_throttle(db: Session, fwa: FormerWorkerAccess, *,
                            session: Optional[FormerWorkerPortalSession] = None,
                            ip: Optional[str] = None) -> None:
    """Per-minute (in-process) + per-24h (DB) cap on downloads. Audits a
    THROTTLED row and raises 429 when either ceiling is hit."""
    ok, _count = _download_limiter.check(f"dl:{fwa.id}")
    if not ok:
        log_action(db, fwa=fwa, action=PortalAction.DOWNLOAD_THROTTLED.value,
                   outcome="throttled", session=session,
                   detail={"window_seconds": PORTAL_DOWNLOAD_WINDOW_SECONDS,
                           "limit": PORTAL_DOWNLOAD_MAX_PER_WINDOW}, ip=ip)
        raise PortalAuthError(status_code=429, detail="download_throttled")

    horizon = _now() - timedelta(hours=24)
    daily = (db.query(PortalAccessLog)
             .filter(PortalAccessLog.fwa_id == fwa.id,
                     PortalAccessLog.action == PortalAction.DOWNLOAD_DOCUMENT.value,
                     PortalAccessLog.occurred_at >= horizon)
             .count())
    if daily >= PORTAL_DAILY_DOWNLOAD_CAP:
        log_action(db, fwa=fwa, action=PortalAction.DOWNLOAD_THROTTLED.value,
                   outcome="throttled", session=session,
                   detail={"window": "24h", "limit": PORTAL_DAILY_DOWNLOAD_CAP,
                           "observed": daily}, ip=ip)
        raise PortalAuthError(status_code=429, detail="daily_download_cap_reached")


# ════════════════════════════════════════════════════════════════════════════
# PRINCIPAL  (the FastAPI dependency portal endpoints use)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class PortalPrincipal:
    session: FormerWorkerPortalSession
    fwa: FormerWorkerAccess
    tenant_id: str
    worker_id: Optional[int]


def _resolve_session(db: Session, raw_token: str, *, require_active: bool
                     ) -> Optional[FormerWorkerPortalSession]:
    if not raw_token:
        return None
    session = (db.query(FormerWorkerPortalSession)
               .filter(FormerWorkerPortalSession.token_hash == _hash(raw_token))
               .first())
    if session is None:
        return None
    if _as_utc(session.expires_at) is not None and _now() > _as_utc(session.expires_at):
        if session.status not in (PortalSessionStatus.EXPIRED.value,
                                   PortalSessionStatus.REVOKED.value):
            session.status = PortalSessionStatus.EXPIRED.value
            db.flush()
            fwa = db.query(FormerWorkerAccess).filter(
                FormerWorkerAccess.id == session.fwa_id).first()
            log_action(db, fwa=fwa, action=PortalAction.SESSION_EXPIRED.value,
                       outcome="denied", session=session)
        return None if require_active else session
    if require_active and session.status != PortalSessionStatus.ACTIVE.value:
        return None
    return session


def get_portal_principal(
    request: Request,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> PortalPrincipal:
    """FastAPI dependency for every portal route. Requires a bearer token whose
    session is ACTIVE (MFA passed, not expired). Performs zero RBAC lookup
    against active-worker permissions — by construction the principal carries
    no permissions outside the portal allowlist (§1)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise PortalAuthError(status_code=401, detail="missing_bearer")
    raw = authorization.split(" ", 1)[1].strip()
    session = _resolve_session(db, raw, require_active=True)
    if session is None:
        raise PortalAuthError(status_code=401, detail="invalid_or_expired_session")
    fwa = db.query(FormerWorkerAccess).filter(FormerWorkerAccess.id == session.fwa_id).first()
    if fwa is None or fwa.status != FormerAccessStatus.ACTIVE.value:
        raise PortalAuthError(status_code=403, detail="access_inactive")
    # Touch the session so the worker's "Active devices" view stays fresh.
    session.last_active_at = _now()
    db.flush()
    return PortalPrincipal(session=session, fwa=fwa, tenant_id=fwa.tenant_id,
                           worker_id=fwa.worker_id)


def _mask_email(email: Optional[str]) -> Optional[str]:
    """``alex.kim@company.com`` → ``a***@c***.com``. Never reveals full email
    in audit blobs — they're widely readable."""
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    masked_local = (local[0] + "***") if local else "***"
    dot = domain.rfind(".")
    masked_dom = (domain[0] + "***" + (domain[dot:] if dot != -1 else "")) if domain else "***"
    return f"{masked_local}@{masked_dom}"
