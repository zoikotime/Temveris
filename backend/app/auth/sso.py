"""
Google SSO (OAuth 2.0 authorization-code flow).

The provider follows this shape:

  ┌──────────┐   1. GET /auth/{provider}/login?remember=1
  │ Frontend │ ─────────────────────────────────────────► Backend builds the
  └──────────┘                                            provider auth URL and
       ▲                                                  302-redirects there.
       │
       │ 4. 302 /sso/callback?token=...&remember=...
       │
  ┌──────────┐   3. GET /auth/{provider}/callback?code=...&state=...
  │  Backend │ ◄──────────────────────────────────────────  Provider redirects
  └──────────┘    here after the user consents. Backend
                  exchanges the code for tokens, looks up
                  the user by email, mints our JWT, and
                  bounces to the SPA's /sso/callback route.

The `state` value is HMAC-signed (SECRET_KEY) and carries the `remember` flag
plus a timestamp. That gives CSRF protection without needing a server-side
store — a forged state can't be HMAC-validated, and an old/replayed state
expires after STATE_MAX_AGE_SEC.

Provisioning policy: this app is invite-only — SSO does NOT auto-create
accounts. Unknown emails are bounced back to /sso/callback?error=no-account so
the SPA shows a friendly "ask your admin to invite you" message instead of
silently creating a free-tier account.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func as _sa_func

from database.database import get_db
from database.models import AuditLog, AppSettings, User
from app.auth.service import (
    _record_login,
    _maybe_promote_super_admin,
    create_access_token,
    establish_session,
)
from app.auth import tokens as rtokens

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # service.py already raises on this; this is defensive in case sso.py is
    # imported standalone (e.g. by a test) and the env didn't load.
    raise RuntimeError("SECRET_KEY is not set — refusing to start SSO module")

BACKEND_URL  = (os.getenv("BACKEND_URL")  or "http://localhost:8000").rstrip("/")
FRONTEND_URL = (os.getenv("FRONTEND_URL") or "http://localhost:5173").rstrip("/")

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")

STATE_MAX_AGE_SEC = 10 * 60   # 10-minute redirect window
HTTP_TIMEOUT_SEC  = 10        # provider token/userinfo calls


# ──────────────────────────────────────────────────────────────────────────────
# HMAC-signed `state` (CSRF protection + carrier for the `remember` flag)
# ──────────────────────────────────────────────────────────────────────────────
def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign_state(payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body_b64 = _b64u_encode(body)
    sig = hmac.new(SECRET_KEY.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{body_b64}.{_b64u_encode(sig)}"


def _verify_state(token: str) -> Optional[dict]:
    try:
        body_b64, sig_b64 = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(SECRET_KEY.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        got = _b64u_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(got, expected):
        return None
    try:
        payload = json.loads(_b64u_decode(body_b64).decode("utf-8"))
    except Exception:
        return None
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)) or (time.time() - ts) > STATE_MAX_AGE_SEC:
        return None
    return payload


# ──────────────────────────────────────────────────────────────────────────────
# Frontend bounce helpers — every SSO failure path lands the user back on the
# SPA's /sso/callback page with a structured error query string so the SPA can
# render a tailored message (e.g. "no account for this email — ask admin").
# ──────────────────────────────────────────────────────────────────────────────
def _frontend_redirect(query: dict) -> RedirectResponse:
    return RedirectResponse(url=f"{FRONTEND_URL}/sso/callback?{urlencode(query)}", status_code=302)


def _redirect_with_token(token: str, provider: str, remember: bool, user: User) -> RedirectResponse:
    return _frontend_redirect({
        "token":    token,
        "provider": provider,
        "remember": "1" if remember else "0",
        "role":     user.role or "employee",
        "name":     user.name or "",
        "email":    user.email or "",
    })


def _redirect_with_error(provider: str, error: str, email: str = "") -> RedirectResponse:
    return _frontend_redirect({
        "provider": provider,
        "error":    error,
        "email":    email,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Org-aware SSO user resolution
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_sso_user(db: Session, email_lc: str, provider: str) -> Optional[User]:
    """Return the User row for *email_lc* scoped to the correct organization.

    Single-tenant (or email globally unique): returns the one matching row as
    before, no behaviour change.

    Multi-tenant same-email collision: disambiguates by checking which
    organization has *provider* configured as its active SSO provider in
    AppSettings (key="security.sso.provider"). The user whose org has that
    provider enabled is the intended recipient of the SSO callback.

    Resolution order:
      1. Fetch all User rows matching the lowercased email.
      2. Exactly one match → return it directly (common case, no overhead).
      3. Multiple matches → narrow to users whose org has AppSettings
         key="security.sso.provider" with value==provider (org-scoped).
      4. Exactly one survives the filter → return it.
      5. Zero or still multiple → unresolvable; return None so the caller
         issues a safe deny (prevents cross-tenant account hijacking).
    """
    users = db.query(User).filter(_sa_func.lower(User.email) == email_lc).all()
    if not users:
        return None
    if len(users) == 1:
        return users[0]

    # Multiple accounts share this email across orgs.  Use the SSO provider
    # configuration as the authoritative org signal.
    orgs_with_provider = {
        row.organization_id
        for row in db.query(AppSettings.organization_id).filter(
            AppSettings.key == "security.sso.provider",
            AppSettings.value == provider,
            AppSettings.organization_id.isnot(None),
        ).all()
    }
    eligible = [u for u in users if u.organization_id in orgs_with_provider]
    if len(eligible) == 1:
        return eligible[0]

    # Ambiguous — log and deny rather than guess.
    _log.warning(
        "[sso:%s] email %s matched %d users, %d sso-eligible — denying to prevent cross-tenant login",
        provider, email_lc, len(users), len(eligible),
    )
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Shared "log in this SSO user" path used by both providers' callbacks.
# Invite-only: never auto-create users. Email match is case-insensitive.
# ──────────────────────────────────────────────────────────────────────────────
def _login_sso_user(
    db: Session,
    request: Request,
    email: str,
    full_name: str,
    provider: str,
    remember: bool,
) -> RedirectResponse:
    email_lc = (email or "").strip().lower()
    if not email_lc:
        return _redirect_with_error(provider, "no-email")

    user = _resolve_sso_user(db, email_lc, provider)
    if not user:
        return _redirect_with_error(provider, "no-account", email_lc)

    if not user.is_active:
        return _redirect_with_error(provider, "account-disabled", user.email)

    # Backfill the display name on first SSO sign-in if the User row never had
    # one set (e.g. invited but never completed the password page).
    if full_name and not (user.name or "").strip():
        user.name = full_name.strip()

    if _maybe_promote_super_admin(db, user):
        try:
            db.add(AuditLog(
                actor_id=user.id, target_id=user.id,
                organization_id=user.organization_id,
                entity="User", action="RolePromotedSuperAdmin",
                payload={"email": user.email, "new_role": user.role, "via": f"sso:{provider}"},
            ))
        except Exception:
            db.rollback()

    try:
        db.add(AuditLog(
            actor_id=user.id, target_id=user.id,
            organization_id=user.organization_id,
            entity="User", action="SsoLogin",
            payload={"email": user.email, "provider": provider},
        ))
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()

    # SSO is always browser-based → mint a full persistent session and deliver
    # the rotating refresh token via the HttpOnly cookie on the redirect.
    result = establish_session(db, user, request, remember=remember)
    resp = _redirect_with_token(result["access_token"], provider, remember, user)
    rtokens.set_refresh_cookie(resp, result["refresh_token"], remember)
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# GOOGLE
# ──────────────────────────────────────────────────────────────────────────────
_GOOGLE_AUTH_URL     = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL    = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _google_redirect_uri() -> str:
    return f"{BACKEND_URL}/auth/google/callback"


@router.get("/google/login")
@router.get("/google/start")  # legacy alias — keep until all clients call /login
def google_start(remember: int = 1):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return _redirect_with_error("google", "not-configured")

    state = _sign_state({
        "ts":       int(time.time()),
        "n":        secrets.token_urlsafe(16),
        "remember": bool(remember),
        "provider": "google",
    })
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  _google_redirect_uri(),
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "online",
        "include_granted_scopes": "true",
        "prompt":        "select_account",
        "state":         state,
    }
    return RedirectResponse(url=f"{_GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if error or not code or not state:
        return _redirect_with_error("google", error or "missing-code")

    parsed = _verify_state(state)
    if not parsed:
        return _redirect_with_error("google", "bad-state")

    remember = bool(parsed.get("remember", True))

    try:
        token_resp = requests.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code":          code,
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri":  _google_redirect_uri(),
                "grant_type":    "authorization_code",
            },
            timeout=HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        _log.warning("[sso:google] token exchange failed: %s", exc)
        return _redirect_with_error("google", "token-exchange-failed")

    if token_resp.status_code != 200:
        _log.warning("[sso:google] token endpoint %s: %s", token_resp.status_code, token_resp.text[:300])
        return _redirect_with_error("google", "token-rejected")

    access_token = token_resp.json().get("access_token")
    if not access_token:
        return _redirect_with_error("google", "no-access-token")

    try:
        ui = requests.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as exc:
        _log.warning("[sso:google] userinfo failed: %s", exc)
        return _redirect_with_error("google", "userinfo-failed")

    if ui.status_code != 200:
        return _redirect_with_error("google", "userinfo-rejected")

    info = ui.json()
    # `email_verified` is included only when the user grants the scope; treat
    # the absence as untrusted — we don't want a Google account that hasn't
    # proven control of the email to claim a Temveris invite.
    if not info.get("email_verified", False):
        return _redirect_with_error("google", "email-not-verified", info.get("email", ""))

    return _login_sso_user(
        db, request,
        email     = info.get("email", ""),
        full_name = info.get("name", "") or info.get("given_name", ""),
        provider  = "google",
        remember  = remember,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Health probe — lets the frontend display the right "configure SSO first"
# nudge instead of a generic error if the operator hasn't set the env vars.
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/sso/providers")
def sso_providers():
    return {
        "google":    bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        "supabase":  bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY")),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Supabase bridge — used when the frontend completes Google sign-in via
# Supabase's hosted OAuth flow (supabase.auth.signInWithOAuth) and then sends
# us the resulting Supabase access token. We:
#
#   1. Verify the token against Supabase's /auth/v1/user endpoint
#      (uses anon key as apikey + access token as bearer)
#   2. Pull the verified email out of the response
#   3. Look the user up in our own `users` table (invite-only — no auto-create)
#   4. Mint our Temveris JWT and return it so the SPA can drop into the
#      normal session via AuthContext.login()
#
# Returns 200 with {access_token, user} on success.
# Returns 4xx with a structured `detail.code` (same codes as the redirect flow:
# "no-account", "account-disabled", "token-rejected") so the SPA can show a
# friendly message inline instead of bouncing to /sso/callback.
# ──────────────────────────────────────────────────────────────────────────────
from pydantic import BaseModel  # noqa: E402 — only imported here to keep the bridge section self-contained
from fastapi import Body  # noqa: E402


SUPABASE_URL      = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")


class _SupabaseExchangeBody(BaseModel):
    access_token: str
    remember: bool = True


@router.post("/sso/supabase-exchange")
def supabase_exchange(
    body: _SupabaseExchangeBody = Body(...),
    request: Request = None,
    response: Response = None,
    db: Session = Depends(get_db),
):
    if not (SUPABASE_URL and SUPABASE_ANON_KEY):
        raise HTTPException(503, detail={"code": "not-configured", "message": "Supabase auth bridge is not configured on the server."})
    if not body.access_token:
        raise HTTPException(400, detail={"code": "missing-token", "message": "Supabase access_token is required."})

    # Verify the token by asking Supabase to return the user.
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey":        SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {body.access_token}",
            },
            timeout=10,
        )
    except requests.RequestException:
        raise HTTPException(502, detail={"code": "token-exchange-failed", "message": "Couldn't reach Supabase to verify the sign-in."})

    if r.status_code == 401:
        raise HTTPException(401, detail={"code": "token-rejected", "message": "Supabase rejected this access token."})
    if not r.ok:
        raise HTTPException(502, detail={"code": "userinfo-failed", "message": f"Supabase returned {r.status_code}."})

    su = r.json() or {}
    email = (su.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, detail={"code": "no-email", "message": "Supabase didn't return an email for this account."})

    # Supabase is Google-backed here; use "google" as the SSO provider key so
    # the same org-scoped disambiguation logic applies as the direct Google flow.
    user = _resolve_sso_user(db, email, "google")
    if not user:
        raise HTTPException(403, detail={"code": "no-account", "email": email, "message": f"{email} is not registered in this workspace."})
    if not user.is_active:
        raise HTTPException(403, detail={"code": "account-disabled", "email": email, "message": "This account has been disabled."})

    # Backfill display name on first SSO sign-in if missing.
    meta = su.get("user_metadata") or {}
    full_name = meta.get("full_name") or meta.get("name") or ""
    if full_name and not (user.name or "").strip():
        user.name = full_name.strip()

    if _maybe_promote_super_admin(db, user):
        try:
            db.add(AuditLog(
                actor_id=user.id, target_id=user.id,
                organization_id=user.organization_id,
                entity="User", action="RolePromotedSuperAdmin",
                payload={"email": user.email, "new_role": user.role, "via": "sso:supabase"},
            ))
        except Exception:
            db.rollback()

    try:
        db.add(AuditLog(
            actor_id=user.id, target_id=user.id,
            organization_id=user.organization_id,
            entity="Auth", action="SsoLogin",
            payload={"email": user.email, "provider": "supabase-google"},
        ))
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()

    remember = bool(body.remember)
    result = establish_session(db, user, request, remember=remember)

    out = {
        "access_token": result["access_token"],
        "token_type":   "bearer",
        "expires_in":   result["expires_in"],
        "user": {
            "id":              user.id,
            "name":            user.name,
            "email":           user.email,
            "role":            user.role,
            "profile_picture": getattr(user, "profile_picture", None),
            "organization_id": user.organization_id,
        },
    }
    if rtokens.is_desktop_client(request):
        out["refresh_token"] = result["refresh_token"]
    elif response is not None:
        rtokens.set_refresh_cookie(response, result["refresh_token"], remember)
    return out
