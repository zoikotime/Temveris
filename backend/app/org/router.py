"""
Organization settings + Team management router.
Handles: workspace profile (identity, operating context, owner,
linked-config status), logo, teams CRUD.
"""

import json
import os, re, shutil
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from database.database import get_db
from database import models
from app import _tz
from app.auth.service import get_current_user
from app.commercial.enforcement import check_team_limit

router = APIRouter(prefix="/org", tags=["organization"])

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR  = os.path.join(BASE_DIR, "..", "..", "..", "tracker-data", "org")
os.makedirs(UPLOAD_DIR, exist_ok=True)

STATIC_PATH = "/static/org"  # served by FastAPI static mount


# Data residency is set at the infrastructure level (where Supabase
# physically hosts the project). It is intentionally NOT a user-editable
# field — the row in OrgSettings has no column for it. Surfaced as a
# fixed string so the UI can display the truth instead of a placeholder.
DATA_RESIDENCY_REGION = os.getenv("DATA_RESIDENCY_REGION", "us-east-1")
DATA_RESIDENCY_LABEL  = os.getenv("DATA_RESIDENCY_LABEL",  "United States — us-east-1")
DATA_RESIDENCY_DETAIL = os.getenv("DATA_RESIDENCY_DETAIL", "AWS US East (N. Virginia)")


