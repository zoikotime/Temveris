from sqlalchemy.orm import Session, make_transient as _sa_make_transient
from sqlalchemy import func as _sa_func
from database.models import User, AuditLog, UserSession, PasswordResetOTP, Organization, RefreshToken
from database.database import get_db
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from app.rate_limit import limiter  # per-endpoint rate limits (brute-force guard)
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import Optional

# Refresh-token engine (opaque, rotating, reuse-detecting) + cookie helpers.
# Imported as a module so service.py stays the single auth entry point while
# the token mechanics live in their own dependency-free module.
from app.auth import tokens as rtokens
# Organization lifecycle gate. Imported as a module (not by name) so the two
# enforcement points below read as `_org_lifecycle.assert_active(...)` and stay
# greppable as a single mechanism rather than a loose helper call.
from app.org import lifecycle as _org_lifecycle
from datetime import datetime, timezone, timedelta, timezone
from dotenv import load_dotenv
import hashlib
import hmac
import logging
import os
import re
import threading
import time as _time
import traceback

_auth_logger = logging.getLogger(__name__)


def _as_utc_aware(dt):
    """Coerce a possibly-naive datetime to UTC-aware so comparisons work.

    The User.invite_token_expires_at column is `DateTime` *without*
    timezone=True (see database/models.py:80), so PostgreSQL stores it as
    `timestamp without time zone` and SQLAlchemy returns naive datetimes.
    Comparing `datetime.now(timezone.utc)` (aware) against a naive value
    raises `TypeError: can't compare offset-naive and offset-aware datetimes`,
    which surfaces to the frontend as a 500 → "Invalid or Expired Link".

    Treat all stored values as UTC (which is how we write them — see the
    `forgot_password` and invite endpoints) so the comparison is correct
    even when the column drops tzinfo on its way through psycopg.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ==============================
# CLIENT IP HELPER  (used for Login IP report)
# ==============================
def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    if request.client:
        return request.client.host
    return None


_BROWSER_PATTERNS = [
    ("Edg",      "Microsoft Edge"),
    ("OPR",      "Opera"),
    ("Chrome",   "Chrome"),
    ("Firefox",  "Firefox"),
    ("Safari",   "Safari"),
    ("Electron", "Electron"),
]
_OS_PATTERNS = [
    ("Windows",   "Windows"),
    ("Mac OS",    "macOS"),
    ("Macintosh", "macOS"),
    ("iPhone",    "iOS"),
    ("iPad",      "iPadOS"),
    ("Android",   "Android"),
    ("Linux",     "Linux"),
]


def _parse_user_agent(ua: str) -> dict:
    ua = ua or ""
    browser = next((label for needle, label in _BROWSER_PATTERNS if needle in ua), "Unknown")
    os_name = next((label for needle, label in _OS_PATTERNS if needle in ua), "Unknown")
    if "Temveris" in ua or "Electron" in ua:
        device_type = "desktop_app"
    elif any(x in ua for x in ("iPhone", "iPad", "Android", "Mobile")):
        device_type = "mobile"
    else:
        device_type = "desktop"
    return {
        "browser":     browser,
        "os":          os_name,
        "device_type": device_type,
        "device_name": f"{os_name} · {browser}",
    }


def _record_login(db: Session, user: User, request: Optional[Request], token: Optional[str] = None) -> None:
    """Persist a UserLogin row in audit_logs so the Login IP report can group by user.

    When a token is supplied, also create a UserSession row keyed by SHA-256
    of that token — this powers the Profile page's active sessions list.
    Errors are swallowed so a misbehaving sessions table never blocks login.
    """
    try:
        ip = _client_ip(request)
        ua = (request.headers.get("user-agent") if request is not None else "") or ""
        db.add(AuditLog(
            actor_id        = user.id,
            target_id       = user.id,
            organization_id = user.organization_id,
            entity          = "User",
            action          = "UserLogin",
            payload         = {"email": user.email, "role": user.role, "user_agent": ua[:255]},
            ip_address      = ip,
        ))

        if token:
            meta = _parse_user_agent(ua)
            th = hashlib.sha256(token.encode("utf-8")).hexdigest()
            existing = db.query(UserSession).filter(UserSession.token_hash == th).first()
            if existing is None:
                db.add(UserSession(
                    user_id     = user.id,
                    token_hash  = th,
                    user_agent  = ua[:512],
                    device_type = meta["device_type"],
                    device_name = meta["device_name"],
                    browser     = meta["browser"],
                    os          = meta["os"],
                    ip_address  = ip,
                ))
            else:
                existing.last_active_at = datetime.now(timezone.utc)
                existing.is_revoked     = False
                existing.revoked_at     = None

        db.commit()
    except Exception:
        db.rollback()

load_dotenv()

# ==============================
# CONFIG
# ==============================
SECRET_KEY      = os.getenv("SECRET_KEY")           # ✅ no insecure fallback — must be in .env
ALGORITHM       = "HS256"
# Short-lived access token. The persistent "Remember for 30 days" experience is
# carried by the rotating REFRESH token (app/auth/tokens.py), NOT by a 30-day
# access JWT — that's the enterprise standard (Google/Slack/GitHub). Old 30-day
# tokens already in the wild keep working until they expire; new logins get a
# short access token + refresh token.
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 30))

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set in .env — refusing to start")

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

router = APIRouter(prefix="/auth", tags=["Auth"])


# ==============================
# PASSWORD HELPERS
# ==============================
def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ==============================
# JWT HELPERS
# ==============================
def create_access_token(data: dict) -> str:
    import uuid as _uuid
    payload = data.copy()
    now = datetime.now(timezone.utc)
    payload["iat"] = now
    # Unique per token so two tokens minted in the same second (e.g. login then
    # an immediate refresh) are still distinct — also a handle for auditing.
    payload.setdefault("jti", _uuid.uuid4().hex)
    payload["exp"] = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)  # ✅ expiry added
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ==============================
# USER IDENTITY CACHE
# ==============================
# Eliminates the DB round-trip on every authenticated request.
# Each DB query to Supabase from Cloud Run takes ~300-400ms; with 100 employees
# sending heartbeats every 30-60s, this was the primary cause of slow endpoints
# and Cloud Run slot exhaustion (429/503 under load).
# TTL = 5 minutes. Trade-off: a deactivated account can still call the API for
# up to 5 minutes after deactivation — acceptable for an internal workforce tool.
_USER_CACHE: dict = {}
_USER_CACHE_TTL = 300  # 5 minutes


def _user_cache_get(user_id: int):
    entry = _USER_CACHE.get(user_id)
    if entry and _time.monotonic() < entry["expires"]:
        return entry["user"]
    return None


def _user_cache_set(user_id: int, user: User) -> None:
    _USER_CACHE[user_id] = {
        "user": user,
        "expires": _time.monotonic() + _USER_CACHE_TTL,
    }


def _user_cache_invalidate(user_id: int) -> None:
    _USER_CACHE.pop(user_id, None)


# ==============================
# ORGANIZATION CACHE
# ==============================
# Same rationale as the user cache above, for the same reason: every endpoint
# that scopes by tenant depends on get_current_organization, so one admin
# dashboard refresh (~12 concurrent requests) paid ~12 uncached Organization
# lookups on top of each endpoint's real work.
#
# Safe to cache here in a way it would NOT be for a richer model:
#   * `organizations` is a 5-column stub (id, name, is_active, created_at,
#     updated_at) — no plan, seats, quota, permissions or role data.
#   * Call sites read `current_org.id` and NOTHING else (142 usages, zero reads
#     of name/is_active/timestamps), so no authorization decision anywhere is
#     made from a cached field.
#   * Rows are created in exactly one place (org provisioning) and are never
#     deleted, and `name`/`is_active` are never mutated — the row is effectively
#     immutable once written.
#
# Keyed by organization_id, so an entry can only ever be returned for the same
# tenant that stored it. The tenant BINDING itself still comes from
# current_user.organization_id, which is unchanged.
#
# Misses are deliberately NOT cached: a 404 for an org that is mid-provisioning
# must not be pinned for the whole TTL.
_ORG_CACHE: dict = {}
_ORG_CACHE_TTL = 300  # 5 minutes — matches _USER_CACHE_TTL


def _org_cache_get(org_id: int):
    entry = _ORG_CACHE.get(org_id)
    if entry and _time.monotonic() < entry["expires"]:
        return entry["org"]
    return None


def _org_cache_set(org_id: int, org: Organization) -> None:
    _ORG_CACHE[org_id] = {
        "org": org,
        "expires": _time.monotonic() + _ORG_CACHE_TTL,
    }


def _org_cache_invalidate(org_id: int) -> None:
    _ORG_CACHE.pop(org_id, None)


# ==============================
# GET CURRENT USER  (used by employees/router.py)
# ==============================
def _billing_gate(request: Optional[Request], db: Session, user: User) -> None:
    """Enforce organization-level read-only when a subscription payment is overdue.

    Checked here because ``get_current_user`` is already the single authorization choke
    point for every authenticated endpoint — the same place ``is_active`` is enforced.
    Putting the billing rule anywhere else would mean a second, competing
    authorization mechanism.

    Reads, clock-in/out, time tracking, auth and billing itself are never blocked; see
    app/billing/suspension.py. Fails open on any error.
    """
    if request is None:
        return
    try:
        from app.billing import suspension as billing_suspension   # noqa: PLC0415
        billing_suspension.check_request(db, user, request.method, request.url.path)
    except HTTPException:
        raise
    except Exception:
        return


def get_current_user(
    request: Request = None,          # injected by FastAPI; needed for method + path
    token: str    = Depends(oauth2_scheme),
    db:    Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code = status.HTTP_401_UNAUTHORIZED,
        detail      = "Could not validate credentials",
        headers     = {"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    user_id = payload.get("user_id")
    if user_id is None:
        raise credentials_exception

    # A super-admin impersonation token carries this claim. Support must be able
    # to look inside a suspended tenant — that is most of the point of suspending
    # one — so it bypasses the organization gate below. Every action is still
    # attributed to the impersonated user in the audit trail.
    impersonated = payload.get("impersonated_by") is not None

    # Cache hit — skip DB query entirely (saves ~400ms per request)
    cached = _user_cache_get(user_id)
    if cached is not None:
        if not cached.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account access has been revoked.",
            )
        # Organization-level gate. Separately cached (30s) from the user row
        # (300s) because this DOES drive an authorization decision and must not
        # be pinned for five minutes after a suspension.
        _org_lifecycle.assert_active(db, cached, impersonated=impersonated)
        # Billing gate runs after the lifecycle gate: a suspended or deleted
        # organization is a harder stop than an unpaid one, and its 403 should win
        # over the billing 402.
        _billing_gate(request, db, cached)
        return cached

    # Cache miss — query DB once, then cache for 5 minutes
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception

    # Server-side access enforcement: a revoked/offboarded account must be
    # denied on EVERY authenticated request, not just at login. The offboarding
    # access-revocation engine flips `is_active=False`; honouring it here means a
    # still-unexpired access token cannot keep calling the API after exit —
    # closing the "API key still valid" / "former worker accesses dashboard"
    # gaps (zero post-exit access).
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account access has been revoked.",
        )

    # Same organization gate as the cache-hit branch above. Applied before the
    # user row is cached so a suspended tenant never warms the cache with a
    # principal that is about to be rejected anyway.
    _org_lifecycle.assert_active(db, user, impersonated=impersonated)

    # Detach from session before caching — make_transient preserves all column
    # values but removes the SQLAlchemy session binding so the object is safe
    # to reuse across requests without DetachedInstanceError.
    db.expunge(user)
    _sa_make_transient(user)
    _user_cache_set(user_id, user)
    _billing_gate(request, db, user)
    return user


# ==============================
# GET CURRENT ORGANIZATION
# ==============================
def get_current_organization(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Organization:
    if current_user.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not associated with any organization",
        )

    # Cache hit — skip the DB round-trip. See the _ORG_CACHE notes above for why
    # this is safe here (id-only reads, effectively immutable row).
    cached = _org_cache_get(current_user.organization_id)
    if cached is not None:
        return cached

    org = db.query(Organization).filter(Organization.id == current_user.organization_id).first()
    if org is None:
        # Deliberately not cached — a miss must not be pinned for the TTL.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    # Detach before caching so the object outlives this request's session
    # without raising DetachedInstanceError — same treatment get_current_user
    # gives the cached User.
    db.expunge(org)
    _sa_make_transient(org)
    _org_cache_set(org.id, org)
    return org


# ==============================
# SCHEMAS
# ==============================
class SetPasswordRequest(BaseModel):
    token:    str
    password: str
    name:     str = ""     # full name — employee fills this on signup page


# ==============================
# VERIFY INVITE TOKEN  GET /auth/invite/verify/{token}
# Called by SetPassword.jsx on mount to validate token + pre-fill email/name
# ==============================
@router.get("/invite/verify/{token}")
def verify_invite(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.invite_token == token).first()
    if not user:
        return {"valid": False}
    if user.invite_token_expires_at and datetime.now(timezone.utc) > _as_utc_aware(user.invite_token_expires_at):
        return {"valid": False, "reason": "expired"}
    return {
        "valid": True,
        "email": user.email,
        "name":  user.name or "",
    }

class ForgotPasswordRequest(BaseModel):
    email: str


# ==============================
# LOGIN  POST /auth/login
# ==============================
def _query_user_by_email(db: Session, email: str):
    """Query user with one DB retry on transient connection errors."""
    import time as _t
    from sqlalchemy.exc import OperationalError
    for attempt in range(2):
        try:
            return db.query(User).filter(User.email == email).first()
        except OperationalError:
            if attempt == 0:
                db.rollback()
                _t.sleep(0.3)
                continue
            raise HTTPException(
                status_code=503,
                detail="Server is busy — please try again in a few seconds.",
            )


# ── Super-admin bootstrap ─────────────────────────────────────────────────────
# A platform owner is anyone whose email appears in the SUPER_ADMIN_EMAILS env
# var (comma-separated, case-insensitive). On every successful login we auto-
# promote that account's role to `super_admin` so the platform-ops portal at
# /super-admin becomes reachable for them.
#
# Why env-driven rather than a DB seed:
#   • lets the operator add/remove super admins without a code change
#   • survives `users` table resets in fresh deploys / restores
#   • the env var is the only source of authority — no in-product UI can
#     grant super-admin, which is what we want for a billing-affecting role
def _super_admin_emails() -> set[str]:
    raw = os.getenv("SUPER_ADMIN_EMAILS", "") or os.getenv("SUPER_ADMIN_EMAIL", "")
    return {
        e.strip().lower()
        for e in raw.replace(";", ",").split(",")
        if e.strip()
    }


def _maybe_promote_super_admin(db: Session, user: User) -> bool:
    """If `user.email` is in SUPER_ADMIN_EMAILS but the user's stored role
    isn't `super_admin`, upgrade them in-place. Returns True when a change
    is made so the caller can audit-log it. Conversely, if a user's role
    is `super_admin` but their email has been removed from the env var,
    we downgrade them to `admin` so revocation is one config change away.

    Super-admin accounts are platform-layer identities and must not belong
    to any tenant org — organization_id is always cleared to NULL on promotion
    so audit logs and org-scoped guards behave correctly."""
    promoted = False
    targets = _super_admin_emails()
    email = (user.email or "").lower()
    if email in targets and user.role != "super_admin":
        user.role = "super_admin"
        user.organization_id = None  # platform layer — no tenant affiliation
        promoted = True
    elif email in targets and user.role == "super_admin" and user.organization_id is not None:
        # Already super_admin but still carrying a tenant org_id (e.g. migrated
        # from an existing admin account). Clear it on every login until clean.
        user.organization_id = None
        promoted = True
    elif user.role == "super_admin" and email not in targets:
        # Self-heal: a removed email shouldn't keep its super-admin role.
        user.role = "admin"
        promoted = True
    return promoted


def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency for routes that only platform owners may call.
    Raises 403 for every other role, including `admin`."""
    if current_user.role != "super_admin":
        raise HTTPException(
            status_code=403,
            detail="Super-admin access required",
        )
    return current_user


