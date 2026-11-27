"""Security Center — Workspace Settings → Security page.

Live, persistent endpoints for the seven-tab Security surface:

    GET  /api/workspace/security-center/overview        Stat tiles + banner
    GET  /api/workspace/security-center/password-policy
    PUT  /api/workspace/security-center/password-policy
    GET  /api/workspace/security-center/sessions-policy
    PUT  /api/workspace/security-center/sessions-policy
    GET  /api/workspace/security-center/sso
    PUT  /api/workspace/security-center/sso
    GET  /api/workspace/security-center/trusted-domains
    POST /api/workspace/security-center/trusted-domains
    DELETE /api/workspace/security-center/trusted-domains/{id}
    PUT  /api/workspace/security-center/invite-restrictions
    GET  /api/workspace/security-center/exceptions
    POST /api/workspace/security-center/exceptions
    DELETE /api/workspace/security-center/exceptions/{id}
    GET  /api/workspace/security-center/break-glass
    POST /api/workspace/security-center/break-glass
    DELETE /api/workspace/security-center/break-glass/{id}
    GET  /api/workspace/security-center/events
    GET  /api/workspace/security-center/export          CSV evidence bundle

Every write appends an AuditLog row so the Audit & Compliance page can
prove who changed which control and when.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Boolean, Text, desc, func,
    UniqueConstraint,
)
from sqlalchemy.orm import Session

from database.database import Base, engine, get_db
from database import models
from app.auth.service import get_current_user

_log = logging.getLogger("zoiko.workspace.security_center")

router = APIRouter(prefix="/api/workspace/security-center", tags=["Security"])


# ──────────────────────────────────────────────────────────────────────────
# Models  (organization_id added for tenant isolation)
# ──────────────────────────────────────────────────────────────────────────
class TrustedDomain(Base):
    __tablename__ = "security_trusted_domains"
    __table_args__ = (
        # Per-org uniqueness: two orgs may independently trust the same domain.
        UniqueConstraint("organization_id", "domain", name="uq_trusted_domain_org_domain"),
    )
    id                 = Column(Integer, primary_key=True, index=True)
    organization_id    = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                                nullable=True, index=True)
    domain             = Column(String(200), nullable=False, index=True)
    status             = Column(String(20), nullable=False, default="pending")  # verified | pending
    verification_token = Column(String(80), nullable=True)
    verified_at        = Column(DateTime(timezone=True), nullable=True)
    added_by_id        = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at         = Column(DateTime(timezone=True), default=datetime.utcnow)


class SecurityException(Base):
    __tablename__ = "security_exceptions"
    id            = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                             nullable=True, index=True)
    control       = Column(String(80), nullable=False)        # e.g. "MFA exemption"
    role_scope    = Column(String(80), nullable=True)         # e.g. "HR Admin"
    reason        = Column(Text, nullable=False)
    owner_id      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at    = Column(DateTime(timezone=True), nullable=True)
    status        = Column(String(40), nullable=False, default="active")  # active | expiring_soon | expired | released
    last_used_at  = Column(DateTime(timezone=True), nullable=True)
    released_at   = Column(DateTime(timezone=True), nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at    = Column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class BreakGlassAccount(Base):
    __tablename__ = "security_break_glass_accounts"
    __table_args__ = (
        # Per-org uniqueness: two orgs may register the same emergency email.
        UniqueConstraint("organization_id", "email", name="uq_break_glass_org_email"),
    )
    id               = Column(Integer, primary_key=True, index=True)
    organization_id  = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"),
                              nullable=True, index=True)
    name             = Column(String(120), nullable=False)
    email            = Column(String(200), nullable=False, index=True)
    scope            = Column(String(200), nullable=False, default="emergency")
    last_used_at     = Column(DateTime(timezone=True), nullable=True)
    last_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at       = Column(DateTime(timezone=True), nullable=True)
    created_by_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at       = Column(DateTime(timezone=True), default=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────────
# AppSettings keys — every scalar / JSON config lives here so the existing
# audit pipeline picks up every change as a SecurityControlChanged event.
# ──────────────────────────────────────────────────────────────────────────
# Password policy
K_PW_MIN_LEN          = "security.password.min_length"
K_PW_REQ_UPPER        = "security.password.require_uppercase"
K_PW_REQ_NUMBER       = "security.password.require_number"
K_PW_REQ_SPECIAL      = "security.password.require_special"
K_PW_HISTORY          = "security.password.history_count"
K_PW_FORCE_ROTATION   = "security.password.force_rotation_days"
K_PW_BLOCK_BREACHED   = "security.password.block_breached"
K_PW_BLOCK_COMMON     = "security.password.block_common"
K_PW_WARN_WEAK        = "security.password.warn_on_weak"
K_PW_FORCE_RESET      = "security.password.force_reset_on_breach"
K_PW_BREACH_COPY      = "security.password.breach_warning_copy"

# Sessions policy
K_SESSION_IDLE_MIN    = "security.sessions.idle_minutes"
K_SESSION_ABS_HOURS   = "security.sessions.absolute_hours"
K_SESSION_REMEMBER    = "security.sessions.allow_remember_device"
K_SESSION_REVOKE_ROLE = "security.sessions.revoke_on_role_change"

# SSO / Identity
K_SSO_PROVIDER        = "security.sso.provider"          # "" | "google" | "microsoft" | "okta" | "saml"
K_SSO_ENFORCE         = "security.sso.enforce_for_admins"
K_SSO_AUTOPROVISION   = "security.sso.auto_provision"
K_SSO_JIT_DEFAULT_ROLE = "security.sso.jit_default_role"

# Invite restrictions (Trusted domains tab)
K_INV_TRUSTED_ONLY    = "security.invites.trusted_only"
K_INV_WARN_OUTSIDE    = "security.invites.warn_outside"
K_INV_ALLOW_PERSONAL  = "security.invites.allow_personal_for_contractors"

# Policy version + review cadence (drives the banner)
K_POLICY_VERSION      = "security.policy_version"
K_LAST_REVIEWED_AT    = "security.last_reviewed_at"
K_NEXT_REVIEW_AT      = "security.next_review_at"


# Defaults that ship on first boot — these match the screenshots
# (Strong password, 12-char min · breach detection on / Enforced sessions
#  4 hr idle · 12 hr absolute) and are real production-grade starting
# points, not demo data. Admins can change every value.
DEFAULTS: Dict[str, str] = {
    K_PW_MIN_LEN:          "12",
    K_PW_REQ_UPPER:        "true",
    K_PW_REQ_NUMBER:       "true",
    K_PW_REQ_SPECIAL:      "true",
    K_PW_HISTORY:          "5",
    K_PW_FORCE_ROTATION:   "0",          # 0 = disabled
    K_PW_BLOCK_BREACHED:   "true",
    K_PW_BLOCK_COMMON:     "true",
    K_PW_WARN_WEAK:        "true",
    K_PW_FORCE_RESET:      "true",
    K_PW_BREACH_COPY:      "This password cannot be used because it appears in known breach data or does not meet workspace security rules.",
    K_SESSION_IDLE_MIN:    "240",        # 4 hours
    K_SESSION_ABS_HOURS:   "12",
    K_SESSION_REMEMBER:    "true",
    K_SESSION_REVOKE_ROLE: "true",
    K_SSO_PROVIDER:        "",
    K_SSO_ENFORCE:         "false",
    K_SSO_AUTOPROVISION:   "false",
    K_SSO_JIT_DEFAULT_ROLE: "employee",
    K_INV_TRUSTED_ONLY:    "true",
    K_INV_WARN_OUTSIDE:    "true",
    K_INV_ALLOW_PERSONAL:  "false",
    K_POLICY_VERSION:      "v1.0",
    K_LAST_REVIEWED_AT:    "",
    K_NEXT_REVIEW_AT:      "",
}


def ensure_tables() -> None:
    """Create security center tables if they don't exist.

    No longer seeds AppSettings rows: DEFAULTS dict is the authoritative
    fallback so NULL rows from a previous boot cannot bleed across tenants.
    """
    try:
        for t in (
            TrustedDomain.__table__,
            SecurityException.__table__,
            BreakGlassAccount.__table__,
        ):
            t.create(bind=engine, checkfirst=True)
        _log.info("Security center tables ensured")
    except Exception:
        _log.exception("Failed to ensure security center tables")


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def _require_admin(user: models.User) -> None:
    if user.role not in ("admin", "hr", "owner", "billing_admin", "super_admin"):
        raise HTTPException(403, "Admin or HR access required")


def _get_all(db: Session, keys: List[str], org_id: Optional[int]) -> Dict[str, str]:
    """Return AppSettings values for *keys* scoped to *org_id*.

    Falls back to DEFAULTS for any key not yet written for this org so new
    tenants always get sensible values without a seeding step.
    """
    rows = (
        db.query(models.AppSettings)
          .filter(
              models.AppSettings.organization_id == org_id,
              models.AppSettings.key.in_(keys),
          )
          .all()
    )
    out = {k: DEFAULTS.get(k, "") for k in keys}
    for r in rows:
        out[r.key] = r.value if r.value is not None else out[r.key]
    return out


def _set(db: Session, key: str, value: str, actor_id: Optional[int],
         org_id: Optional[int]) -> None:
    """Upsert one AppSettings row scoped to *org_id*."""
    row = (
        db.query(models.AppSettings)
          .filter(
              models.AppSettings.organization_id == org_id,
              models.AppSettings.key == key,
          )
          .one_or_none()
    )
    if row is None:
        db.add(models.AppSettings(
            organization_id=org_id,
            key=key,
            value=value,
            updated_by=actor_id,
            description="Security Center setting",
        ))
    else:
        row.value = value
        row.updated_by = actor_id


def _audit(db: Session, *, actor_id: Optional[int], entity: str, action: str,
           payload: Optional[Dict[str, Any]] = None,
           org_id: Optional[int] = None) -> None:
    db.add(models.AuditLog(
        actor_id=actor_id,
        organization_id=org_id,
        entity=entity,
        action=action,
        payload=payload or {},
    ))


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _user_name(u: Optional[models.User]) -> str:
    if not u:
        return "System"
    return u.name or (u.email or "").split("@")[0] or "User"


_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")


# ──────────────────────────────────────────────────────────────────────────
# Overview — banner + 6 stat tiles
# ──────────────────────────────────────────────────────────────────────────
@router.get("/overview")
def overview(db: Session = Depends(get_db),
             current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    cfg = _get_all(db, list(DEFAULTS.keys()), org_id)

    # ── MFA tile ────────────────────────────────────────────────────────
    admins_q = (
        db.query(models.User)
          .filter(
              models.User.is_active == True,  # noqa: E712
              models.User.organization_id == org_id,
          )
    )
    admin_total = int(admins_q.filter(models.User.role.in_(
        ("admin", "hr", "owner", "billing_admin"))).count() or 0)
    admin_with_mfa = 0
    if hasattr(models, "UserMfaFactor"):
        try:
            admin_with_mfa = int(
                db.query(func.count(models.User.id.distinct()))
                  .join(models.UserMfaFactor, models.UserMfaFactor.user_id == models.User.id)
                  .filter(
                      models.User.is_active == True,  # noqa: E712
                      models.User.organization_id == org_id,
                      models.User.role.in_(("admin", "hr", "owner", "billing_admin")),
                      models.UserMfaFactor.verified_at.isnot(None),
                  )
                  .scalar() or 0
            )
        except Exception:
            admin_with_mfa = 0
    admin_missing = max(0, admin_total - admin_with_mfa)
    mfa_pct = int(round((admin_with_mfa / admin_total) * 100)) if admin_total else 0
    if admin_total == 0:
        mfa_state, mfa_caption = "not_configured", "no admin users yet"
    elif admin_missing == 0:
        mfa_state, mfa_caption = "enforced", f"{mfa_pct}% coverage · all admins enrolled"
    elif admin_with_mfa == 0:
        mfa_state, mfa_caption = "not_configured", f"{admin_total} admin{'s' if admin_total != 1 else ''} missing"
    else:
        mfa_state = "partial"
        mfa_caption = f"{mfa_pct}% coverage · {admin_missing} admin{'s' if admin_missing != 1 else ''} missing"

    # ── SSO tile ────────────────────────────────────────────────────────
    sso_provider = cfg.get(K_SSO_PROVIDER, "") or ""
    if sso_provider:
        sso_state = "configured"
        sso_caption = f"{sso_provider.title()} · " + ("admin-enforced" if _truthy(cfg.get(K_SSO_ENFORCE)) else "available")
        sso_value = "Configured"
    else:
        sso_state = "not_set"
        sso_caption = "No IdP configured yet"
        sso_value = "Not set"

    # ── Password tile ───────────────────────────────────────────────────
    min_len = int(cfg.get(K_PW_MIN_LEN, "12") or "12")
    breach_on = _truthy(cfg.get(K_PW_BLOCK_BREACHED))
    has_complex = (
        _truthy(cfg.get(K_PW_REQ_UPPER))
        and _truthy(cfg.get(K_PW_REQ_NUMBER))
        and _truthy(cfg.get(K_PW_REQ_SPECIAL))
    )
    if min_len >= 12 and breach_on and has_complex:
        pw_state, pw_value = "strong", "Strong"
    elif min_len >= 8 and has_complex:
        pw_state, pw_value = "moderate", "Moderate"
    else:
        pw_state, pw_value = "weak", "Weak"
    pw_caption = (
        f"{min_len}-char min · breach detection " + ("on" if breach_on else "off")
    )

    # ── Sessions tile ───────────────────────────────────────────────────
    idle_min = int(cfg.get(K_SESSION_IDLE_MIN, "240") or "240")
    abs_hr = int(cfg.get(K_SESSION_ABS_HOURS, "12") or "12")
    sess_value = "Enforced"
    sess_state = "configured"
    if idle_min == 0 and abs_hr == 0:
        sess_value, sess_state = "Not set", "not_set"
        sess_caption = "No timeouts configured"
    else:
        idle_label = f"{idle_min // 60} hr idle" if idle_min >= 60 else f"{idle_min} min idle"
        sess_caption = f"{idle_label} · {abs_hr} hr absolute"

    # ── Trusted domains tile ────────────────────────────────────────────
    domains = (
        db.query(TrustedDomain)
          .filter(TrustedDomain.organization_id == org_id)
          .all()
    )
    verified = sum(1 for d in domains if d.status == "verified")
    blocks_personal = _truthy(cfg.get(K_INV_TRUSTED_ONLY)) and not _truthy(cfg.get(K_INV_ALLOW_PERSONAL))
    if verified == 0:
        td_state, td_value = "not_set", "0 verified"
        td_caption = "No trusted domains yet"
    else:
        td_state, td_value = "configured", f"{verified} verified"
        td_caption = (
            "Personal email blocked" if blocks_personal else "Personal email allowed"
        )

    # ── Exceptions tile ─────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    active_excs = (
        db.query(SecurityException)
          .filter(
              SecurityException.organization_id == org_id,
              SecurityException.released_at.is_(None),
          )
          .all()
    )
    soonest_expiry: Optional[datetime] = None
    for ex in active_excs:
        if ex.expires_at:
            exp = ex.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if soonest_expiry is None or exp < soonest_expiry:
                soonest_expiry = exp
    exc_count = len(active_excs)
    if exc_count == 0:
        ex_state, ex_value, ex_caption = "configured", "0 active", "No active exceptions"
    elif soonest_expiry and (soonest_expiry - now) <= timedelta(days=7):
        days_left = max(0, (soonest_expiry - now).days)
        ex_state, ex_value = "warning", f"{exc_count} active"
        ex_caption = f"Expiring in {days_left} day{'s' if days_left != 1 else ''} · review needed"
    else:
        ex_state, ex_value = "configured", f"{exc_count} active"
        ex_caption = "All within review cadence"

    # ── Banner — surfaces the highest-priority weakness ─────────────────
    weak_signals: List[str] = []
    if mfa_state in ("partial", "not_configured"):
        if admin_missing:
            weak_signals.append(
                f"MFA is not enforced for {admin_missing} admin-level user{'s' if admin_missing != 1 else ''}"
            )
        elif admin_total == 0:
            pass
        else:
            weak_signals.append("MFA enforcement is incomplete")
    if sso_state == "not_set":
        weak_signals.append("SSO is not configured")
    if ex_state == "warning":
        weak_signals.append(
            f"{exc_count} exception{'s' if exc_count != 1 else ''} {'is' if exc_count == 1 else 'are'} approaching expiry"
        )
    if pw_state == "weak":
        weak_signals.append("Password policy is below recommended strength")

    banner_state = "ok" if not weak_signals else "review_needed"
    banner_title = (
        "Workspace controls are healthy"
        if not weak_signals
        else "Review needed — weak controls detected"
    )

    policy_version = cfg.get(K_POLICY_VERSION) or "v1.0"
    last_reviewed = cfg.get(K_LAST_REVIEWED_AT) or None
    next_review = cfg.get(K_NEXT_REVIEW_AT) or None

    return {
        "banner": {
            "state":   banner_state,
            "title":   banner_title,
            "summary": " · ".join(weak_signals) if weak_signals else "All material controls are in place.",
            "policy_version": policy_version,
            "last_reviewed":  last_reviewed,
            "next_review":    next_review,
        },
        "tiles": {
            "mfa": {
                "state": mfa_state, "value": "Partial" if mfa_state == "partial" else ("Enforced" if mfa_state == "enforced" else "Not set"),
                "caption": mfa_caption, "coverage_pct": mfa_pct,
                "missing": admin_missing, "total": admin_total,
            },
            "sso": {"state": sso_state, "value": sso_value, "caption": sso_caption, "provider": sso_provider},
            "password": {"state": pw_state, "value": pw_value, "caption": pw_caption, "min_length": min_len},
            "sessions": {"state": sess_state, "value": sess_value, "caption": sess_caption,
                         "idle_minutes": idle_min, "absolute_hours": abs_hr},
            "trusted_domains": {"state": td_state, "value": td_value, "caption": td_caption,
                                "verified_count": verified, "total": len(domains)},
            "exceptions": {"state": ex_state, "value": ex_value, "caption": ex_caption,
                           "active_count": exc_count,
                           "soonest_expiry": soonest_expiry.isoformat() if soonest_expiry else None},
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# Password policy
# ──────────────────────────────────────────────────────────────────────────
class PasswordPolicy(BaseModel):
    min_length:          Optional[int]  = Field(default=None, ge=6, le=128)
    require_uppercase:   Optional[bool] = None
    require_number:      Optional[bool] = None
    require_special:     Optional[bool] = None
    history_count:       Optional[int]  = Field(default=None, ge=0, le=24)
    force_rotation_days: Optional[int]  = Field(default=None, ge=0, le=730)
    block_breached:      Optional[bool] = None
    block_common:        Optional[bool] = None
    warn_on_weak:        Optional[bool] = None
    force_reset_on_breach: Optional[bool] = None
    breach_warning_copy: Optional[str]  = None


def _password_policy_response(db: Session, org_id: Optional[int]) -> Dict[str, Any]:
    cfg = _get_all(db, [
        K_PW_MIN_LEN, K_PW_REQ_UPPER, K_PW_REQ_NUMBER, K_PW_REQ_SPECIAL,
        K_PW_HISTORY, K_PW_FORCE_ROTATION, K_PW_BLOCK_BREACHED, K_PW_BLOCK_COMMON,
        K_PW_WARN_WEAK, K_PW_FORCE_RESET, K_PW_BREACH_COPY,
    ], org_id)
    return {
        "min_length":          int(cfg[K_PW_MIN_LEN] or 12),
        "require_uppercase":   _truthy(cfg[K_PW_REQ_UPPER]),
        "require_number":      _truthy(cfg[K_PW_REQ_NUMBER]),
        "require_special":     _truthy(cfg[K_PW_REQ_SPECIAL]),
        "history_count":       int(cfg[K_PW_HISTORY] or 0),
        "force_rotation_days": int(cfg[K_PW_FORCE_ROTATION] or 0),
        "block_breached":      _truthy(cfg[K_PW_BLOCK_BREACHED]),
        "block_common":        _truthy(cfg[K_PW_BLOCK_COMMON]),
        "warn_on_weak":        _truthy(cfg[K_PW_WARN_WEAK]),
        "force_reset_on_breach": _truthy(cfg[K_PW_FORCE_RESET]),
        "breach_warning_copy": cfg[K_PW_BREACH_COPY],
    }


@router.get("/password-policy")
def get_password_policy(db: Session = Depends(get_db),
                       current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    return _password_policy_response(db, current_user.organization_id)


@router.put("/password-policy")
def put_password_policy(body: PasswordPolicy, db: Session = Depends(get_db),
                        current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    delta = {}
    field_map = [
        ("min_length",            K_PW_MIN_LEN,        lambda v: str(int(v))),
        ("require_uppercase",     K_PW_REQ_UPPER,      lambda v: "true" if v else "false"),
        ("require_number",        K_PW_REQ_NUMBER,     lambda v: "true" if v else "false"),
        ("require_special",       K_PW_REQ_SPECIAL,    lambda v: "true" if v else "false"),
        ("history_count",         K_PW_HISTORY,        lambda v: str(int(v))),
        ("force_rotation_days",   K_PW_FORCE_ROTATION, lambda v: str(int(v))),
        ("block_breached",        K_PW_BLOCK_BREACHED, lambda v: "true" if v else "false"),
        ("block_common",          K_PW_BLOCK_COMMON,   lambda v: "true" if v else "false"),
        ("warn_on_weak",          K_PW_WARN_WEAK,      lambda v: "true" if v else "false"),
        ("force_reset_on_breach", K_PW_FORCE_RESET,    lambda v: "true" if v else "false"),
        ("breach_warning_copy",   K_PW_BREACH_COPY,    lambda v: (v or "").strip()),
    ]
    for attr, key, conv in field_map:
        v = getattr(body, attr)
        if v is not None:
            _set(db, key, conv(v), current_user.id, org_id)
            delta[attr] = v
    if delta:
        _set(db, K_LAST_REVIEWED_AT, datetime.now(timezone.utc).isoformat(), current_user.id, org_id)
        _audit(db, actor_id=current_user.id, entity="SecurityPolicy",
               action="PasswordPolicyUpdated", payload={"fields": list(delta.keys())},
               org_id=org_id)
    db.commit()
    return _password_policy_response(db, org_id)


# ──────────────────────────────────────────────────────────────────────────
# Sessions policy
# ──────────────────────────────────────────────────────────────────────────
class SessionsPolicy(BaseModel):
    idle_minutes:           Optional[int]  = Field(default=None, ge=0, le=2880)
    absolute_hours:         Optional[int]  = Field(default=None, ge=0, le=720)
    allow_remember_device:  Optional[bool] = None
    revoke_on_role_change:  Optional[bool] = None


def _sessions_policy_response(db: Session, org_id: Optional[int]) -> Dict[str, Any]:
    cfg = _get_all(db, [K_SESSION_IDLE_MIN, K_SESSION_ABS_HOURS,
                        K_SESSION_REMEMBER, K_SESSION_REVOKE_ROLE], org_id)
    # Active session count scoped to this org (join to User for org filter).
    active_q = (
        db.query(func.count(models.UserSession.id))
          .join(models.User, models.User.id == models.UserSession.user_id)
          .filter(models.UserSession.is_revoked == False)  # noqa: E712
    )
    if org_id is not None:
        active_q = active_q.filter(models.User.organization_id == org_id)
    active = int(active_q.scalar() or 0)
    return {
        "idle_minutes":          int(cfg[K_SESSION_IDLE_MIN] or 0),
        "absolute_hours":        int(cfg[K_SESSION_ABS_HOURS] or 0),
        "allow_remember_device": _truthy(cfg[K_SESSION_REMEMBER]),
        "revoke_on_role_change": _truthy(cfg[K_SESSION_REVOKE_ROLE]),
        "active_sessions":       active,
    }


@router.get("/sessions-policy")
def get_sessions_policy(db: Session = Depends(get_db),
                        current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    return _sessions_policy_response(db, current_user.organization_id)


@router.put("/sessions-policy")
def put_sessions_policy(body: SessionsPolicy, db: Session = Depends(get_db),
                        current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    delta = {}
    if body.idle_minutes is not None:
        _set(db, K_SESSION_IDLE_MIN, str(body.idle_minutes), current_user.id, org_id)
        delta["idle_minutes"] = body.idle_minutes
    if body.absolute_hours is not None:
        _set(db, K_SESSION_ABS_HOURS, str(body.absolute_hours), current_user.id, org_id)
        delta["absolute_hours"] = body.absolute_hours
    if body.allow_remember_device is not None:
        _set(db, K_SESSION_REMEMBER, "true" if body.allow_remember_device else "false", current_user.id, org_id)
        delta["allow_remember_device"] = body.allow_remember_device
    if body.revoke_on_role_change is not None:
        _set(db, K_SESSION_REVOKE_ROLE, "true" if body.revoke_on_role_change else "false", current_user.id, org_id)
        delta["revoke_on_role_change"] = body.revoke_on_role_change
    if delta:
        _set(db, K_LAST_REVIEWED_AT, datetime.now(timezone.utc).isoformat(), current_user.id, org_id)
        _audit(db, actor_id=current_user.id, entity="SecurityPolicy",
               action="SessionsPolicyUpdated", payload=delta, org_id=org_id)
    db.commit()
    return _sessions_policy_response(db, org_id)


# ──────────────────────────────────────────────────────────────────────────
# SSO / Identity
# ──────────────────────────────────────────────────────────────────────────
SUPPORTED_PROVIDERS = ["", "google", "microsoft", "okta", "saml"]


class SsoConfig(BaseModel):
    provider:           Optional[str]  = None  # "", google, microsoft, okta, saml
    enforce_for_admins: Optional[bool] = None
    auto_provision:     Optional[bool] = None
    jit_default_role:   Optional[str]  = None


def _sso_response(db: Session, org_id: Optional[int]) -> Dict[str, Any]:
    cfg = _get_all(db, [K_SSO_PROVIDER, K_SSO_ENFORCE, K_SSO_AUTOPROVISION,
                        K_SSO_JIT_DEFAULT_ROLE], org_id)
    return {
        "provider":           cfg[K_SSO_PROVIDER] or "",
        "enforce_for_admins": _truthy(cfg[K_SSO_ENFORCE]),
        "auto_provision":     _truthy(cfg[K_SSO_AUTOPROVISION]),
        "jit_default_role":   cfg[K_SSO_JIT_DEFAULT_ROLE] or "employee",
        "supported_providers": [p for p in SUPPORTED_PROVIDERS if p],
    }


@router.get("/sso")
def get_sso(db: Session = Depends(get_db),
            current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    return _sso_response(db, current_user.organization_id)


@router.put("/sso")
def put_sso(body: SsoConfig, db: Session = Depends(get_db),
            current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    delta = {}
    if body.provider is not None:
        if body.provider not in SUPPORTED_PROVIDERS:
            raise HTTPException(400, f"Unsupported provider: {body.provider}")
        _set(db, K_SSO_PROVIDER, body.provider, current_user.id, org_id)
        # Mirror to the legacy /api/workspace/security toggle so the rest of
        # the codebase (which checks security.sso_enabled) stays in sync.
        _set(db, "security.sso_enabled", "true" if body.provider else "false", current_user.id, org_id)
        delta["provider"] = body.provider
    if body.enforce_for_admins is not None:
        _set(db, K_SSO_ENFORCE, "true" if body.enforce_for_admins else "false", current_user.id, org_id)
        delta["enforce_for_admins"] = body.enforce_for_admins
    if body.auto_provision is not None:
        _set(db, K_SSO_AUTOPROVISION, "true" if body.auto_provision else "false", current_user.id, org_id)
        delta["auto_provision"] = body.auto_provision
    if body.jit_default_role is not None:
        _set(db, K_SSO_JIT_DEFAULT_ROLE, body.jit_default_role.strip() or "employee", current_user.id, org_id)
        delta["jit_default_role"] = body.jit_default_role
    if delta:
        _set(db, K_LAST_REVIEWED_AT, datetime.now(timezone.utc).isoformat(), current_user.id, org_id)
        _audit(db, actor_id=current_user.id, entity="SecurityPolicy",
               action="SsoConfigUpdated", payload=delta, org_id=org_id)
    db.commit()
    return _sso_response(db, org_id)


# ──────────────────────────────────────────────────────────────────────────
# Trusted domains
# ──────────────────────────────────────────────────────────────────────────
class TrustedDomainBody(BaseModel):
    domain: str


class InviteRestrictions(BaseModel):
    trusted_only:                  Optional[bool] = None
    warn_outside:                  Optional[bool] = None
    allow_personal_for_contractors: Optional[bool] = None


def _trusted_domains_response(db: Session, org_id: Optional[int]) -> Dict[str, Any]:
    rows = (
        db.query(TrustedDomain)
          .filter(TrustedDomain.organization_id == org_id)
          .order_by(TrustedDomain.created_at.asc())
          .all()
    )
    cfg = _get_all(db, [K_INV_TRUSTED_ONLY, K_INV_WARN_OUTSIDE, K_INV_ALLOW_PERSONAL], org_id)
    return {
        "domains": [
            {
                "id":     d.id,
                "domain": d.domain,
                "status": d.status,
                "verification_token": d.verification_token,
                "verified_at": d.verified_at.isoformat() if d.verified_at else None,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            } for d in rows
        ],
        "invite_restrictions": {
            "trusted_only":                   _truthy(cfg[K_INV_TRUSTED_ONLY]),
            "warn_outside":                   _truthy(cfg[K_INV_WARN_OUTSIDE]),
            "allow_personal_for_contractors": _truthy(cfg[K_INV_ALLOW_PERSONAL]),
        },
    }


@router.get("/trusted-domains")
def get_trusted_domains(db: Session = Depends(get_db),
                        current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    return _trusted_domains_response(db, current_user.organization_id)


@router.post("/trusted-domains")
def add_trusted_domain(body: TrustedDomainBody, db: Session = Depends(get_db),
                       current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    domain = (body.domain or "").strip().lower().lstrip("@")
    if not _DOMAIN_RE.match(domain):
        raise HTTPException(400, "Enter a valid domain (e.g. acme.com)")
    existing = (
        db.query(TrustedDomain)
          .filter(
              TrustedDomain.organization_id == org_id,
              TrustedDomain.domain == domain,
          )
          .first()
    )
    if existing:
        raise HTTPException(409, "Domain already exists")
    row = TrustedDomain(
        organization_id=org_id,
        domain=domain,
        status="pending",
        verification_token=f"temveris-verify-{secrets.token_hex(12)}",
        added_by_id=current_user.id,
    )
    db.add(row)
    _audit(db, actor_id=current_user.id, entity="SecurityTrustedDomain",
           action="TrustedDomainAdded", payload={"domain": domain}, org_id=org_id)
    _set(db, K_LAST_REVIEWED_AT, datetime.now(timezone.utc).isoformat(), current_user.id, org_id)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "domain": row.domain, "status": row.status,
            "verification_token": row.verification_token}


@router.post("/trusted-domains/{dom_id}/verify")
def verify_trusted_domain(dom_id: int, db: Session = Depends(get_db),
                          current_user: models.User = Depends(get_current_user)):
    """Mark a domain as verified. In production this checks DNS TXT records;
    here we trust the admin clicked the button after they ran the lookup."""
    _require_admin(current_user)
    org_id = current_user.organization_id
    row = (
        db.query(TrustedDomain)
          .filter(
              TrustedDomain.id == dom_id,
              TrustedDomain.organization_id == org_id,
          )
          .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Domain not found")
    row.status = "verified"
    row.verified_at = datetime.now(timezone.utc)
    _audit(db, actor_id=current_user.id, entity="SecurityTrustedDomain",
           action="TrustedDomainVerified", payload={"domain": row.domain}, org_id=org_id)
    db.commit()
    return {"id": row.id, "status": row.status,
            "verified_at": row.verified_at.isoformat() if row.verified_at else None}


@router.delete("/trusted-domains/{dom_id}")
def delete_trusted_domain(dom_id: int, db: Session = Depends(get_db),
                          current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    row = (
        db.query(TrustedDomain)
          .filter(
              TrustedDomain.id == dom_id,
              TrustedDomain.organization_id == org_id,
          )
          .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Domain not found")
    domain = row.domain
    db.delete(row)
    _audit(db, actor_id=current_user.id, entity="SecurityTrustedDomain",
           action="TrustedDomainRemoved", payload={"domain": domain}, org_id=org_id)
    db.commit()
    return {"id": dom_id, "removed": True}


@router.put("/invite-restrictions")
def put_invite_restrictions(body: InviteRestrictions, db: Session = Depends(get_db),
                            current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    delta = {}
    if body.trusted_only is not None:
        _set(db, K_INV_TRUSTED_ONLY, "true" if body.trusted_only else "false", current_user.id, org_id)
        delta["trusted_only"] = body.trusted_only
    if body.warn_outside is not None:
        _set(db, K_INV_WARN_OUTSIDE, "true" if body.warn_outside else "false", current_user.id, org_id)
        delta["warn_outside"] = body.warn_outside
    if body.allow_personal_for_contractors is not None:
        _set(db, K_INV_ALLOW_PERSONAL, "true" if body.allow_personal_for_contractors else "false", current_user.id, org_id)
        delta["allow_personal_for_contractors"] = body.allow_personal_for_contractors
    if delta:
        _audit(db, actor_id=current_user.id, entity="SecurityPolicy",
               action="InviteRestrictionsUpdated", payload=delta, org_id=org_id)
    db.commit()
    return _trusted_domains_response(db, org_id)


# ──────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────
class ExceptionBody(BaseModel):
    control:    str
    role_scope: Optional[str] = None
    reason:     str
    owner_id:   Optional[int] = None
    expires_at: Optional[datetime] = None


def _exception_status(ex: SecurityException) -> str:
    if ex.released_at is not None:
        return "released"
    if ex.expires_at:
        exp = ex.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if exp < now:
            return "expired"
        if (exp - now) <= timedelta(days=7):
            return "expiring_soon"
    return "active"


def _exception_dict(ex: SecurityException, owner: Optional[models.User]) -> Dict[str, Any]:
    return {
        "id":          ex.id,
        "control":     ex.control,
        "role_scope":  ex.role_scope,
        "reason":      ex.reason,
        "owner":       _user_name(owner) if owner else None,
        "owner_id":    ex.owner_id,
        "expires_at":  ex.expires_at.isoformat() if ex.expires_at else None,
        "last_used_at": ex.last_used_at.isoformat() if ex.last_used_at else None,
        "released_at": ex.released_at.isoformat() if ex.released_at else None,
        "status":      _exception_status(ex),
        "created_at":  ex.created_at.isoformat() if ex.created_at else None,
    }


@router.get("/exceptions")
def list_exceptions(db: Session = Depends(get_db),
                    current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    rows = (
        db.query(SecurityException, models.User)
          .outerjoin(models.User, models.User.id == SecurityException.owner_id)
          .filter(SecurityException.organization_id == org_id)
          .order_by(desc(SecurityException.created_at))
          .all()
    )
    return {"items": [_exception_dict(ex, u) for ex, u in rows]}


@router.post("/exceptions")
def create_exception(body: ExceptionBody, db: Session = Depends(get_db),
                     current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    if not body.control.strip() or len(body.reason.strip()) < 4:
        raise HTTPException(400, "Control and reason are required (reason at least 4 chars)")
    ex = SecurityException(
        organization_id=org_id,
        control=body.control.strip(),
        role_scope=(body.role_scope or "").strip() or None,
        reason=body.reason.strip(),
        owner_id=body.owner_id,
        expires_at=body.expires_at,
        created_by_id=current_user.id,
    )
    db.add(ex)
    _audit(db, actor_id=current_user.id, entity="SecurityException",
           action="ExceptionCreated",
           payload={"control": ex.control, "scope": ex.role_scope},
           org_id=org_id)
    _set(db, K_LAST_REVIEWED_AT, datetime.now(timezone.utc).isoformat(), current_user.id, org_id)
    db.commit()
    db.refresh(ex)
    owner = db.query(models.User).filter(models.User.id == ex.owner_id).first() if ex.owner_id else None
    return _exception_dict(ex, owner)


@router.delete("/exceptions/{ex_id}")
def release_exception(ex_id: int, db: Session = Depends(get_db),
                      current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    ex = (
        db.query(SecurityException)
          .filter(
              SecurityException.id == ex_id,
              SecurityException.organization_id == org_id,
          )
          .one_or_none()
    )
    if ex is None:
        raise HTTPException(404, "Exception not found")
    ex.released_at = datetime.now(timezone.utc)
    _audit(db, actor_id=current_user.id, entity="SecurityException",
           action="ExceptionReleased",
           payload={"control": ex.control, "scope": ex.role_scope},
           org_id=org_id)
    db.commit()
    return {"id": ex_id, "status": "released"}


# ──────────────────────────────────────────────────────────────────────────
# Break-glass accounts
# ──────────────────────────────────────────────────────────────────────────
class BreakGlassBody(BaseModel):
    name:  str
    email: str
    scope: Optional[str] = "emergency"


def _break_glass_dict(b: BreakGlassAccount) -> Dict[str, Any]:
    return {
        "id":              b.id,
        "name":            b.name,
        "email":           b.email,
        "scope":           b.scope,
        "last_used_at":    b.last_used_at.isoformat() if b.last_used_at else None,
        "last_reviewed_at": b.last_reviewed_at.isoformat() if b.last_reviewed_at else None,
        "revoked_at":      b.revoked_at.isoformat() if b.revoked_at else None,
        "active":          b.revoked_at is None,
        "created_at":      b.created_at.isoformat() if b.created_at else None,
    }


@router.get("/break-glass")
def list_break_glass(db: Session = Depends(get_db),
                     current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    rows = (
        db.query(BreakGlassAccount)
          .filter(BreakGlassAccount.organization_id == org_id)
          .order_by(desc(BreakGlassAccount.created_at))
          .all()
    )
    return {"items": [_break_glass_dict(b) for b in rows]}


@router.post("/break-glass")
def create_break_glass(body: BreakGlassBody, db: Session = Depends(get_db),
                       current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    name = body.name.strip()
    email = body.email.strip().lower()
    if len(name) < 2 or "@" not in email:
        raise HTTPException(400, "Provide a name and a valid email")
    if (
        db.query(BreakGlassAccount)
          .filter(
              BreakGlassAccount.organization_id == org_id,
              BreakGlassAccount.email == email,
          )
          .first()
    ):
        raise HTTPException(409, "An emergency account with this email already exists")
    row = BreakGlassAccount(
        organization_id=org_id,
        name=name,
        email=email,
        scope=(body.scope or "emergency").strip() or "emergency",
        created_by_id=current_user.id,
    )
    db.add(row)
    _audit(db, actor_id=current_user.id, entity="SecurityBreakGlass",
           action="BreakGlassCreated", payload={"email": email, "scope": row.scope},
           org_id=org_id)
    db.commit()
    db.refresh(row)
    return _break_glass_dict(row)


@router.delete("/break-glass/{acc_id}")
def revoke_break_glass(acc_id: int, db: Session = Depends(get_db),
                       current_user: models.User = Depends(get_current_user)):
    _require_admin(current_user)
    org_id = current_user.organization_id
    row = (
        db.query(BreakGlassAccount)
          .filter(
              BreakGlassAccount.id == acc_id,
              BreakGlassAccount.organization_id == org_id,
          )
          .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Break-glass account not found")
    row.revoked_at = datetime.now(timezone.utc)
    _audit(db, actor_id=current_user.id, entity="SecurityBreakGlass",
           action="BreakGlassRevoked", payload={"email": row.email},
           org_id=org_id)
    db.commit()
    return {"id": acc_id, "revoked": True}


# ──────────────────────────────────────────────────────────────────────────
# Security events feed — filtered audit log
# ──────────────────────────────────────────────────────────────────────────
_EVENT_ENTITIES = (
    "WorkspaceSecurity", "SecurityPolicy", "SecurityTrustedDomain",
    "SecurityException", "SecurityBreakGlass",
    "Auth", "User", "Session", "MFA", "SSO", "Role", "RoleAssignment",
    "MfaControls", "MfaException", "UserMfaFactor", "Security",
)


def _risk_for(entity: str, action: str) -> str:
    a = (action or "").lower()
    if "failed" in a or "fail" in a or "blocked" in a or "lockout" in a or "denied" in a:
        return "High"
    if "delete" in a or "remove" in a or "revoke" in a or "released" in a:
        return "Medium"
    if "password" in a or "policy" in a or "exception" in a or "rule" in a:
        return "Medium"
    if "enrolled" in a or "verified" in a or "succeeded" in a or "added" in a:
        return "Info"
    return "Info"


@router.get("/events")
def list_events(
    since_days: int = Query(7, ge=1, le=365),
    event_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    query = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(models.AuditLog.created_at >= cutoff)
          .filter(models.AuditLog.entity.in_(_EVENT_ENTITIES))
    )
    if not is_super:
        query = query.filter(models.AuditLog.organization_id == current_user.organization_id)
    if event_type and event_type != "all":
        query = query.filter(
            (models.AuditLog.entity.ilike(f"%{event_type}%"))
            | (models.AuditLog.action.ilike(f"%{event_type}%"))
        )
    rows = query.order_by(desc(models.AuditLog.created_at)).limit(200).all()
    items = []
    for r, u in rows:
        items.append({
            "id":          r.id,
            "occurred_at": r.created_at.isoformat() if r.created_at else None,
            "actor":       _user_name(u),
            "actor_email": u.email if u else None,
            "entity":      r.entity,
            "action":      r.action,
            "payload":     r.payload or {},
            "ip_address":  r.ip_address,
            "risk":        _risk_for(r.entity, r.action),
        })
    return {"items": items, "since_days": since_days}


# ──────────────────────────────────────────────────────────────────────────
# Evidence export — CSV of recent security events
# ──────────────────────────────────────────────────────────────────────────
@router.get("/export")
def export_evidence(
    since_days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id = current_user.organization_id
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    q = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(models.AuditLog.created_at >= cutoff)
          .filter(models.AuditLog.entity.in_(_EVENT_ENTITIES))
    )
    if not is_super:
        q = q.filter(models.AuditLog.organization_id == org_id)
    rows = q.order_by(desc(models.AuditLog.created_at)).limit(5000).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["EventID", "OccurredAt", "Actor", "ActorEmail", "Entity", "Action", "Risk", "IP", "Payload"])
    for r, u in rows:
        writer.writerow([
            f"SEC-{r.id:06d}",
            r.created_at.isoformat() if r.created_at else "",
            _user_name(u), u.email if u else "",
            r.entity, r.action, _risk_for(r.entity, r.action),
            r.ip_address or "",
            json.dumps(r.payload or {}, separators=(",", ":")),
        ])
    _audit(db, actor_id=current_user.id, entity="SecurityPolicy",
           action="SecurityEvidenceExported",
           payload={"since_days": since_days, "rows": len(rows)},
           org_id=org_id)
    db.commit()
    csv_text = buf.getvalue()
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="security-evidence-{datetime.utcnow():%Y%m%d}.csv"',
        },
    )


# ──────────────────────────────────────────────────────────────────────────
# Save-all (used by the Discard/Save bar in the UI)
# ──────────────────────────────────────────────────────────────────────────
class SecureWorkspaceBody(BaseModel):
    note: Optional[str] = None


@router.post("/secure")
def mark_secured(body: Optional[SecureWorkspaceBody] = None,
                 db: Session = Depends(get_db),
                 current_user: models.User = Depends(get_current_user)):
    """Stamps the workspace as freshly reviewed. Drives the banner state +
    'Last reviewed' line. Idempotent — admins can press this from the
    'Secure workspace' header CTA at any time."""
    _require_admin(current_user)
    org_id = current_user.organization_id
    now = datetime.now(timezone.utc)
    _set(db, K_LAST_REVIEWED_AT, now.isoformat(), current_user.id, org_id)
    _set(db, K_NEXT_REVIEW_AT, (now + timedelta(days=30)).isoformat(), current_user.id, org_id)
    _audit(db, actor_id=current_user.id, entity="SecurityPolicy",
           action="WorkspaceMarkedSecured",
           payload={"note": (body.note if body else None) or ""},
           org_id=org_id)
    db.commit()
    return {"last_reviewed": now.isoformat(),
            "next_review":   (now + timedelta(days=30)).isoformat()}