_SLUG_RE     = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_COUNTRY_RE  = re.compile(r"^[A-Z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_EMAIL_RE    = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HEX_RE      = re.compile(r"^#([0-9A-Fa-f]{6})$")
_URL_RE      = re.compile(r"^https?://[^\s]{2,}$")


def _admin_user(
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """Dependency for OrgSettings endpoints — returns the full User model so
    organization_id is available without a second DB round-trip."""
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin/HR only")
    return current_user


# ─── ORG SETTINGS ────────────────────────────────────────────────────────────


def _slugify(s: str) -> str:
    """Best-effort slug — lowercase, hyphens, alphanumerics only."""
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "workspace"


def _serialise_owner(owner: Optional[models.User], owner_since) -> Optional[dict]:
    if not owner:
        return None
    return {
        "id":         owner.id,
        "name":       owner.name,
        "email":      owner.email,
        "role":       owner.role,
        "since":      owner_since.isoformat() if owner_since else None,
    }


def _resolve_owner(
    db: Session,
    settings: Optional[models.OrgSettings],
    organization_id: Optional[int] = None,
) -> tuple[Optional[models.User], Any]:
    """Return (owner_user, owner_since_dt).

    Falls back to the earliest-created admin/hr user in this org when
    settings has no owner_user_id set.
    """
    if settings and settings.owner_user_id:
        # Verify the stored owner belongs to this org before returning them.
        # Without this check a stale or tampered owner_user_id could surface
        # a user from a different organization as this org's owner.
        owner_q = db.query(models.User).filter(
            models.User.id == settings.owner_user_id,
        )
        if organization_id is not None:
            owner_q = owner_q.filter(models.User.organization_id == organization_id)
        owner = owner_q.first()
        return owner, settings.owner_since

    # Fallback: return the earliest-created admin/hr in this org.
    # When organization_id is None (no org context) return nothing rather than
    # running an unscoped global query that would return an arbitrary user.
    if organization_id is None:
        return None, None

    fallback = (
        db.query(models.User)
          .filter(
              models.User.role.in_(("admin", "hr")),
              models.User.organization_id == organization_id,
          )
          .order_by(models.User.created_at.asc())
          .first()
    )
    return fallback, (fallback.created_at if fallback else None)


def _row_or_create(db: Session, organization_id: Optional[int]) -> models.OrgSettings:
    """Get or create the OrgSettings row for this organization."""
    q = db.query(models.OrgSettings)
    if organization_id is not None:
        q = q.filter(models.OrgSettings.organization_id == organization_id)
    row = q.first()
    if row is None:
        row = models.OrgSettings(company_name="Temveris", organization_id=organization_id)
        db.add(row)
        db.flush()
    return row


def _serialise_settings(db: Session, row: models.OrgSettings) -> dict:
    owner, since = _resolve_owner(db, row, organization_id=row.organization_id)
    return {
        # ── existing identity fields ──────────────────────────
        "company_name":         row.company_name,
        "slug":                 row.slug,
        "internal_description": row.internal_description,
        "logo_path":            row.logo_path,
        "logo_url":             row.logo_url,
        # ── existing operating context ────────────────────────
        "primary_country":      row.primary_country,
        "default_timezone":     row.default_timezone,
        "default_currency":     row.default_currency,
        "data_residency": {
            "region":  DATA_RESIDENCY_REGION,
            "label":   DATA_RESIDENCY_LABEL,
            "detail":  DATA_RESIDENCY_DETAIL,
            "managed": "infrastructure",
        },
        # ── new: contact & company profile ────────────────────
        "hr_email":             row.hr_email,
        "support_email":        row.support_email,
        "company_address":      row.company_address,
        "company_phone":        row.company_phone,
        "company_website":      row.company_website,
        "company_description":  row.company_description,
        # ── new: branding ─────────────────────────────────────
        "primary_brand_color":   row.primary_brand_color,
        "secondary_brand_color": row.secondary_brand_color,
        # ── ownership ─────────────────────────────────────────
        "owner":      _serialise_owner(owner, since),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/settings")
def get_org_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    row = (
        db.query(models.OrgSettings)
        .filter(models.OrgSettings.organization_id == org_id)
        .first()
    )
    if not row:
        # No row yet — synthesize a fresh-defaults response so the UI
        # always has something to render. We don't write the row from a
        # GET (idempotent reads only); the first PUT will create it.
        return {
            "company_name":         "Temveris",
            "slug":                 None,
            "internal_description": None,
            "logo_path":            None,
            "logo_url":             None,
            "primary_country":      None,
            "default_timezone":     None,
            "default_currency":     None,
            "data_residency": {
                "region":  DATA_RESIDENCY_REGION,
                "label":   DATA_RESIDENCY_LABEL,
                "detail":  DATA_RESIDENCY_DETAIL,
                "managed": "infrastructure",
            },
            "hr_email":             None,
            "support_email":        None,
            "company_address":      None,
            "company_phone":        None,
            "company_website":      None,
            "company_description":  None,
            "primary_brand_color":   None,
            "secondary_brand_color": None,
            "owner":      _serialise_owner(*_resolve_owner(db, None, organization_id=org_id)),
            "updated_at": None,
        }
    return _serialise_settings(db, row)


class OrgSettingsUpdate(BaseModel):
    """Partial update — every field is optional, only sent fields apply.

    Validation runs server-side as defence in depth (the SPA also
    validates). Bad inputs fail fast with 400 + specific message rather
    than silently storing garbage.
    """
    # existing identity fields
    company_name:         Optional[str] = None
    slug:                 Optional[str] = None
    internal_description: Optional[str] = None
    # existing operating context
    primary_country:      Optional[str] = None
    default_timezone:     Optional[str] = None
    default_currency:     Optional[str] = None
    # new: contact & company profile
    hr_email:             Optional[str] = None
    support_email:        Optional[str] = None
    company_address:      Optional[str] = None
    company_phone:        Optional[str] = None
    company_website:      Optional[str] = None
    company_description:  Optional[str] = None
    # new: branding
    primary_brand_color:   Optional[str] = None
    secondary_brand_color: Optional[str] = None


@router.put("/settings")
def update_org_settings(
    payload: OrgSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_admin_user),
):
    org_id = current_user.organization_id
    row = _row_or_create(db, org_id)
    before = {
        "company_name":          row.company_name,
        "slug":                  row.slug,
        "internal_description":  row.internal_description,
        "primary_country":       row.primary_country,
        "default_timezone":      row.default_timezone,
        "default_currency":      row.default_currency,
        "hr_email":              row.hr_email,
        "support_email":         row.support_email,
        "company_address":       row.company_address,
        "company_phone":         row.company_phone,
        "company_website":       row.company_website,
        "company_description":   row.company_description,
        "primary_brand_color":   row.primary_brand_color,
        "secondary_brand_color": row.secondary_brand_color,
    }

    if payload.company_name is not None:
        name = payload.company_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Company name cannot be empty")
        row.company_name = name

    if payload.slug is not None:
        slug = payload.slug.strip().lower()
        if slug and not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=400,
                detail="Slug must be lowercase letters, numbers, and single hyphens",
            )
        # Uniqueness check is scoped to this org — other orgs may use the same slug.
        if slug:
            clash = (
                db.query(models.OrgSettings)
                .filter(
                    models.OrgSettings.slug == slug,
                    models.OrgSettings.organization_id == org_id,
                    models.OrgSettings.id != row.id,
                )
                .first()
            )
            if clash:
                raise HTTPException(status_code=409, detail="Slug is already in use")
        row.slug = slug or None

    if payload.internal_description is not None:
        row.internal_description = payload.internal_description.strip() or None

    if payload.primary_country is not None:
        cc = payload.primary_country.strip().upper()
        if cc and not _COUNTRY_RE.match(cc):
            raise HTTPException(status_code=400, detail="Primary country must be a 2-letter ISO code")
        row.primary_country = cc or None

    if payload.default_timezone is not None:
        tz = payload.default_timezone.strip()
        # Validated as a real IANA zone, not just "a short string". This field
        # is not decoration: app._tz resolves it into the wall clock that
        # decides this tenant's attendance day, absence cutoffs, shift
        # boundaries, payroll periods and report dates. Free text silently fell
        # back to another zone, so an admin could believe they had configured
        # one timezone while every business decision was made in another.
        #
        # Only validated ON CHANGE. An organization whose stored value is
        # already invalid must still be able to edit its address or HR email —
        # this handler is one long sequence of field checks and a single raise
        # aborts the whole PUT, so validating unconditionally would lock such a
        # workspace out of its entire profile form until the timezone is fixed.
        if tz and tz != (row.default_timezone or "") and not _tz.is_valid_timezone(tz):
            raise HTTPException(
                status_code=400,
                detail=("default_timezone must be a valid IANA timezone "
                        "(e.g. Asia/Kolkata, America/New_York, Europe/London, UTC)"),
            )
        row.default_timezone = tz or None

    if payload.default_currency is not None:
        cur = payload.default_currency.strip().upper()
        if cur and not _CURRENCY_RE.match(cur):
            raise HTTPException(status_code=400, detail="Currency must be a 3-letter ISO 4217 code")
        row.default_currency = cur or None

    # ── new contact & company profile fields ──────────────────────────────
    if payload.hr_email is not None:
        v = payload.hr_email.strip().lower()
        if v and not _EMAIL_RE.match(v):
            raise HTTPException(status_code=400, detail="hr_email must be a valid email address")
        row.hr_email = v or None

    if payload.support_email is not None:
        v = payload.support_email.strip().lower()
        if v and not _EMAIL_RE.match(v):
            raise HTTPException(status_code=400, detail="support_email must be a valid email address")
        row.support_email = v or None

    if payload.company_address is not None:
        row.company_address = payload.company_address.strip() or None

    if payload.company_phone is not None:
        v = payload.company_phone.strip()
        if v and len(v) > 30:
            raise HTTPException(status_code=400, detail="company_phone must be 30 characters or fewer")
        row.company_phone = v or None

    if payload.company_website is not None:
        v = payload.company_website.strip()
        if v and not _URL_RE.match(v):
            raise HTTPException(status_code=400, detail="company_website must start with http:// or https://")
        row.company_website = v or None

    if payload.company_description is not None:
        row.company_description = payload.company_description.strip() or None

    # ── new branding fields ───────────────────────────────────────────────
    if payload.primary_brand_color is not None:
        v = payload.primary_brand_color.strip()
        if v and not _HEX_RE.match(v):
            raise HTTPException(status_code=400, detail="primary_brand_color must be a hex color e.g. #3D6BFF")
        row.primary_brand_color = v or None

    if payload.secondary_brand_color is not None:
        v = payload.secondary_brand_color.strip()
        if v and not _HEX_RE.match(v):
            raise HTTPException(status_code=400, detail="secondary_brand_color must be a hex color e.g. #00BFA9")
        row.secondary_brand_color = v or None

    after = {
        "company_name":          row.company_name,
        "slug":                  row.slug,
        "internal_description":  row.internal_description,
        "primary_country":       row.primary_country,
        "default_timezone":      row.default_timezone,
        "default_currency":      row.default_currency,
        "hr_email":              row.hr_email,
        "support_email":         row.support_email,
        "company_address":       row.company_address,
        "company_phone":         row.company_phone,
        "company_website":       row.company_website,
        "company_description":   row.company_description,
        "primary_brand_color":   row.primary_brand_color,
        "secondary_brand_color": row.secondary_brand_color,
    }
    diff = {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}
    if diff:
        db.add(models.AuditLog(
            actor_id        = current_user.id,
            target_id       = row.id,
            organization_id = org_id,
            entity          = "OrgSettings",
            action          = "WorkspaceProfileUpdated",
            payload         = {"changes": diff},
        ))

    db.commit()
    # Drop ONLY this organization's cached zone. app._tz caches for 60s to keep
    # /employees/team off a round-trip per poll; without this the admin changes
    # their workspace timezone and the dashboard keeps answering on the old one
    # for up to a minute. Per-org, never a global clear — another tenant's
    # cached value is not ours to invalidate. This clears the cache on THIS
    # process only; other Cloud Run instances expire on the normal TTL.
    if "default_timezone" in diff:
        _tz.invalidate_tz_cache(org_id)
    db.refresh(row)
    return _serialise_settings(db, row)


# ─── OWNERSHIP TRANSFER ──────────────────────────────────────────────────────
#
# Transfer is a sensitive operation: it grants the new owner all
# workspace privileges and is auditable. Caller must:
#   • be a current admin/HR
#   • include `confirm: true` in the body (stops accidental clicks)
#   • send the new owner's work email
# The new owner is promoted to admin if not already one. The previous
# owner is NOT demoted automatically — admins decide that separately.

class OwnerTransferRequest(BaseModel):
    new_owner_email: str
    confirm:         bool = False


@router.post("/owner/transfer")
def transfer_owner(
    payload: OwnerTransferRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_admin_user),
):
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Ownership transfer requires explicit confirmation (confirm: true)",
        )
    target_email = (payload.new_owner_email or "").strip().lower()
    if not target_email:
        raise HTTPException(status_code=400, detail="New owner email is required")

    org_id = current_user.organization_id

    # New owner must belong to the same organization — prevents cross-org ownership grab.
    new_owner = (
        db.query(models.User)
        .filter(
            func.lower(models.User.email) == target_email,
            models.User.organization_id == org_id,
        )
        .first()
    )
    if not new_owner:
        raise HTTPException(status_code=404, detail="No account with that email in this organization")
    if not new_owner.is_active:
        raise HTTPException(status_code=400, detail="That account is disabled")

    row = _row_or_create(db, org_id)
    prev_owner_id     = row.owner_user_id
    row.owner_user_id = new_owner.id
    row.owner_since   = datetime.now(timezone.utc)

    if new_owner.role not in ("admin", "hr"):
        new_owner.role = "admin"

    db.add(models.AuditLog(
        actor_id        = current_user.id,
        target_id       = new_owner.id,
        organization_id = org_id,
        entity          = "Workspace",
        action          = "OwnerTransferred",
        payload         = {
            "previous_owner_id": prev_owner_id,
            "new_owner_id":      new_owner.id,
            "new_owner_email":   new_owner.email,
        },
    ))

    db.commit()
    db.refresh(row)
    return _serialise_settings(db, row)