def require_active_organization(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Explicit form of the tenant-lifecycle gate.

    `get_current_user` already applies this to every authenticated request, so
    no existing route needs it — enforcement is global by construction rather
    than by remembering to annotate each endpoint. It is exported so that the
    guarantee has a greppable name, and so a future route that authenticates
    by some other path can still opt in with one dependency.
    """
    _org_lifecycle.assert_active(db, current_user)
    return current_user


# ==============================
# SESSION ESTABLISHMENT  (login / sso / activation share this)
# ==============================
def _create_user_session(db: Session, user: User, request: Optional[Request], family_id: str) -> UserSession:
    """Create the device row that backs the Profile 'Active devices' list.

    Keyed by SHA-256(family_id) — a value that is STABLE across access-token
    rotation, so the device row (and its last-seen time) survives every
    /auth/refresh instead of a new row appearing each rotation.
    """
    ip = _client_ip(request)
    ua = (request.headers.get("user-agent") if request is not None else "") or ""
    meta = _parse_user_agent(ua)
    sess = UserSession(
        user_id     = user.id,
        token_hash  = hashlib.sha256(family_id.encode("utf-8")).hexdigest(),
        user_agent  = ua[:512],
        device_type = meta["device_type"],
        device_name = meta["device_name"],
        browser     = meta["browser"],
        os          = meta["os"],
        ip_address  = ip,
    )
    db.add(sess)
    db.flush()  # assign sess.id so it can go into the access-token `sid` claim
    return sess


def establish_session(
    db: Session,
    user: User,
    request: Optional[Request],
    *,
    remember: bool,
) -> dict:
    """Mint a full authenticated session: device row + access JWT (carrying the
    stable `sid` claim) + rotating refresh token, and write the login audit row.

    Returns {access_token, refresh_token, expires_in, session_id, remember}.
    The caller decides how to DELIVER the refresh token (HttpOnly cookie for
    web, JSON body for desktop) — see the login endpoints.

    Raises 403 when the user's organization is suspended. This is the single
    place the check belongs: /auth/login, /auth/login-json, /auth/set-password
    and both SSO callbacks all mint their session here, so none of them can
    acquire a session for a suspended tenant, and none of them needs its own
    copy of the rule.
    """
    _org_lifecycle.assert_active(db, user)

    family_id = rtokens.new_family_id()

    # Device row first (best-effort — never block login on session bookkeeping).
    session_id: Optional[int] = None
    try:
        sess = _create_user_session(db, user, request, family_id)
        session_id = sess.id
    except Exception:
        db.rollback()
        session_id = None

    access_token = create_access_token({
        "user_id": user.id,
        "role":    user.role,
        "sid":     session_id,
    })

    raw_refresh, _row = rtokens.issue_refresh_token(
        db,
        user_id    = user.id,
        session_id = session_id,
        remember   = remember,
        family_id  = family_id,
    )

    # Audit trail — powers the Login IP report + login history.
    try:
        ip = _client_ip(request)
        ua = (request.headers.get("user-agent") if request is not None else "") or ""
        db.add(AuditLog(
            actor_id   = user.id,
            target_id  = user.id,
            entity     = "User",
            action     = "UserLogin",
            payload    = {"email": user.email, "role": user.role,
                          "user_agent": ua[:255], "remember": bool(remember)},
            ip_address = ip,
        ))
    except Exception:
        pass

    db.commit()

    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "expires_in":   ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "session_id":   session_id,
        "remember":     bool(remember),
    }


def _deliver_session(result: dict, request: Request, response: Response, remember: bool) -> str:
    """Attach the refresh token to the response via the right channel and
    return the access token. Web → HttpOnly cookie. Desktop → caller adds it to
    the JSON body (we DON'T set a cookie there, so the token is never delivered
    over two channels — that would trip single-use rotation reuse-detection)."""
    if not rtokens.is_desktop_client(request):
        rtokens.set_refresh_cookie(response, result["refresh_token"], remember)
    return result["access_token"]


@router.post("/login")
@limiter.limit("200/minute")
def login(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db:   Session                   = Depends(get_db),
):
    user = _query_user_by_email(db, form.username.lower())

    if not user or not user.password:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(form.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    if _maybe_promote_super_admin(db, user):
        try:
            db.add(AuditLog(
                actor_id=user.id, target_id=user.id,
                organization_id=user.organization_id,
                entity="User", action="RolePromotedSuperAdmin",
                payload={"email": user.email, "new_role": user.role},
            ))
            db.commit()
        except Exception:
            db.rollback()

    # The OAuth2 form endpoint (Swagger / CLI) defaults to a persistent session.
    result = establish_session(db, user, request, remember=True)
    access_token = _deliver_session(result, request, response, remember=True)

    body = {
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   result["expires_in"],
        "user": {
            "id":              user.id,
            "name":            user.name,
            "email":           user.email,
            "role":            user.role,
            "organization_id": user.organization_id,
        },
    }
    if rtokens.is_desktop_client(request):
        body["refresh_token"] = result["refresh_token"]
    return body


# ==============================
# LOGIN JSON  POST /auth/login-json
# Accepts JSON body instead of form data
# ==============================
class LoginJsonRequest(BaseModel):
    email: str
    password: str
    # "Remember for 30 days" — drives refresh-token lifetime + cookie Max-Age.
    # Defaults True so existing callers keep their persistent behaviour.
    remember: bool = True

@router.post("/login-json")
@limiter.limit("200/minute")
def login_json(data: LoginJsonRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user = _query_user_by_email(db, data.email.lower())

    if not user or not user.password:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(data.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    if _maybe_promote_super_admin(db, user):
        try:
            db.add(AuditLog(
                actor_id=user.id, target_id=user.id,
                organization_id=user.organization_id,
                entity="User", action="RolePromotedSuperAdmin",
                payload={"email": user.email, "new_role": user.role},
            ))
            db.commit()
        except Exception:
            db.rollback()

    remember = bool(data.remember)
    result = establish_session(db, user, request, remember=remember)
    access_token = _deliver_session(result, request, response, remember=remember)

    body = {
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   result["expires_in"],
        "user": {
            "id":              user.id,
            "name":            user.name,
            "email":           user.email,
            "role":            user.role,
            "organization_id": user.organization_id,
        },
    }
    if rtokens.is_desktop_client(request):
        body["refresh_token"] = result["refresh_token"]
    return body


# ==============================
# SET PASSWORD  POST /auth/set-password
# Called when employee clicks the invite link
# ==============================
@router.post("/set-password")
def set_password(data: SetPasswordRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    # Look up user by invite token
    user = db.query(User).filter(User.invite_token == data.token).first()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired invite link")

    # Check token has not expired
    if user.invite_token_expires_at and datetime.now(timezone.utc) > _as_utc_aware(user.invite_token_expires_at):
        raise HTTPException(status_code=400, detail="Invite link has expired — ask admin to resend")

    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Update name if provided (employee fills it on signup page)
    if data.name and data.name.strip():
        user.name = data.name.strip()

    # Set password and clear invite token
    user.password                = hash_password(data.password)
    user.is_password_set         = True
    user.invite_token            = None   # one-time use — invalidate after use
    user.invite_token_expires_at = None
    user.is_active               = True

    db.commit()
    db.refresh(user)

    # ZT-ONB-02 account-activated welcome (Operational; fail-open). Best-effort:
    # never block activation on email. No preference context is passed, so the
    # welcome always sends. reply-to = the inviting admin when available.
    try:
        from app.email_service import send_account_activated_email
        from database.models import Organization
        _org = (db.query(Organization).filter(Organization.id == user.organization_id).first()
                if user.organization_id else None)
        _inviter = (db.query(User).filter(User.id == user.invited_by_id).first()
                    if user.invited_by_id else None)
        send_account_activated_email(
            to_email=user.email, name=user.name or "", role_name=user.role or "",
            workspace_name=(_org.name if _org else ""),
            reply_to=(_inviter.email if _inviter else None),
        )
    except Exception:
        _auth_logger.warning("[set-password] ZT-ONB-02 activation email failed (activation preserved)", exc_info=True)

    # Establish a full persistent session so the just-activated user is
    # auto-logged-in AND survives the short access-token lifetime via refresh.
    result = establish_session(db, user, request, remember=True)
    access_token = _deliver_session(result, request, response, remember=True)

    body = {
        "message":      "Account activated successfully",
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   result["expires_in"],
        "user_id":      user.id,
        "name":         user.name,
        "email":        user.email,
        "role":         user.role,
        "user": {
            "id":              user.id,
            "name":            user.name,
            "email":           user.email,
            "role":            user.role,
            "organization_id": user.organization_id,
        },
    }
    if rtokens.is_desktop_client(request):
        body["refresh_token"] = result["refresh_token"]
    return body


# ==============================
# FORGOT PASSWORD RATE LIMIT
# ==============================
# In-memory throttle: at most one reset-mail send per email per 60 s.
# This is intentionally NOT a token bucket per IP — IP-based limits hurt
# legitimate users behind a corporate NAT/VPN, while per-email limits
# directly address the abuse case (someone spamming /forgot-password to
# flood a victim's inbox).
#
# Caveats:
#   • In-memory only. On a multi-instance deploy (Cloud Run minInstances>1
#     scales to several pods) each pod has its own dict, so a determined
#     attacker can still get N sends per minute where N = pod count.
#     If we move to higher scale, swap this for Redis (SETEX with EX=60).
#   • Throttled requests still return the same 200 + generic message so
#     the rate limiter doesn't itself become an enumeration oracle.
_FORGOT_PWD_RATE_WINDOW_SEC  = 10 * 60   # 10-minute sliding window
_FORGOT_PWD_MAX_PER_WINDOW   = 3         # max OTP sends per email per window
_OTP_TTL_SECONDS             = 5 * 60    # 5-minute OTP lifetime
_OTP_MAX_VERIFY_ATTEMPTS     = 5         # lock the OTP after this many wrong guesses
_VERIFIED_OTP_GRACE_SECONDS  = 10 * 60   # how long after verify a user has to set the new password

_forgot_pwd_sends: dict[str, list[float]] = {}   # email_lc → list of monotonic timestamps
_forgot_pwd_lock = threading.Lock()


def _forgot_password_within_throttle(email_lc: str) -> bool:
    """True if `email_lc` has already used its quota in the current window."""
    cutoff = _time.monotonic() - _FORGOT_PWD_RATE_WINDOW_SEC
    with _forgot_pwd_lock:
        history = _forgot_pwd_sends.get(email_lc, [])
        # Drop expired stamps and keep only those still inside the window.
        history = [t for t in history if t >= cutoff]
        _forgot_pwd_sends[email_lc] = history
        return len(history) >= _FORGOT_PWD_MAX_PER_WINDOW


def _mark_forgot_password_sent(email_lc: str) -> None:
    with _forgot_pwd_lock:
        bucket = _forgot_pwd_sends.setdefault(email_lc, [])
        bucket.append(_time.monotonic())
        # Cap memory growth from people probing random fake emails.
        if len(_forgot_pwd_sends) > 5000:
            cutoff = _time.monotonic() - _FORGOT_PWD_RATE_WINDOW_SEC * 2
            stale = [e for e, ts in _forgot_pwd_sends.items() if all(t < cutoff for t in ts)]
            for e in stale:
                _forgot_pwd_sends.pop(e, None)


def _generate_otp_code() -> str:
    """4-digit OTP, padded so the user always sees four characters.

    `secrets.randbelow` is preferred over `random.randint` because it draws
    from the OS CSPRNG — appropriate for anything used as a credential.
    """
    import secrets
    return f"{secrets.randbelow(10000):04d}"


def _hash_otp(code: str) -> str:
    """HMAC-SHA256 of the OTP, keyed with SECRET_KEY.

    Deterministic so /verify-otp can recompute and constant-time compare, but a
    database read never reveals a live code — the plaintext OTP exists only long
    enough to be emailed and is never persisted.
    """
    key = (SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, (code or "").encode("utf-8"), hashlib.sha256).hexdigest()


# Background worker — runs AFTER the response is sent to the client.
# Owns its own DB session because the request-scoped one is already
# closed by the time this fires.
def _send_otp_email_and_audit(user_id: int, to_email: str, name: str,
                              otp_code: str, expires_at_iso: str) -> None:
    from app.email_service import send_password_reset_otp_email
    from database.database import SessionLocal

    sent = False
    error_msg: Optional[str] = None
    try:
        sent = bool(send_password_reset_otp_email(
            to_email = to_email,
            name     = name or "there",
            otp_code = otp_code,
        ))
        if not sent:
            error_msg = "Email transport failed — see backend email_logs.txt"
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        _auth_logger.error(
            "[forgot-password] OTP send failed for %s: %s\n%s",
            to_email, error_msg, traceback.format_exc(),
        )

    # Audit row — admins can verify reset attempts after the fact. The OTP
    # itself is NEVER logged. We open a fresh session here because the
    # request-scoped session is closed once the response is flushed.
    audit_db = SessionLocal()
    try:
        _otp_user = audit_db.query(User).filter(User.id == user_id).first()
        _otp_org_id = _otp_user.organization_id if _otp_user else None
        audit_db.add(AuditLog(
            actor_id        = user_id,
            target_id       = user_id,
            organization_id = _otp_org_id,
            entity          = "PasswordReset",
            action          = "OtpSent" if sent else "OtpSendFailed",
            payload         = {
                "email":           to_email,
                "delivery_status": "delivered" if sent else "failed",
                "error":           error_msg,
                "expires_at":      expires_at_iso,
            },
        ))
        audit_db.commit()
    except Exception as e:
        audit_db.rollback()
        _auth_logger.warning("[forgot-password] audit insert failed: %s", e)
    finally:
        audit_db.close()


# ==============================
# FORGOT PASSWORD  POST /auth/forgot-password
# Generates a 4-digit OTP and emails it to the user
# ==============================
@router.post("/forgot-password")
@limiter.limit("60/minute")
def forgot_password(
    request: Request,
    data: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Send a 4-digit OTP to a registered work email.

    Account existence:
      Per the Employee Login spec this endpoint tells the caller whether an
      account exists ("No account found with this work email."). That is a
      deliberate product choice for an INTERNAL workforce tool where admins
      provision accounts — it trades the old anti-enumeration property for a
      clearer employee experience. Abuse is still bounded by the per-IP rate
      limit above and the per-email send throttle below, and every send is
      audit-logged. If you ever need anti-enumeration back, return the generic
      "if that email exists…" body in both branches instead.

    OTP storage:
      Only an HMAC-SHA256 of the code is stored (otp_hash); the plaintext code
      lives just long enough to be emailed. employee_id ties the row to the user.

    Response timing:
      The OTP row is saved, then the email is sent SYNCHRONOUSLY via Resend
      (~1-2 s) before responding. BackgroundTasks runs after the response is
      flushed, but Cloud Run throttles container CPU to ~0% at that instant
      (no --no-cpu-throttling), which froze the Resend call mid-flight in prod
      so codes never went out. Resend's HTTP API is fast, well inside the SPA's
      45 s auth timeout.

    Email lookup is case-insensitive — User.email keeps its original casing but
    the OTP table normalises to lower case so verify can find it again.
    """
    incoming_email = (data.email or "").strip().lower()
    if not incoming_email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", incoming_email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")

    user = (
        db.query(User)
        .filter(_sa_func.lower(User.email) == incoming_email)
        .first()
    )
    if not user:
        _auth_logger.info("[forgot-password] No account for %s", incoming_email)
        return {
            "account_exists": False,
            "email_sent":     False,
            "message":        "No account found with this work email.",
        }

    # 3 OTP sends per email per 10 minutes. The account already proved to exist
    # above, so we can be transparent that a code went out recently.
    if _forgot_password_within_throttle(incoming_email):
        _auth_logger.info("[forgot-password] Throttled %s", incoming_email)
        return {
            "account_exists": True,
            "email_sent":     True,
            "throttled":      True,
            "message":        "A code was sent recently. Please check your inbox.",
        }

    # Invalidate any prior unverified OTPs for this email — only one live
    # code at a time. Verified rows are left alone so the post-verify
    # /reset-password call can still find them within the grace window.
    db.query(PasswordResetOTP).filter(
        PasswordResetOTP.email == incoming_email,
        PasswordResetOTP.is_verified.is_(False),
    ).delete(synchronize_session=False)

    otp_code = _generate_otp_code()
    otp_row = PasswordResetOTP(
        employee_id = user.id,
        email       = incoming_email,
        otp_hash    = _hash_otp(otp_code),
        expires_at  = datetime.now(timezone.utc) + timedelta(seconds=_OTP_TTL_SECONDS),
        is_verified = False,
        attempts    = 0,
    )
    db.add(otp_row)
    db.commit()
    db.refresh(otp_row)

    _mark_forgot_password_sent(incoming_email)

    # Synchronous send — see the docstring (Cloud Run CPU throttling froze the
    # post-response send). The plaintext code is passed to the mailer here and
    # nowhere else; only its hash was persisted above.
    _send_otp_email_and_audit(
        user.id,
        user.email,
        user.name or "there",
        otp_code,
        otp_row.expires_at.isoformat(),
    )

    return {
        "account_exists": True,
        "email_sent":     True,   # optimistic — true delivery state in the audit log
        "message":        "OTP has been sent successfully to your work email.",
    }


