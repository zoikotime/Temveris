"""
Profile module router.

Powers the employee Profile page: personal info, work preferences,
account security (password + active sessions), notification preferences,
productivity summary, and access history.

Every endpoint here resolves the caller via the existing JWT bearer flow,
so the user can only view / edit their own profile — no admin override.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app import _tz
from app.schedule_policy import resolve_work_schedule
from app.schedule_policy.keys import WEEKDAY_NAMES
from app.auth.service import (
    decode_access_token,
    get_current_user,
    hash_password,
    verify_password,
    _user_cache_invalidate,
)


router = APIRouter(prefix="/profile", tags=["Profile"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
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


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_BROWSER_PATTERNS = [
    ("Edg",     "Microsoft Edge"),
    ("OPR",     "Opera"),
    ("Chrome",  "Chrome"),
    ("Firefox", "Firefox"),
    ("Safari",  "Safari"),
    ("Electron", "Electron"),
]
_OS_PATTERNS = [
    ("Windows", "Windows"),
    ("Mac OS",  "macOS"),
    ("Macintosh", "macOS"),
    ("iPhone",  "iOS"),
    ("iPad",    "iPadOS"),
    ("Android", "Android"),
    ("Linux",   "Linux"),
]


def _parse_user_agent(ua: str) -> dict:
    """Best-effort UA parsing — no external deps. Good enough for the
    Profile sessions list."""
    ua = ua or ""
    browser = next((label for needle, label in _BROWSER_PATTERNS if needle in ua), "Unknown")
    os_name = next((label for needle, label in _OS_PATTERNS if needle in ua), "Unknown")

    # Device type heuristic
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


def _ensure_preferences(db: Session, user_id: int) -> models.UserPreferences:
    """Fetch or create the prefs row for a given user."""
    prefs = (
        db.query(models.UserPreferences)
        .filter(models.UserPreferences.user_id == user_id)
        .first()
    )
    if prefs:
        return prefs

    prefs = models.UserPreferences(user_id=user_id)
    db.add(prefs)
    db.commit()
    db.refresh(prefs)
    return prefs


def _schedule_view(db: Session, user: models.User) -> dict:
    """The work schedule in force for `user`, straight from the resolver.

    These four values used to be employee-editable rows on UserPreferences,
    which made an employee's own profile a fifth, self-service source of
    "expected hours" that silently disagreed with the org/team policy the
    reports and payroll are computed from. They are now REPORTED, not stored:
    whatever the cascade resolves is what the profile shows.
    """
    sched = resolve_work_schedule(db, user, _tz.local_date(db, user.organization_id))
    return {
        "timezone":           sched.timezone_name,
        "work_start_time":    sched.start_local.strftime("%H:%M") if sched.start_local else "",
        "work_end_time":      sched.end_local.strftime("%H:%M") if sched.end_local else "",
        # date.weekday() indices, the shape this endpoint has always returned.
        "work_days":          [WEEKDAY_NAMES.index(d) for d in sched.working_days
                               if d in WEEKDAY_NAMES],
        "daily_target_hours": round(sched.expected_work_seconds / 3600.0, 2),
        # Read-only for the employee; the admin changes it in Settings.
        "schedule_editable":  False,
    }


def _serialise_prefs(prefs: models.UserPreferences, schedule: dict) -> dict:
    return {
        **schedule,
        "is_available":       bool(prefs.is_available),
        "away_message":       prefs.away_message or "",
        "notifications": {
            "schedule_email":     bool(prefs.notify_schedule_email),
            "schedule_push":      bool(prefs.notify_schedule_push),
            "activity_summary":   bool(prefs.notify_activity_summary),
            "system_updates":     bool(prefs.notify_system_updates),
            "leave_status":       bool(prefs.notify_leave_status),
            "security_alerts":    bool(prefs.notify_security_alerts),
            "weekly_digest":      bool(prefs.weekly_digest),
        },
    }


def _current_sid(token: str) -> Optional[int]:
    """The stable session id baked into the access token at login (`sid`
    claim). Stays constant across access-token rotation, so it — not a hash of
    the rotating token — is how we identify 'this device'."""
    if not token:
        return None
    payload = decode_access_token(token) or {}
    sid = payload.get("sid")
    try:
        return int(sid) if sid is not None else None
    except (TypeError, ValueError):
        return None


def _touch_session_from_token(db: Session, token: str, request: Optional[Request]) -> Optional[models.UserSession]:
    """Bump last_active_at on the UserSession this token belongs to.

    Modern tokens carry a `sid` claim → we resolve the device row by id, so
    refresh-driven token rotation reuses the SAME row instead of spawning a new
    one each rotation. Legacy tokens (no sid, issued before refresh tokens
    existed) fall back to the old hash-of-token lookup, creating the row on the
    fly if missing."""
    if not token:
        return None

    sid = _current_sid(token)
    if sid is not None:
        sess = (
            db.query(models.UserSession)
            .filter(models.UserSession.id == sid)
            .first()
        )
        if sess is not None:
            sess.last_active_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(sess)
            return sess
        # sid present but row gone (e.g. DB reset) — fall through to recreate.

    th = _hash_token(token)
    sess = (
        db.query(models.UserSession)
        .filter(models.UserSession.token_hash == th)
        .first()
    )
    if sess is None:
        payload = decode_access_token(token) or {}
        user_id = payload.get("user_id")
        if not user_id:
            return None
        ua = (request.headers.get("user-agent") if request else "") or ""
        meta = _parse_user_agent(ua)
        sess = models.UserSession(
            user_id     = user_id,
            token_hash  = th,
            user_agent  = ua[:512],
            device_type = meta["device_type"],
            device_name = meta["device_name"],
            browser     = meta["browser"],
            os          = meta["os"],
            ip_address  = _client_ip(request),
        )
        db.add(sess)

    sess.last_active_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(sess)
    return sess


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC PROFILE  GET /profile/users/{user_id}
# Read-only directory view of any worker — used by the "View Profile" flow
# from team cards / employee grid. Returns the fields teammates can see
# (identity, role, team, designation, picture, join date) but NEVER the
# private surface: no preferences, no sessions, no access history,
# no password or 2FA state. Owner always gets the full /me payload.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/users/{user_id}")
def get_public_profile(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # If a worker requests their own id, hand them straight to /me so they
    # see their full editable profile instead of the directory subset.
    if user_id == current_user.id:
        # Re-use the existing /me handler by composing its payload inline.
        prefs = _ensure_preferences(db, current_user.id)
        org_name = "Temveris"
        try:
            org = (
                db.query(models.OrgSettings)
                .filter(models.OrgSettings.organization_id == current_user.organization_id)
                .first()
            )
            if org and org.company_name:
                org_name = org.company_name
        except Exception:
            pass
        return {
            "id":              current_user.id,
            "name":            current_user.name or "",
            "email":           current_user.email,
            "phone":           current_user.phone or "",
            "role":            current_user.role,
            "designation":     current_user.designation or "",
            "department":      current_user.department or "",
            "team":            current_user.team or "",
            "employment_type": current_user.employment_type or "full_time",
            "profile_picture": current_user.profile_picture,
            "is_active":       bool(current_user.is_active),
            "joined_at":       current_user.created_at.isoformat() if current_user.created_at else None,
            "organization":    {"name": org_name},
            "preferences":     _serialise_prefs(prefs, _schedule_view(db, current_user)),
            "is_self":         True,
        }

    target = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == current_user.organization_id,
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    org_name = "Temveris"
    try:
        org = (
            db.query(models.OrgSettings)
            .filter(models.OrgSettings.organization_id == current_user.organization_id)
            .first()
        )
        if org and org.company_name:
            org_name = org.company_name
    except Exception:
        pass

    # Public-safe projection. Anything missing here is deliberate — adding
    # a field exposes it to every teammate, so go through review first.
    return {
        "id":              target.id,
        "name":            target.name or "",
        "email":           target.email,
        "phone":           target.phone or "",
        "role":            target.role,
        "designation":     target.designation or "",
        "department":      target.department or "",
        "team":            target.team or "",
        "employment_type": target.employment_type or "full_time",
        "profile_picture": target.profile_picture,
        "is_active":       bool(target.is_active),
        "joined_at":       target.created_at.isoformat() if target.created_at else None,
        "organization":    {"name": org_name},
        "is_self":         False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# OVERVIEW  GET /profile/me
# Single payload covering identity + work prefs + org context, so the
# frontend Profile page only needs one request to render its base shell.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/me")
def get_my_profile(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _touch_session_from_token(db, token, request)
    prefs = _ensure_preferences(db, current_user.id)

    org_name = "Temveris"
    try:
        org = (
            db.query(models.OrgSettings)
            .filter(models.OrgSettings.organization_id == current_user.organization_id)
            .first()
        )
        if org and org.company_name:
            org_name = org.company_name
    except Exception:
        # org_settings may not exist on first boot — fall through with default.
        pass

    return {
        "id":              current_user.id,
        "name":            current_user.name or "",
        "email":           current_user.email,
        "phone":           current_user.phone or "",
        "role":            current_user.role,
        "designation":     current_user.designation or "",
        "department":      current_user.department or "",
        "team":            current_user.team or "",
        "employment_type": current_user.employment_type or "full_time",
        "profile_picture": current_user.profile_picture,
        "is_active":       bool(current_user.is_active),
        "joined_at":       current_user.created_at.isoformat() if current_user.created_at else None,
        "organization":    {"name": org_name},
        "preferences":     _serialise_prefs(prefs, _schedule_view(db, current_user)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE PERSONAL INFO  PATCH /profile/me
# ─────────────────────────────────────────────────────────────────────────────
class UpdateProfileRequest(BaseModel):
    name:            Optional[str] = Field(default=None, max_length=255)
    phone:           Optional[str] = Field(default=None, max_length=30)
    designation:     Optional[str] = Field(default=None, max_length=100)
    department:      Optional[str] = Field(default=None, max_length=100)
    profile_picture: Optional[str] = None  # data URL or "" to clear


@router.patch("/me")
def update_my_profile(
    data: UpdateProfileRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # current_user is a detached copy from the identity cache (see
    # auth.get_current_user → make_transient), so mutating it never persists and
    # db.refresh() on it raises. Load the live row, mutate that, drop the cache.
    user = db.query(models.User).filter(models.User.id == current_user.id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if data.name is not None:
        trimmed = data.name.strip()
        if not trimmed:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        user.name = trimmed

    if data.phone is not None:
        phone = data.phone.strip()
        if phone and not re.fullmatch(r"[+\d][\d\s\-()]{4,29}", phone):
            raise HTTPException(status_code=400, detail="Phone number looks invalid")
        user.phone = phone or None

    if data.designation is not None:
        user.designation = data.designation.strip() or None

    if data.department is not None:
        user.department = data.department.strip() or None

    if data.profile_picture is not None:
        if data.profile_picture and len(data.profile_picture) > 2_000_000:
            raise HTTPException(status_code=400, detail="Profile picture too large (max ~1.5 MB)")
        user.profile_picture = data.profile_picture or None

    db.commit()
    db.refresh(user)
    _user_cache_invalidate(user.id)
    return {
        "id":              user.id,
        "name":            user.name or "",
        "email":           user.email,
        "phone":           user.phone or "",
        "designation":     user.designation or "",
        "department":      user.department or "",
        "profile_picture": user.profile_picture,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WORK PREFERENCES  GET / PUT /profile/preferences
# ─────────────────────────────────────────────────────────────────────────────
class NotificationPrefs(BaseModel):
    schedule_email:   Optional[bool] = None
    schedule_push:    Optional[bool] = None
    activity_summary: Optional[bool] = None
    system_updates:   Optional[bool] = None
    leave_status:     Optional[bool] = None
    security_alerts:  Optional[bool] = None
    weekly_digest:    Optional[bool] = None


class UpdatePrefsRequest(BaseModel):
    # DEPRECATED, ignored. The work schedule is org/team/employee policy and is
    # resolved by app.schedule_policy — an employee cannot set their own
    # expected hours, shift or working days from their profile page. Still
    # accepted (and discarded) so existing clients that POST the whole prefs
    # object back do not start failing with a 422.
    timezone:           Optional[str]   = Field(default=None, max_length=64)
    work_start_time:    Optional[str]   = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    work_end_time:      Optional[str]   = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    work_days:          Optional[List[int]] = None
    daily_target_hours: Optional[float] = Field(default=None, ge=0.0, le=24.0)

    is_available:       Optional[bool]  = None
    away_message:       Optional[str]   = Field(default=None, max_length=255)
    notifications:      Optional[NotificationPrefs] = None


@router.get("/preferences")
def get_preferences(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    prefs = _ensure_preferences(db, current_user.id)
    return _serialise_prefs(prefs, _schedule_view(db, current_user))


@router.put("/preferences")
def update_preferences(
    data: UpdatePrefsRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    prefs = _ensure_preferences(db, current_user.id)

    # timezone / work_start_time / work_end_time / work_days /
    # daily_target_hours are deliberately NOT written. They are workforce
    # policy, owned by the org/team/employee settings cascade; the existing
    # column values are left untouched rather than deleted.

    if data.is_available is not None:
        prefs.is_available = bool(data.is_available)

    if data.away_message is not None:
        prefs.away_message = data.away_message.strip() or None

    if data.notifications is not None:
        n = data.notifications
        if n.schedule_email   is not None: prefs.notify_schedule_email   = n.schedule_email
        if n.schedule_push    is not None: prefs.notify_schedule_push    = n.schedule_push
        if n.activity_summary is not None: prefs.notify_activity_summary = n.activity_summary
        if n.system_updates   is not None: prefs.notify_system_updates   = n.system_updates
        if n.leave_status     is not None: prefs.notify_leave_status     = n.leave_status
        if n.security_alerts  is not None: prefs.notify_security_alerts  = n.security_alerts
        if n.weekly_digest    is not None: prefs.weekly_digest           = n.weekly_digest

    db.commit()
    db.refresh(prefs)
    return _serialise_prefs(prefs, _schedule_view(db, current_user))


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATIONS  shortcut endpoint
# Mirrors the prefs.notifications block so the UI can save just that
# section without sending the entire prefs object.
# ─────────────────────────────────────────────────────────────────────────────
@router.put("/notifications")
def update_notifications(
    data: NotificationPrefs,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    prefs = _ensure_preferences(db, current_user.id)
    if data.schedule_email   is not None: prefs.notify_schedule_email   = data.schedule_email
    if data.schedule_push    is not None: prefs.notify_schedule_push    = data.schedule_push
    if data.activity_summary is not None: prefs.notify_activity_summary = data.activity_summary
    if data.system_updates   is not None: prefs.notify_system_updates   = data.system_updates
    if data.leave_status     is not None: prefs.notify_leave_status     = data.leave_status
    if data.security_alerts  is not None: prefs.notify_security_alerts  = data.security_alerts
    if data.weekly_digest    is not None: prefs.weekly_digest           = data.weekly_digest

    db.commit()
    db.refresh(prefs)
    return _serialise_prefs(prefs, _schedule_view(db, current_user))["notifications"]


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE PASSWORD  POST /profile/password
# ─────────────────────────────────────────────────────────────────────────────
class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str = Field(min_length=8, max_length=128)


@router.post("/password")
def change_password(
    data: ChangePasswordRequest,
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not current_user.password:
        raise HTTPException(
            status_code=400,
            detail="Account has no password set. Use the password reset flow first.",
        )

    if not verify_password(data.current_password, current_user.password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if data.current_password == data.new_password:
        raise HTTPException(
            status_code=400,
            detail="New password must be different from your current password",
        )

    # Light strength rule — 8+ chars covered by Field; ensure at least
    # one letter + one digit so we don't accept obviously weak entries.
    if not (re.search(r"[A-Za-z]", data.new_password) and re.search(r"\d", data.new_password)):
        raise HTTPException(
            status_code=400,
            detail="Password must contain at least one letter and one number",
        )

    # Mutate the live row, not the detached cached copy (see update_my_profile).
    user = db.query(models.User).filter(models.User.id == current_user.id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.password = hash_password(data.new_password)
    user.is_password_set = True
    _user_cache_invalidate(user.id)
    db.add(models.AuditLog(
        actor_id        = current_user.id,
        target_id       = current_user.id,
        organization_id = current_user.organization_id,
        entity          = "User",
        action          = "PasswordChanged",
        payload         = {"email": current_user.email},
        ip_address      = _client_ip(request),
    ))
    db.commit()

    # ZT-IAM-05 password-changed notification (Essential - security). Best-effort;
    # never blocks the completed change.
    try:
        from app.email_service import send_password_changed_email
        send_password_changed_email(user.email, user.name or "")
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "ZT-IAM-05 password-changed email failed (change preserved)", exc_info=True)

    # Revoke every other session so the password change actually
    # boots out leaked devices. The current device is preserved.
    current_sid = _current_sid(token)
    others = (
        db.query(models.UserSession)
        .filter(
            models.UserSession.user_id == current_user.id,
            (models.UserSession.id != current_sid) if current_sid is not None
                else (models.UserSession.token_hash != _hash_token(token)),
            models.UserSession.is_revoked.is_(False),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for s in others:
        s.is_revoked = True
        s.revoked_at = now
    db.commit()

    return {
        "message":          "Password updated successfully",
        "revoked_sessions": len(others),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVE SESSIONS
# GET   /profile/sessions               – list active devices
# DELETE /profile/sessions/{id}         – revoke a single device
# POST  /profile/sessions/revoke-others – revoke every device except the
#                                          one making the call
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/sessions")
def list_sessions(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    current = _touch_session_from_token(db, token, request)
    current_id = current.id if current else None

    rows = (
        db.query(models.UserSession)
        .filter(
            models.UserSession.user_id == current_user.id,
            models.UserSession.is_revoked.is_(False),
        )
        .order_by(models.UserSession.last_active_at.desc())
        .all()
    )

    return [
        {
            "id":             s.id,
            "device_name":    s.device_name or "Unknown device",
            "device_type":    s.device_type or "desktop",
            "browser":        s.browser or "Unknown",
            "os":             s.os or "Unknown",
            "ip_address":     s.ip_address,
            "location":       s.location,
            "is_current":     (s.id == current_id),
            "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
            "created_at":     s.created_at.isoformat() if s.created_at else None,
        }
        for s in rows
    ]


@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: int,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    sess = (
        db.query(models.UserSession)
        .filter(
            models.UserSession.id == session_id,
            models.UserSession.user_id == current_user.id,
        )
        .first()
    )
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    current_sid = _current_sid(token)
    is_current = (sess.id == current_sid) if current_sid is not None else (sess.token_hash == _hash_token(token))
    if is_current:
        raise HTTPException(
            status_code=400,
            detail="Use sign-out to revoke the current device",
        )

    now = datetime.now(timezone.utc)
    sess.is_revoked = True
    sess.revoked_at = now
    db.commit()
    # Kill the persistent refresh token bound to this device too.
    try:
        from database.models import RefreshToken as _RT
        db.query(_RT).filter(
            _RT.session_id == sess.id,
            _RT.is_revoked.is_(False),
        ).update({"is_revoked": True, "revoked_at": now, "revoked_reason": "device_revoked"},
                 synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    return {"message": "Session revoked", "id": session_id}


@router.post("/sessions/revoke-others")
def revoke_other_sessions(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    current_sid = _current_sid(token)
    others = (
        db.query(models.UserSession)
        .filter(
            models.UserSession.user_id == current_user.id,
            (models.UserSession.id != current_sid) if current_sid is not None
                else (models.UserSession.token_hash != _hash_token(token)),
            models.UserSession.is_revoked.is_(False),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for s in others:
        s.is_revoked = True
        s.revoked_at = now
    db.commit()
    # Also revoke the refresh tokens for those device rows so a 'remember me'
    # token can't silently resurrect a session we just signed out.
    try:
        from app.auth import tokens as _rtok
        sess_ids = [s.id for s in others]
        if sess_ids:
            from database.models import RefreshToken as _RT
            db.query(_RT).filter(
                _RT.session_id.in_(sess_ids),
                _RT.is_revoked.is_(False),
            ).update({"is_revoked": True, "revoked_at": now, "revoked_reason": "revoke_others"},
                     synchronize_session=False)
            db.commit()
    except Exception:
        db.rollback()
    return {"message": "Other sessions revoked", "count": len(others)}


# ─────────────────────────────────────────────────────────────────────────────
# ACCESS HISTORY  GET /profile/access-history
# Pulled from AuditLog rows whose action is UserLogin / PasswordChanged /
# SessionRevoked, ordered most-recent first.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/access-history")
def access_history(
    limit: int = 25,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    limit = max(1, min(int(limit or 25), 100))
    rows = (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.actor_id == current_user.id,
            models.AuditLog.entity == "User",
            models.AuditLog.action.in_(["UserLogin", "PasswordChanged", "SessionRevoked"]),
        )
        .order_by(models.AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id":         r.id,
            "action":     r.action,
            "ip_address": r.ip_address,
            "metadata":   r.payload or {},
            "at":         r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTIVITY SUMMARY  GET /profile/productivity-summary
# Today's working hours, active vs idle time, recent activity insights.
# Designed for the Profile page hero strip — avoids duplicating the full
# Today dashboard endpoint.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/productivity-summary")
def productivity_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_ago = now - timedelta(days=7)

    # ── Today's clock session(s) ────────────────────────────────────────────
    sessions_today = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.user_id == current_user.id,
            models.ClockSession.clock_in_time >= start_of_day,
        )
        .all()
    )
    total_seconds = 0
    break_seconds = 0
    is_clocked_in = False
    last_clock_in = None
    for s in sessions_today:
        # If the session is still open, count up to "now"
        if s.is_active and s.clock_out_time is None:
            is_clocked_in = True
            last_clock_in = s.clock_in_time
            elapsed = int((now - s.clock_in_time).total_seconds()) if s.clock_in_time else 0
            total_seconds += max(0, elapsed)
        else:
            total_seconds += int(s.total_seconds or 0)
        break_seconds += int(s.break_seconds or 0)

    working_seconds = max(0, total_seconds - break_seconds)

    # ── Activity-derived active vs idle (today) ─────────────────────────────
    active_seconds = (
        db.query(func.coalesce(func.sum(models.Activity.idle_seconds), 0))
        .filter(
            models.Activity.user_id == current_user.id,
            models.Activity.timestamp >= start_of_day,
            models.Activity.is_active.is_(True),
        )
        .scalar()
    ) or 0
    idle_seconds_total = (
        db.query(func.coalesce(func.sum(models.Activity.idle_seconds), 0))
        .filter(
            models.Activity.user_id == current_user.id,
            models.Activity.timestamp >= start_of_day,
            models.Activity.is_active.is_(False),
        )
        .scalar()
    ) or 0

    # ── Top 5 apps used today ───────────────────────────────────────────────
    top_apps_q = (
        db.query(
            models.Activity.app_name,
            func.count(models.Activity.id).label("samples"),
        )
        .filter(
            models.Activity.user_id == current_user.id,
            models.Activity.timestamp >= start_of_day,
            models.Activity.app_name.isnot(None),
        )
        .group_by(models.Activity.app_name)
        .order_by(func.count(models.Activity.id).desc())
        .limit(5)
        .all()
    )
    top_apps = [
        {"app_name": app or "Unknown", "samples": int(samples)}
        for app, samples in top_apps_q
    ]

    # ── Past-7-day total (for the Profile page badge) ───────────────────────
    week_total = (
        db.query(func.coalesce(func.sum(models.ClockSession.total_seconds), 0))
        .filter(
            models.ClockSession.user_id == current_user.id,
            models.ClockSession.clock_in_time >= week_ago,
        )
        .scalar()
    ) or 0

    # ── Recent leave / break activity (last 5 events) ───────────────────────
    recent_breaks = (
        db.query(models.BreakSession)
        .filter(models.BreakSession.user_id == current_user.id)
        .order_by(models.BreakSession.break_start.desc())
        .limit(3)
        .all()
    )
    recent_activity = [
        {
            "type":    "break",
            "started": b.break_start.isoformat() if b.break_start else None,
            "ended":   b.break_end.isoformat() if b.break_end else None,
            "seconds": int(b.break_seconds or 0),
            "reason":  b.reason or "",
        }
        for b in recent_breaks
    ]

    # One canonical answer to "how long is this person's day" — the same one
    # the dashboard, reports, risk alerts and payroll use.
    target_seconds = resolve_work_schedule(
        db, current_user, _tz.local_date(db, current_user.organization_id),
    ).expected_work_seconds
    progress = (working_seconds / target_seconds) if target_seconds > 0 else 0.0
    progress = max(0.0, min(1.0, progress))

    return {
        "today": {
            "working_seconds": int(working_seconds),
            "break_seconds":   int(break_seconds),
            "active_seconds":  int(active_seconds),
            "idle_seconds":    int(idle_seconds_total),
            "is_clocked_in":   is_clocked_in,
            "last_clock_in":   last_clock_in.isoformat() if last_clock_in else None,
            "target_seconds":  target_seconds,
            "progress":        round(progress, 4),
        },
        "week": {
            "total_seconds":   int(week_total),
        },
        "top_apps":        top_apps,
        "recent_activity": recent_activity,
    }