# ─── OWNER EVENTS (workspace-level audit feed) ───────────────────────────────
#
# Returns the most recent audit events that are owner-relevant — the
# Workspace Profile UI shows the last few of these on the Workspace
# Owner card. We filter on entity to keep noise out: only Workspace
# / OrgSettings / OwnerTransfer rows surface here.

_OWNER_EVENT_ENTITIES = ("Workspace", "OrgSettings")
_OWNER_EVENT_ACTIONS  = (
    "OwnerTransferred",
    "WorkspaceProfileUpdated",
    "LogoUpdated",
    "LogoRemoved",
)


@router.get("/owner/events")
def owner_events(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    limit = max(1, min(50, int(limit or 10)))

    # Single query: outerjoin fetches actor name/email in the same round-trip,
    # eliminating the N per-row User lookups.  Organization scope is enforced
    # via AuditLog.organization_id (set on every write since the multitenancy
    # hardening) instead of the old two-step user-ids-then-in_() pattern.
    rows = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(
              models.AuditLog.organization_id == current_user.organization_id,
              (models.AuditLog.entity.in_(_OWNER_EVENT_ENTITIES)) |
              (models.AuditLog.action.in_(_OWNER_EVENT_ACTIONS)),
          )
          .order_by(models.AuditLog.id.desc())
          .limit(limit)
          .all()
    )
    out = [
        {
            "id":          r.id,
            "action":      r.action,
            "entity":      r.entity,
            "payload":     r.payload,
            "actor_name":  u.name  if u else None,
            "actor_email": u.email if u else None,
            "ip_address":  r.ip_address,
            "at":          r.created_at.isoformat() if getattr(r, "created_at", None) else None,
        }
        for r, u in rows
    ]
    return {"events": out}