# ==============================
# VERIFY OTP  POST /auth/verify-otp
# Step 2 of the forgot-password flow. The SPA calls this with the 4-digit
# code; on success we mark the row verified and the user becomes eligible
# to call /auth/reset-password.
# ==============================
class VerifyOtpRequest(BaseModel):
    email: str
    otp:   str


@router.post("/verify-otp")
@limiter.limit("200/minute")
def verify_otp(request: Request, data: VerifyOtpRequest, db: Session = Depends(get_db)):
    incoming_email = (data.email or "").strip().lower()
    incoming_otp   = (data.otp or "").strip()

    if not incoming_email or not incoming_otp:
        raise HTTPException(status_code=400, detail="Email and OTP are required.")
    if not incoming_otp.isdigit() or len(incoming_otp) != 4:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    # Newest unverified row first — we expire the rest in /forgot-password,
    # but order_by created_at DESC keeps us correct if that ever changes.
    row = (
        db.query(PasswordResetOTP)
        .filter(
            PasswordResetOTP.email == incoming_email,
            PasswordResetOTP.is_verified.is_(False),
        )
        .order_by(PasswordResetOTP.created_at.desc())
        .first()
    )

    if not row:
        raise HTTPException(status_code=400, detail="Invalid OTP. Please try again.")

    if datetime.now(timezone.utc) > _as_utc_aware(row.expires_at):
        # Remove the expired row so a follow-up resend starts clean.
        db.delete(row); db.commit()
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new OTP.")

    if row.attempts >= _OTP_MAX_VERIFY_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Please request a new OTP.")

    # New rows store an HMAC of the code; legacy rows may still carry plaintext.
    # Constant-time compare in both cases so a wrong code can't be timed.
    if row.otp_hash:
        ok = hmac.compare_digest(row.otp_hash, _hash_otp(incoming_otp))
    else:
        ok = bool(row.otp_code) and hmac.compare_digest(row.otp_code, incoming_otp)
    if not ok:
        row.attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid OTP. Please try again.")

    row.is_verified = True
    # Reset the verify-side counter once the code is correct so an attacker
    # who learns the OTP can't probe the reset endpoint forever.
    row.attempts = 0
    # Trim TTL to the grace window — the user has 10 minutes from a
    # successful verify to set the new password before they have to
    # re-OTP. Keeps the eligibility window predictable.
    row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=_VERIFIED_OTP_GRACE_SECONDS)
    db.commit()

    return {"success": True, "message": "OTP verified successfully"}