# ─── LINKED CONFIG STATUS ────────────────────────────────────────────────────
#
# Computes a real status for each module the Workspace Profile lists.
# Every value comes from a DB query or a real config check — no fake
# "Active" labels. Modules that genuinely don't have an implementation
# yet return status="not_implemented" with detail explaining why.

@router.get("/linked-status")
def linked_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    settings = (
        db.query(models.OrgSettings)
        .filter(models.OrgSettings.organization_id == org_id)
        .first()
    )

    user_total = db.query(func.count(models.User.id)).filter(
        models.User.organization_id == org_id
    ).scalar() or 0
    admin_count = (
        db.query(func.count(models.User.id))
        .filter(
            models.User.organization_id == org_id,
            models.User.role.in_(("admin", "hr")),
        )
        .scalar() or 0
    )
    employee_count = user_total - admin_count

    try:
        holiday_count = db.query(func.count(models.Holiday.id)).filter(
            models.Holiday.organization_id == org_id
        ).scalar() or 0
    except Exception:
        holiday_count = 0
    try:
        # Scoped to the caller's organization. Previously hardcoded to the
        # "default" sentinel, which returned organization 1's subscription to
        # every tenant. A user with no organization matches no row (IS NULL)
        # rather than inheriting another tenant's plan.
        billing_sub = db.query(models.BillingSubscription).filter(
            models.BillingSubscription.organization_id == org_id
        ).first()
        billing_plan = (
            db.query(models.BillingPlan).filter(models.BillingPlan.id == billing_sub.plan_id).first()
            if billing_sub and billing_sub.plan_id else None
        )
    except Exception:
        billing_sub = None
        billing_plan = None
    try:
        security_rows = {
            r.key: r.value
            for r in db.query(models.AppSettings).filter(
                models.AppSettings.organization_id == org_id,
                models.AppSettings.key.in_([
                    "security.mfa_required",
                    "security.sso_enabled",
                    "security.ip_allowlist_enabled",
                ]),
            ).all()
        }
        security_enabled = any(str(v).lower() == "true" for v in security_rows.values())
    except Exception:
        security_enabled = False
    integration_count = 0
    try:
        integration_row = db.query(models.AppSettings).filter(
            models.AppSettings.organization_id == org_id,
            models.AppSettings.key == "integrations.state_json",
        ).first()
        if integration_row and integration_row.value:
            try:
                integration_state = json.loads(integration_row.value)
                if isinstance(integration_state, dict):
                    integration_count = sum(1 for item in integration_state.values() if item.get("connected"))
            except Exception:
                integration_count = 0
    except Exception:
        integration_count = 0
    try:
        _audit_count_uids = [
            row.id for row in db.query(models.User.id)
            .filter(models.User.organization_id == org_id)
            .all()
        ]
        from sqlalchemy import or_ as _or_audit
        audit_count = db.query(func.count(models.AuditLog.id)).filter(
            _or_audit(
                models.AuditLog.actor_id.in_(_audit_count_uids),
                models.AuditLog.target_id.in_(_audit_count_uids),
            )
        ).scalar() or 0
    except Exception:
        audit_count = 0
    try:
        leave_policy_count = db.query(func.count(models.LeavePolicy.id)).filter(
            models.LeavePolicy.organization_id == org_id,
            models.LeavePolicy.is_active == True,
        ).scalar() or 0
    except Exception:
        leave_policy_count = 0

    # Custom Branding = workspace has either custom name or logo.
    branding_active = bool(
        settings and (
            settings.logo_url
            or (settings.company_name and settings.company_name != "Temveris")
        )
    )

    return {
        "billing": {
            "title":  "Billing & Subscription",
            "status": billing_sub.subscription_status if billing_sub else "trial",
            "detail": (
                f"{billing_plan.name} · {billing_sub.subscription_status}"
                if billing_sub and billing_plan else "Trial workspace"
            ),
        },
        "public_holidays": {
            "title":  "Public Holidays",
            "status": "configured" if (holiday_count or 0) > 0 else "incomplete",
            "detail": (
                f"{holiday_count} holidays configured" if holiday_count
                else ("No holidays configured yet" if holiday_count == 0 else "Module not enabled")
            ),
        },
        "security": {
            "title":  "Security",
            "status": "configured" if security_enabled else "incomplete",
            "detail": "MFA/SSO/IP controls configured" if security_enabled else "MFA, SSO, and IP rules not configured",
        },
        "audit_compliance": {
            "title":  "Audit & Compliance",
            "status": "active",
            "detail": f"{audit_count} audit events recorded",
        },
        "policies": {
            "title":  "Policies",
            "status": "configured" if leave_policy_count else "review",
            "detail": "Workforce + leave policy overrides configured" if leave_policy_count else "Review workforce + leave policies",
        },
        "roles_permissions": {
            "title":  "Roles & Permissions",
            "status": "configured" if admin_count > 0 else "incomplete",
            "detail": f"{admin_count} admin/HR · {employee_count} workers",
        },
        "integrations": {
            "title":  "Integrations",
            "status": "configured" if integration_count else "notset",
            "detail": f"{integration_count} connected system(s)" if integration_count else "No integrations connected yet",
        },
        "custom_branding": {
            "title":  "Custom Branding",
            "status": "active" if branding_active else "notset",
            "detail": "Logo & name set" if branding_active else "Not configured",
        },
    }





@router.post("/logo")
async def upload_logo(
    logo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_admin_user),
):
    if logo.content_type not in ("image/jpeg", "image/png", "image/jpg", "image/gif", "image/webp"):
        raise HTTPException(status_code=400, detail="Only jpg/png/gif/webp allowed")

    ext      = logo.filename.rsplit(".", 1)[-1].lower()
    org_id   = current_user.organization_id
    filename = f"org_{org_id}/company_logo.{ext}"

    from app.storage import put_upload_file, ORG_LOGOS
    logo_url = put_upload_file(ORG_LOGOS, filename, logo, logo.content_type)
    filepath = logo_url

    org_id = current_user.organization_id
    row = (
        db.query(models.OrgSettings)
        .filter(models.OrgSettings.organization_id == org_id)
        .first()
    )
    if row:
        row.logo_path = filepath
        row.logo_url  = logo_url
    else:
        db.add(models.OrgSettings(
            organization_id = org_id,
            logo_path       = filepath,
            logo_url        = logo_url,
        ))
    db.add(models.AuditLog(
        actor_id        = current_user.id,
        organization_id = org_id,
        entity          = "OrgSettings",
        action          = "LogoUpdated",
        payload         = {"logo_url": logo_url},
    ))
    db.commit()
    return {"success": True, "logo_url": logo_url}