# ==============================
# RESET PASSWORD  POST /auth/reset-password
# Step 3 — accepts the new password once a verified OTP row exists
# inside the grace window for the given email.
# ==============================
class ResetPasswordRequest(BaseModel):
    """Reset-password payload.

    Accepts the new password under any of these three field names:
      • `newPassword` — the camelCase name the public spec / SPA uses
      • `new_password` — snake_case variant for curl/CLI users
      • `password` — legacy name kept so older clients keep working

    `populate_by_name=True` is what makes the alias optional — without it
    Pydantic v2 would *only* accept the alias and refuse the canonical
    field name on the wire.
    """
    model_config = ConfigDict(populate_by_name=True)

    email: str
    new_password: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("new_password", "newPassword"),
    )
    password: Optional[str] = None


# Password rules — same set the SPA enforces on the reset form. Pattern,
# human-readable label. Used server-side as defence in depth: the
# frontend rules are easy to bypass with curl.
_PASSWORD_RULES = (
    (r".{8,}",      "at least 8 characters"),
    (r"[A-Z]",      "an uppercase letter"),
    (r"[a-z]",      "a lowercase letter"),
    (r"[0-9]",      "a number"),
    (r"[@$!%*?&]",  "a special character (@$!%*?&)"),
)


def _validate_password_strength(password: str) -> Optional[str]:
    """Return the human-readable failure message, or None when valid."""
    missing = [label for pattern, label in _PASSWORD_RULES if not re.search(pattern, password)]
    if missing:
        return "Password must contain " + ", ".join(missing) + "."
    return None


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest, db: Session = Depends(get_db)):
    incoming_email = (data.email or "").strip().lower()
    new_password   = data.new_password or data.password

    if not incoming_email or not new_password:
        raise HTTPException(
            status_code=400,
            detail="Email and password are required.",
        )

    err = _validate_password_strength(new_password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Must have a verified-and-unexpired OTP for this email.
    otp_row = (
        db.query(PasswordResetOTP)
        .filter(
            PasswordResetOTP.email == incoming_email,
            PasswordResetOTP.is_verified.is_(True),
        )
        .order_by(PasswordResetOTP.created_at.desc())
        .first()
    )
    if not otp_row or datetime.now(timezone.utc) > _as_utc_aware(otp_row.expires_at):
        raise HTTPException(
            status_code=400,
            detail="OTP verification required or expired. Please restart the reset flow.",
        )

    user = (
        db.query(User)
        .filter(_sa_func.lower(User.email) == incoming_email)
        .first()
    )
    if not user:
        raise HTTPException(status_code=400, detail="Account not found")

    user.password        = hash_password(new_password)
    user.is_password_set = True
    user.is_active       = True
    # Setting a password is acceptance: the worker moves from a reserved
    # (unbilled) invitation to a billable seat (ZT-COM-BILL-001 §A1 — "the
    # billable event is acceptance/activation of the worker"). Best-effort seat
    # resync; a Stripe outage must never block someone setting their password.
    #
    # enforce_capacity=False deliberately. If the employer is at its plan ceiling, the
    # person setting their password cannot upgrade the plan — refusing here would show
    # them a failure they have no way to resolve, over their employer's billing. The
    # ceiling is enforced earlier instead, when an admin invites or adds someone
    # (service.assert_room_for_new_worker), where it reaches someone who can act. An
    # overage that slips through is logged and shown to admins as a red billing alert.
    from app.billing import seats as billing_seats, worker_state as _ws  # noqa: PLC0415
    billing_seats.set_worker_billing_state(
        db, user, _ws.ACTIVE_BILLABLE, reason="password_set",
        enforce_capacity=False,
    )
    # Any stale invite/reset-link token on the User row is now meaningless —
    # the OTP path superseded it. Clear it so a leaked old link is dead too.
    user.invite_token            = None
    user.invite_token_expires_at = None

    # Burn every OTP for this email — used and unused alike. Defence in
    # depth: the verified row above is now non-replayable and any stray
    # unverified rows can't be used to bypass another reset later.
    db.query(PasswordResetOTP).filter(
        PasswordResetOTP.email == incoming_email,
    ).delete(synchronize_session=False)

    # Invalidate every existing session so an attacker who already grabbed
    # a JWT can't keep using it after the rightful owner regains the account.
    # `is_revoked` is the existing kill-switch the SessionsService honours.
    try:
        db.query(UserSession).filter(
            UserSession.user_id == user.id,
            UserSession.is_revoked.is_(False),
        ).update(
            {"is_revoked": True, "revoked_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
        # Kill every persistent "Remember me" refresh token too, so a stolen
        # 30-day token can't outlive the password the owner just changed.
        rtokens.revoke_all_for_user(db, user.id, "password_reset")
    except Exception as e:
        # Best-effort — never block the password reset on session bookkeeping.
        _auth_logger.warning("[reset-password] session revoke failed: %s", e)

    # Audit log — admins can trace which email actually completed the reset.
    try:
        db.add(AuditLog(
            actor_id        = user.id,
            target_id       = user.id,
            organization_id = user.organization_id,
            entity          = "PasswordReset",
            action          = "PasswordResetCompleted",
            payload         = {"email": user.email},
        ))
    except Exception:
        pass

    db.commit()

    # ZT-IAM-05 password-changed notification (Essential - security). Best-effort:
    # a mail failure must never block the completed reset.
    try:
        from app.email_service import send_password_changed_email
        send_password_changed_email(user.email, user.name or "")
    except Exception:
        _auth_logger.warning("[reset-password] ZT-IAM-05 email failed (reset preserved)", exc_info=True)

    # No auto-login — the spec asks the user to re-authenticate with the
    # new password. This is also the safer choice: it makes the user prove
    # they know what they just set.
    return {
        "success": True,
        "message": "Password updated successfully",
    }


# ==============================
# ME  GET /auth/me
# ==============================
@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "id":              current_user.id,
        "name":            current_user.name,
        "email":           current_user.email,
        "role":            current_user.role,
        "designation":     current_user.designation,
        "department":      current_user.department,
        "team":            current_user.team,
        "profile_picture": current_user.profile_picture,
        "organization_id": current_user.organization_id,
    }


# ==============================
# PATCH /auth/me — update own name and/or profile picture
# Used by the Account Settings page.
# ==============================
class UpdateMeRequest(BaseModel):
    name:            Optional[str] = None
    profile_picture: Optional[str] = None   # data URL (base64) or null to clear


@router.patch("/me")
def update_me(
    data: UpdateMeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # current_user is a detached copy from the identity cache (make_transient),
    # so mutating it never persists and db.refresh() on it raises. Load the live
    # row, mutate that, drop the cache.
    user = db.query(User).filter(User.id == current_user.id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if data.name is not None:
        trimmed = data.name.strip()
        if not trimmed:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        user.name = trimmed

    if data.profile_picture is not None:
        # Guard against oversized payloads — base64 roughly 1.37× binary size.
        # 2 MB of base64 ≈ 1.5 MB image, plenty for an avatar.
        if len(data.profile_picture) > 2_000_000:
            raise HTTPException(status_code=400, detail="Profile picture too large (max ~1.5 MB)")
        user.profile_picture = data.profile_picture or None

    db.commit()
    db.refresh(user)
    _user_cache_invalidate(user.id)
    return {
        "id":              user.id,
        "name":            user.name,
        "email":           user.email,
        "role":            user.role,
        "profile_picture": user.profile_picture,
    }


# ══════════════════════════════════════════════════════════════════════════════
# REFRESH-TOKEN LIFECYCLE
#   POST /auth/refresh     — exchange a refresh token for a new access token
#   POST /auth/logout      — revoke THIS device's refresh token + clear cookie
#   POST /auth/logout-all  — revoke every refresh token + device for the user
#   GET  /auth/session     — validate the current access token (session probe)
# ══════════════════════════════════════════════════════════════════════════════
class RefreshRequest(BaseModel):
    # Web clients send nothing (the token rides in the HttpOnly cookie).
    # Desktop (Electron) posts the token it holds in OS-encrypted safeStorage.
    refresh_token: Optional[str] = None


def _refresh_input(request: Request, body: Optional[RefreshRequest]) -> Optional[str]:
    """Pick the refresh token from the body (desktop) or the cookie (web)."""
    if body and body.refresh_token:
        return body.refresh_token
    return rtokens.read_refresh_cookie(request)


@router.post("/refresh")
def refresh(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = Body(default=None),
    db: Session = Depends(get_db),
):
    """Rotate the refresh token and mint a fresh access token.

    On any failure we clear the cookie and return 401 so the client falls back
    to the login screen. The `reason` field lets the SPA distinguish a benign
    expiry from a security event (reuse_detected) if it wants to.
    """
    desktop = rtokens.is_desktop_client(request)
    raw = _refresh_input(request, body)

    try:
        new_raw, new_row = rtokens.rotate(db, raw)
    except rtokens.RefreshError as e:
        rtokens.clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail={"error": "refresh_failed", "reason": e.reason})

    user = db.query(User).filter(User.id == new_row.user_id).first()
    if user is None or not user.is_active:
        rtokens.revoke_family(db, new_row.family_id, "user_inactive")
        db.commit()
        rtokens.clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail={"error": "refresh_failed", "reason": "user_inactive"})

    # Organization gate. Without this the 30-day rotating refresh window keeps
    # re-arming a suspended tenant indefinitely — the access token expires every
    # 30 minutes but is silently replaced, so suspension would never take hold
    # on a long-lived desktop or browser session.
    #
    # Deliberately 401, not 403, and shaped like the other refresh failures: both
    # clients already treat a 401 from /auth/refresh as a definitive end of
    # session (web: api/refresh.js `performRefresh` -> false -> redirectToLogin;
    # desktop: main.js `refreshAccessToken` -> performLogout). A 403 would fall
    # into the desktop tracker's "transient — keep the session" branch and it
    # would retry forever. The refresh family is revoked so the token cannot be
    # replayed after a later restore.
    if _org_lifecycle.is_suspended(db, getattr(user, "organization_id", None)):
        rtokens.revoke_family(db, new_row.family_id, "organization_suspended")
        db.commit()
        rtokens.clear_refresh_cookie(response)
        raise HTTPException(
            status_code=401,
            detail={"error": "refresh_failed", "reason": _org_lifecycle.ERR_ORG_SUSPENDED},
        )

    access_token = create_access_token({
        "user_id": user.id,
        "role":    user.role,
        "sid":     new_row.session_id,
    })
    db.commit()

    out = {
        "access_token": access_token,
        "token_type":   "bearer",
        "expires_in":   ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": {
            "id":    user.id,
            "name":  user.name,
            "email": user.email,
            "role":  user.role,
            "profile_picture": user.profile_picture,
        },
    }
    if desktop:
        out["refresh_token"] = new_raw
    else:
        rtokens.set_refresh_cookie(response, new_raw, bool(new_row.remember))
    return out


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = Body(default=None),
    db: Session = Depends(get_db),
):
    """Destroy the active session: revoke its refresh token, mark the device
    row revoked, and clear the cookie. Idempotent — safe to call with no token."""
    raw = _refresh_input(request, body)

    if raw:
        row = (
            db.query(RefreshToken)
            .filter_by(token_hash=rtokens.hash_token(raw))
            .first()
        )
        if row and not row.is_revoked:
            row.is_revoked     = True
            row.revoked_at     = datetime.now(timezone.utc)
            row.revoked_reason = "logout"
            # Take the device row offline too so it leaves the Active devices list.
            if row.session_id:
                sess = db.query(UserSession).filter(UserSession.id == row.session_id).first()
                if sess and not sess.is_revoked:
                    sess.is_revoked = True
                    sess.revoked_at = datetime.now(timezone.utc)
            db.commit()

    rtokens.clear_refresh_cookie(response)
    return {"success": True}


@router.post("/logout-all")
def logout_all(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sign out everywhere — revoke every refresh token and device for the
    current user. Used by 'Sign out of all devices' / after a security event."""
    n = rtokens.revoke_all_for_user(db, current_user.id, "logout_all")
    try:
        db.query(UserSession).filter(
            UserSession.user_id == current_user.id,
            UserSession.is_revoked.is_(False),
        ).update(
            {"is_revoked": True, "revoked_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
    except Exception:
        pass
    db.commit()
    rtokens.clear_refresh_cookie(response)
    return {"success": True, "revoked": n}


@router.get("/session")
def session_probe(
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
):
    """Lightweight 'is my access token still valid?' probe. 401s automatically
    (via get_current_user) when the token is missing/expired/invalid, so the
    client can decide whether to attempt a silent /auth/refresh."""
    payload = decode_access_token(token) or {}
    exp = payload.get("exp")
    return {
        "valid": True,
        "user": {
            "id":              current_user.id,
            "name":            current_user.name,
            "email":           current_user.email,
            "role":            current_user.role,
            "profile_picture": current_user.profile_picture,
        },
        "session": {
            "sid":        payload.get("sid"),
            "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None,
        },
    }