@router.delete("/logo")
def remove_logo(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_admin_user),
):
    org_id = current_user.organization_id
    row = (
        db.query(models.OrgSettings)
        .filter(models.OrgSettings.organization_id == org_id)
        .first()
    )
    if row and row.logo_path and os.path.exists(row.logo_path):
        try:
            os.remove(row.logo_path)
        except Exception:
            pass
    if row:
        row.logo_path = None
        row.logo_url  = None
        db.add(models.AuditLog(
            actor_id        = current_user.id,
            organization_id = org_id,
            entity          = "OrgSettings",
            action          = "LogoRemoved",
            payload         = {},
        ))
        db.commit()
    return {"success": True}


# ─── TEAMS CRUD ──────────────────────────────────────────────────────────────

@router.get("/teams")
def list_teams(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    org_id = user.organization_id

    # Employee count scoped to this org's active users only.
    employee_count_subquery = db.query(
        models.User.team, func.count(models.User.id).label('count')
    ).filter(
        models.User.is_active == True,
        models.User.organization_id == org_id,
    ).group_by(models.User.team).subquery()

    teams = db.query(
        models.Team,
        models.User,
        employee_count_subquery.c.count,
    ).filter(
        models.Team.organization_id == org_id,
    ).outerjoin(
        models.User, models.Team.head_user_id == models.User.id
    ).outerjoin(
        employee_count_subquery, models.Team.name == employee_count_subquery.c.team
    ).order_by(models.Team.created_at.asc()).all()

    return [
        {
            "id":             t.id,
            "name":           t.name,
            "head_user_id":   t.head_user_id,
            "head_name":      u.name if u else None,
            "head_email":     u.email if u else None,
            "employee_count": count or 0,
        }
        for t, u, count in teams
    ]


@router.post("/teams")
def create_team(
    payload: dict,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(_admin_user),
):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Team name is required")

    # Plan team limit. Unlimited on every current tier, so a no-op today; wired
    # here so that if a cap is ever configured the wizard's displayed limit is
    # backed by real server-side enforcement rather than being decorative.
    check_team_limit(db, admin, adding=1)

    existing = db.query(models.Team).filter(
        models.Team.organization_id == admin.organization_id,
        models.Team.name == name,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Team already exists")

    new_team = models.Team(
        name=name,
        head_user_id=payload.get("head_user_id"),
        organization_id=admin.organization_id,
    )
    db.add(new_team)
    db.commit()
    return {"success": True, "name": name}


@router.put("/teams/{team_id}")
def update_team(
    team_id: int,
    payload: dict,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(_admin_user),
):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")

    team = db.query(models.Team).filter(
        models.Team.id == team_id,
        models.Team.organization_id == admin.organization_id,
    ).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Guard against a rename that would collide with another team in this org.
    clash = db.query(models.Team).filter(
        models.Team.organization_id == admin.organization_id,
        models.Team.name == name,
        models.Team.id != team_id,
    ).first()
    if clash:
        raise HTTPException(status_code=409, detail="A team with that name already exists")

    # Membership is stored as User.team == Team.name (no join table), so a
    # rename MUST re-point every assigned worker from the old name to the new
    # one — otherwise they silently orphan (their User.team no longer matches
    # any team) and the department appears to lose all its members.
    old_name = team.name
    if old_name != name:
        db.query(models.User).filter(
            models.User.organization_id == admin.organization_id,
            models.User.team == old_name,
        ).update({models.User.team: name}, synchronize_session=False)

    team.name = name
    # Only overwrite the department lead when the caller actually sent one —
    # the rename dialog omits head_user_id, and blindly defaulting to None here
    # would wipe the assigned lead on every rename.
    if "head_user_id" in payload:
        team.head_user_id = payload.get("head_user_id")
    db.commit()
    return {"success": True}


@router.delete("/teams/{team_id}")
def delete_team(
    team_id: int,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(_admin_user),
):
    team = db.query(models.Team).filter(
        models.Team.id == team_id,
        models.Team.organization_id == admin.organization_id,
    ).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Detach any users currently flagged as belonging to this team so we
    # don't leave them pointing at a name that no longer exists. We do not
    # delete the users themselves.
    db.query(models.User).filter(models.User.team == team.name).update(
        {models.User.team: None}, synchronize_session=False
    )
    db.delete(team)
    db.commit()
    return {"success": True}


# ─── TEAM MEMBERSHIP ─────────────────────────────────────────────────────────
#
# Membership is stored on User.team (a string) — there is no join table
# today. Multitenancy compatibility: after the org_id merge, User rows
# already carry organization_id, and the queries below only ever return
# users the caller can already see via the existing employee/users
# endpoints (same auth scope). Cross-org name collisions ("Dev Team" in
# org A vs org B) resolve correctly because the User → Team match is
# implicitly scoped by the user's own organization_id.

class _TeamMembersAddBody(BaseModel):
    user_ids: list[int]


@router.get("/teams/{team_id}/members")
def list_team_members(
    team_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Users currently assigned to this team."""
    org_id = user.organization_id
    team = db.query(models.Team).filter(
        models.Team.id == team_id,
        models.Team.organization_id == org_id,
    ).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Scope to the caller's org: membership is a User.team string match, so
    # without this filter a same-named team in another tenant would leak
    # members — and any added foreign-org user would show here but never
    # count on the grid (which IS org-scoped). See list_teams.
    members = (
        db.query(models.User)
        .filter(
            models.User.team == team.name,
            models.User.organization_id == org_id,
            models.User.is_active == True,  # noqa: E712
        )
        .order_by(models.User.name.asc())
        .all()
    )
    return [
        {
            "id":          u.id,
            "name":        u.name,
            "email":       u.email,
            "role":        u.role,
            "designation": u.designation,
            "department":  u.department,
        }
        for u in members
    ]


@router.get("/teams/{team_id}/assignable-users")
def list_assignable_users(
    team_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Active users not currently in this team — candidates for adding."""
    org_id = user.organization_id
    team = db.query(models.Team).filter(
        models.Team.id == team_id,
        models.Team.organization_id == org_id,
    ).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Only offer candidates from the caller's own org — otherwise the admin
    # can assign a foreign-org user who then orphans on the next rename and
    # never appears in the org-scoped worker count.
    rows = (
        db.query(models.User)
        .filter(
            models.User.organization_id == org_id,
            models.User.is_active == True,  # noqa: E712
            (models.User.team != team.name) | (models.User.team.is_(None)),
        )
        .order_by(models.User.name.asc())
        .all()
    )
    return [
        {
            "id":           u.id,
            "name":         u.name,
            "email":        u.email,
            "role":         u.role,
            "current_team": u.team,
        }
        for u in rows
    ]


@router.post("/teams/{team_id}/members")
def add_team_members(
    team_id: int,
    payload: _TeamMembersAddBody,
    db: Session = Depends(get_db),
    admin: models.User = Depends(_admin_user),
):
    """Add one or more users to this team.

    Idempotent: re-adding a user already on this team is a no-op.
    If a user belongs to a different team, they are moved (User.team
    holds a single team name today).
    """
    org_id = admin.organization_id
    team = db.query(models.Team).filter(
        models.Team.id == team_id,
        models.Team.organization_id == org_id,
    ).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if not payload.user_ids:
        raise HTTPException(status_code=400, detail="user_ids is required")

    # Restrict to users in the caller's org. Assigning a foreign-org user
    # here is what produced the "0 workers / member wiped on rename" bug:
    # both the count and the rename re-point queries are org-scoped, so a
    # cross-org member is invisible to them.
    users = (
        db.query(models.User)
        .filter(
            models.User.id.in_(payload.user_ids),
            models.User.organization_id == org_id,
            models.User.is_active == True,  # noqa: E712
        )
        .all()
    )
    if not users:
        raise HTTPException(status_code=404, detail="No active users found")

    moved = []
    for u in users:
        if u.team != team.name:
            moved.append({"id": u.id, "name": u.name, "from": u.team})
            u.team = team.name

    if moved:
        db.add(models.AuditLog(
            actor_id=admin.id,
            target_id=team.id,
            entity="Team",
            action="MembersAdded",
            organization_id=org_id,
            payload={"team_id": team.id, "team_name": team.name, "users": moved},
        ))
    db.commit()
    return {"success": True, "added": len(moved), "team_id": team.id}


@router.delete("/teams/{team_id}/members/{user_id}")
def remove_team_member(
    team_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(_admin_user),
):
    """Remove a user from this team (sets User.team to NULL)."""
    org_id = admin.organization_id
    team = db.query(models.Team).filter(
        models.Team.id == team_id,
        models.Team.organization_id == org_id,
    ).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    user = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.organization_id == org_id,
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.team != team.name:
        # No-op — keeps the endpoint idempotent so repeated DELETEs don't 404.
        return {"success": True, "changed": False}

    user.team = None
    db.add(models.AuditLog(
        actor_id=admin.id,
        organization_id=org_id,
        target_id=user.id,
        entity="Team",
        action="MemberRemoved",
        payload={"team_id": team.id, "team_name": team.name, "user_id": user.id},
    ))
    db.commit()
    return {"success": True, "changed": True}
