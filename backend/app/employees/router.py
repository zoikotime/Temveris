from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Request
from app.rate_limit import limiter  # per-endpoint rate limits (invite abuse guard)
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, text, inspect as sa_inspect
from datetime import datetime, timezone, timedelta, timezone, date as date_type
from typing import Optional, Literal
from pydantic import BaseModel, EmailStr
import uuid
import os
import re

from database.database import get_db
from database import models
# Billable-seat states live with billing; employee creation records the state so
# seats derive correctly (ZT-COM-BILL-001 §17).
from app.billing import worker_state
from app.auth.service import get_current_user, get_current_organization, hash_password
from app.commercial.enforcement import check_seat_limit
from app.email_service import (
    send_invite_email,
    send_absent_alert_to_admin_hr,
    send_time_adjustment_email,
    send_time_adjustment_reverted_email,
    send_early_clockout_alert,
)
from app._time_utils import iso_z
from app._system_admin import (
    is_system_admin,
    is_non_working_role,
    exclude_system_admin,
)
from app._time_adjustments import (
    adjustment_seconds_for_day,
    adjustment_seconds_for_day_by_user,
)
from app.breaks import governance as break_governance
from app.attendance_desktop_policy import (
    desktop_clock_in_required,
    enforce_desktop_attendance,
    enforce_desktop_clock_in,
)
from app.attendance_policy import (
    resolve_expected_work_seconds,
    is_flexible_clock_out_day,
)
from app import _tz
from app.notifications.recipients import recipients_for_user

# §45.D-15 — session-boundary provenance. Every write to clock_in_time or
# clock_out_time below records which of the five owner-approved origins it
# represents, in the same transaction as the boundary write. Provenance is
# never inferred from the endpoint, the timestamp or the session state: the
# writer states it, or `append_boundary_event` refuses.
from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    SYSTEM_ACTOR_LABEL,
    Provenance as _Provenance,
    actor_label_for_user,
    append_boundary_event,
)

import logging as _logging_att
_log_att = _logging_att.getLogger("zoiko.attendance")


def _device_info(request) -> dict:
    """Best-effort device fingerprint for the break-governance audit stream.
    The desktop tracker sends X-Device-Id; browsers won't, so it may be None."""
    if request is None:
        return {}
    try:
        return {
            "deviceId":   request.headers.get("x-device-id") or request.headers.get("x-device"),
            "ip_address": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }
    except Exception:
        return {}

router = APIRouter(prefix="/employees", tags=["Employees"])

FRONTEND_URL    = os.getenv("FRONTEND_URL",    "http://localhost:5173")
API_BASE        = os.getenv("FRONTEND_URL",    "http://localhost:5173")

# Small in-process TTL cache shared by several read-heavy admin endpoints.
# Supabase round-trips make multi-query aggregations slow (~20–30s with 30
# users); the admin dashboard polls these every 30–60s so even a few seconds
# of staleness is invisible but cuts DB load dramatically.
_CACHE: dict = {}

# There is no module-level workforce timezone and no module-level default
# shift.
#
# `SYSTEM_DEFAULT_CLOCK_IN = "10:30"` used to live here — one customer's
# 2026-05-13 product decision applied to every workspace, and directly at odds
# with `schedule.shift_start_time`, whose system default is deliberately EMPTY
# ("no scheduled start", so an unscheduled worker can never be marked late).
# `SYSTEM_DEFAULT_CLOCK_IN_GRACE_MIN = 20` was a third competing grace default
# alongside `schedule.grace_minutes` and attendance_alerts.DEFAULT_GRACE_MIN.
# Both are now resolved per employee by the Schedule Resolver.
#
# `_DASHBOARD_TZ` was a fixed +05:30. Dashboard "today" buckets now reset at
# local midnight in each ORGANIZATION's configured timezone
# (OrgSettings.default_timezone via app._tz), which is the only way two tenants
# in different countries can both see a correct "today".


def _today_dashboard(db, org_id) -> date_type:
    """Current calendar date on this organization's wall clock."""
    return _tz.local_date(db, org_id)


def _to_dashboard_date(db, org_id, dt) -> Optional[date_type]:
    """Coerce a stored UTC timestamp to its calendar date in the org's local time.

    DB columns use DateTime(timezone=True) so values come back tz-aware,
    but older rows may be naive — we treat naive as UTC so the
    conversion is consistent across the table.
    """
    if dt is None:
        return None
    return _tz.to_local(db, org_id, dt).date()

def _utc_aware(dt):
    """Treat a naive stored timestamp as UTC — same convention as
    _to_dashboard_date above. Postgres returns tz-aware values, but legacy rows
    (and SQLite under test) can be naive, which makes any comparison against an
    aware boundary raise "can't compare offset-naive and offset-aware".
    clock_out already normalizes inline for exactly this reason; this is that
    guard as a helper so the read paths can share it."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if entry and datetime.now(timezone.utc) < entry["expires_at"]:
        return entry["data"]
    return None

def _cache_set(key: str, data, ttl_seconds: int):
    _CACHE[key] = {"data": data, "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)}

# Per-org team cache — keyed by organization_id so org A's snapshot
# is never served to org B. Each entry: {"data": list, "expires_at": datetime}
_TEAM_CACHE: dict = {}   # org_id -> {"data": list, "expires_at": datetime}
_TEAM_CACHE_TTL_SECONDS = 30


def _team_cache_get(org_id: int):
    entry = _TEAM_CACHE.get(org_id)
    if entry and entry["data"] is not None and datetime.now(timezone.utc) < entry["expires_at"]:
        return entry["data"]
    return None


def _team_cache_set(org_id: int, data, ttl: int = _TEAM_CACHE_TTL_SECONDS):
    _TEAM_CACHE[org_id] = {
        "data": data,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl),
    }


def _team_cache_invalidate_all():
    """Invalidate all orgs' caches (used on clock-in/out/break events).

    Defensive on purpose. This runs on the clock-in / clock-out / break write
    paths, where it is called AFTER the DB commit — so anything it raises turns
    a successful attendance write into a 500 for the worker while the session is
    already closed. It must never raise.

    It previously assumed every value was a `{"data":..., "expires_at":...}`
    dict. Two callers set `_TEAM_CACHE["data"] = None` instead of calling this
    helper, which put a bare None in the dict; the next invalidation then died
    with "TypeError: 'NoneType' object does not support item assignment" and
    every clock-out returned 500 until the process restarted. Both callers are
    fixed, and skipping non-conforming entries here keeps a future one from
    breaking attendance the same way.
    """
    for key, entry in list(_TEAM_CACHE.items()):
        if isinstance(entry, dict):
            entry["data"] = None
        else:
            # Not a cache record (legacy/bad write) — drop it outright.
            _TEAM_CACHE.pop(key, None)

# ===========================
# INVITE BY EMAIL (simple)
# Called by AdminDashboard "Invite User" modal
# Accepts just an email — name is derived and updated on Set Password page
# ===========================
class InviteByEmail(BaseModel):
    email: EmailStr
    name: str | None = None   # optional — admin can provide, or it's derived from email
    # Official Start Date — drives leave eligibility. Accepted under either
    # name; the admin invite modal sends `start_date`.
    start_date: date_type | None = None
    date_of_joining: date_type | None = None


@router.post("/invite")
def invite_by_email(
    data: InviteByEmail,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only admin or HR can invite employees")

    # Seat limit. No-op for tenants whose entitlement cannot be resolved (e.g.
    # super-admin-provisioned orgs, which have no subscription row) so existing
    # customers are never told they are out of seats — see
    # app.commercial.enforcement.
    check_seat_limit(db, current_user, adding=1)

    email = data.email.lower().strip()

    # Derive a readable placeholder name from the email address if none given
    raw_name = data.name.strip() if data.name and data.name.strip() else ""
    if not raw_name:
        prefix = email.split("@")[0]              # e.g. "john.doe"
        raw_name = prefix.replace(".", " ").replace("_", " ").replace("-", " ").title()

    # Generate fresh 24-hour invite token
    invite_token  = str(uuid.uuid4())
    token_expires = datetime.now(timezone.utc) + timedelta(hours=24)

    # Resolve start date — accept either field name.
    start_dt = data.start_date or data.date_of_joining
    official_start_dt = datetime.combine(start_dt, datetime.min.time()) if start_dt else None

    # Cross-org guard: find any existing user with this email system-wide.
    # We must check all orgs — not just our own — to prevent an admin from
    # overwriting another org's active account by re-inviting their email.
    system_user = db.query(models.User).filter(models.User.email == email).first()

    # Plan capacity, checked at invite time and counting pending invitations.
    #
    # An invitation reserves capacity even though it is never charged (§H3), so this is
    # the right place to refuse: the admin sending the invite can upgrade, whereas the
    # invited person later setting their password cannot. A re-send is exempt — the
    # capacity was already reserved by the original invitation.
    if system_user is None:
        from app.billing import service as _billing_service      # noqa: PLC0415
        try:
            # SAVEPOINT: a failed read here would otherwise poison the session and take
            # the whole invite down. See app/schedule_policy/resolver.py for the idiom.
            with db.begin_nested():
                _billing_service.assert_room_for_new_worker(db, current_user.organization_id)
        except _billing_service.PlanCapacityExceeded as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:                                 # noqa: BLE001
            # Any other failure is a billing-infrastructure problem, not the admin's.
            # Inviting a teammate is core workflow and must not be blocked by it, so
            # this fails OPEN and logs loudly — the same stance the suspension gate takes.
            _log_att.warning("capacity pre-check unavailable for org=%s (%s) — allowing invite",
                             current_user.organization_id, type(exc).__name__)

    if system_user:
        same_org = (system_user.organization_id == current_user.organization_id
                    or system_user.organization_id is None)
        if not same_org:
            # Email belongs to a user in a different org.
            # Return a generic message — do NOT reveal cross-org existence.
            raise HTTPException(
                status_code=409,
                detail="This email address cannot be invited",
            )
        if system_user.is_password_set:
            # Account is already fully activated — re-invite would generate a
            # new token that could be used to reset a live account's password.
            raise HTTPException(
                status_code=409,
                detail="This employee has already activated their account. Use 'Resend Invite' only for pending accounts.",
            )
        # Safe to re-invite: same org, password not yet set.
        # Never overwrite organization_id here — it is already the correct value
        # (same_org condition guarantees this).
        system_user.invite_token            = invite_token
        system_user.invite_token_expires_at = token_expires
        system_user.is_invited              = True
        if raw_name and raw_name != system_user.name:
            system_user.name = raw_name
        if official_start_dt is not None:
            system_user.official_start_date = official_start_dt
        db.commit()
        db.refresh(system_user)
        user     = system_user
        is_resend = True
    else:
        user = models.User(
            name                    = raw_name,
            email                   = email,
            password                = None,
            role                    = "employee",
            organization_id         = current_user.organization_id,
            is_active               = True,
            is_invited              = True,
            is_password_set         = False,
            invite_token            = invite_token,
            invite_token_expires_at = token_expires,
            official_start_date     = official_start_dt,
            # An invitation reserves plan capacity but is never charged
            # (ZT-COM-BILL-001 §H3). billing_state is the sole authority for
            # billability — is_invited is not cleared on acceptance and cannot
            # be used for this.
            billing_state           = worker_state.INVITED_PENDING,
        )
        try:
            db.add(user)
            db.commit()
            db.refresh(user)
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
        is_resend = False

    # Build invite link
    frontend_url  = os.getenv("FRONTEND_URL", "http://localhost:5173")
    invite_link   = f"{frontend_url}/set-password/{invite_token}"

    # Send invite email (non-fatal — user can still be resent later)
    email_sent = False
    try:
        email_sent = send_invite_email(
            to_email      = email,
            employee_name = user.name,
            invite_link   = invite_link,
            inviter_name  = "Admin",
        )
    except Exception as e:
        print(f"❌ INVITE EMAIL ERROR: {e}")

    return {
        "message":     "Invite resent" if is_resend else ("Invite sent" if email_sent else "Employee created — email delivery failed (check logs)"),
        "email":       email,
        "invite_sent": email_sent,
        "id":          user.id,
    }


# ===========================
# SCHEMA
# ===========================
class EmployeeCreate(BaseModel):
    name: str
    email: EmailStr
    role: str = "employee"
    designation: str | None = None
    phone: str | None = None
    employment_type: str | None = "full_time"
    team: str | None = None
    department: str | None = None
    # Official Start Date — drives leave eligibility. Accepted under either
    # `start_date` (spec wording, preferred) or `date_of_joining` (legacy
    # Pydantic field name kept for older API clients).
    start_date: date_type | None = None
    date_of_joining: date_type | None = None


# ===========================
# ADD EMPLOYEE + SEND INVITE
# ===========================
@router.post("")  # ✅ FIXED: was "/add", now matches POST /employees
def add_employee(
    data: EmployeeCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only admin or HR can add employees")

    check_seat_limit(db, current_user, adding=1)

    print("🔥 EMPLOYEE API HIT")

    email = data.email.lower()
    name  = data.name.strip()
    role  = data.role.lower()

    # Prevent tenant admins from creating accounts with elevated platform roles.
    # Mirrors the guard in update_employee — only super_admin may assign these.
    if role in ("super_admin", "owner") and current_user.role != "super_admin":
        raise HTTPException(status_code=403,
                            detail=f"Only super_admin may assign the '{role}' role")

    # 1. duplicate check — search system-wide so we never create a duplicate
    # account for an email already registered in any org.
    # Return a generic 400 regardless of whether the collision is in-org or
    # cross-org, to avoid leaking cross-org account existence.
    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        print("⚠️ USER ALREADY EXISTS:", email)
        raise HTTPException(status_code=400, detail="A user with this email already exists")

    # 2. generate token + 24-hr expiry
    invite_token  = str(uuid.uuid4())
    token_expires = datetime.now(timezone.utc) + timedelta(hours=24)

    # 3. create user WITH token saved in DB
    # `start_date` is the canonical field; `date_of_joining` is accepted
    # as an alias for older API clients.
    start_dt = data.start_date or data.date_of_joining
    official_start_dt = datetime.combine(start_dt, datetime.min.time()) if start_dt else None
    new_user = models.User(
        name                    = name,
        email                   = email,
        password                = None,
        role                    = role,
        organization_id         = current_user.organization_id,
        designation             = data.designation,
        phone                   = data.phone,
        employment_type         = data.employment_type,
        team                    = data.team,
        department              = data.department,
        official_start_date     = official_start_dt,
        invite_token            = invite_token,
        invite_token_expires_at = token_expires,
        is_invited              = True,
        is_password_set         = False,
        is_active               = True,
        # Invited, not accepted — reserves capacity, not billed (§H3).
        billing_state           = worker_state.INVITED_PENDING,
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        print("✅ USER SAVED IN DB:", email, "| token:", invite_token)
    except Exception as e:
        db.rollback()
        print("❌ DB ERROR:", str(e))
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")

    # 4. send invite email (non-fatal)
    frontend_url  = os.getenv("FRONTEND_URL", "http://localhost:5173")
    invite_link   = f"{frontend_url}/set-password/{invite_token}"
    print("🔗 INVITE LINK:", invite_link)

    email_sent = False
    try:
        email_sent = send_invite_email(
            to_email      = email,
            employee_name = name,
            invite_link   = invite_link,
            inviter_name  = "Admin",
        )
        print("📩 EMAIL RESULT:", email_sent)
    except Exception as e:
        print("❌ EMAIL ERROR:", str(e))

    return {
        "message":     "Employee created & invite sent" if email_sent else "Employee created (email failed — check logs)",
        "id":          new_user.id,
        "email":       email,
        "invite_sent": email_sent,
    }


# ===========================
# ADD EMPLOYEE DIRECTLY (admin sets password, no email flow)
# Used by AdminDashboard "Invite Worker" → "Add Directly" tab
# Admin types the credentials and shares them with the user out-of-band.
# ===========================
class EmployeeCreateDirect(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "employee"
    designation: str | None = None
    phone: str | None = None
    employment_type: str | None = "full_time"
    team: str | None = None
    department: str | None = None
    start_date: date_type | None = None
    date_of_joining: date_type | None = None


@router.post("/add-direct")
def add_employee_direct(
    data: EmployeeCreateDirect,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin/HR only")

    check_seat_limit(db, current_user, adding=1)

    email = data.email.lower().strip()
    name  = data.name.strip()
    role  = (data.role or "employee").lower()

    # Prevent tenant admins from creating accounts with elevated platform roles.
    if role in ("super_admin", "owner") and current_user.role != "super_admin":
        raise HTTPException(status_code=403,
                            detail=f"Only super_admin may assign the '{role}' role")

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters")
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = db.query(models.User).filter(models.User.email == email).first()
    if existing:
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    # Plan capacity, checked BEFORE the row is created. This account is billable from
    # creation, so without this the organization would silently pass its plan ceiling.
    # Refused here, to the admin, rather than later to the employee.
    from app.billing import service as _billing_service          # noqa: PLC0415
    try:
        with db.begin_nested():
            _billing_service.assert_room_for_new_worker(db, current_user.organization_id)
    except _billing_service.PlanCapacityExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:                                     # noqa: BLE001
        _log_att.warning("capacity pre-check unavailable for org=%s (%s) — allowing add-direct",
                         current_user.organization_id, type(exc).__name__)

    start_dt = data.start_date or data.date_of_joining
    official_start_dt = datetime.combine(start_dt, datetime.min.time()) if start_dt else None
    new_user = models.User(
        name                    = name,
        email                   = email,
        password                = hash_password(data.password),
        role                    = role,
        organization_id         = current_user.organization_id,
        designation             = data.designation,
        phone                   = data.phone,
        employment_type         = data.employment_type,
        team                    = data.team,
        department              = data.department,
        official_start_date     = official_start_dt,
        is_active               = True,
        is_invited              = False,
        is_password_set         = True,
        # Created active with a password — billable from creation.
        billing_state           = worker_state.ACTIVE_BILLABLE,
        invite_token            = None,
        invite_token_expires_at = None,
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")

    return {
        "message": "Employee created with credentials",
        "id":      new_user.id,
        "email":   email,
        "name":    name,
        "role":    role,
    }


# ===========================
# RESEND INVITE
# ===========================
@router.post("/{emp_id}/resend-invite")
def resend_invite(
    emp_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Only admin or HR can resend invites")

    emp = db.query(models.User).filter(
        models.User.id == emp_id,
        models.User.organization_id == current_user.organization_id,
    ).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    if emp.is_password_set:
        raise HTTPException(status_code=400, detail="Employee has already set their password")

    # org_id is already correct (guaranteed by the filter above — do not overwrite)
    emp.invite_token            = str(uuid.uuid4())
    emp.invite_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    emp.is_invited              = True
    db.commit()
    db.refresh(emp)

    frontend_url  = os.getenv("FRONTEND_URL", "http://localhost:5173")
    invite_link   = f"{frontend_url}/set-password/{emp.invite_token}"
    email_sent    = False
    try:
        email_sent = send_invite_email(
            to_email      = emp.email,
            employee_name = emp.name,
            invite_link   = invite_link,
            inviter_name  = "Admin",
        )
    except Exception as e:
        print("❌ RESEND EMAIL ERROR:", str(e))

    return {
        "message":     "Invite resent" if email_sent else "Token refreshed but email failed",
        "invite_sent": email_sent,
    }


# ===========================
# GET ALL EMPLOYEES
# ===========================
@router.get("")
def get_employees(
    skip: int = Query(0, ge=0, description="Pagination offset (opt-in)"),
    limit: Optional[int] = Query(
        None, ge=1, le=1000,
        description="Max rows to return. Omit for the full list (backward-compatible default).",
    ),
    active_only: bool = Query(
        False,
        description=(
            "Return only currently-selectable employees of this organization: "
            "is_active=True, no platform super-admin identity, and none of the "
            "org's configured non-working seats. Opt-in — the default response "
            "still includes deactivated/invited rows because the Workers & Users "
            "management page and the offboarding directory need them."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # Org-scoped + backward-compatible pagination: with no `limit`, returns all
    # employees in this organization. Callers that opt in with ?limit=&skip=
    # get a bounded page.
    #
    # `active_only` exists because this one endpoint serves two different
    # questions, and they need different answers:
    #
    #   • "who is on the roster, whatever their state?" — Workers & Users
    #     (renders an Inactive badge and an is_active toggle) and the offboarding
    #     directory (must resolve a FORMER worker's id back to a name). Both need
    #     deactivated rows, which is why the default cannot filter them out.
    #   • "who can I pick right now?" — every target picker, e.g. Organization
    #     Settings → Employee scope. Offering a deactivated account there is a
    #     bug: `User.is_active=False` is this codebase's single canonical
    #     "no longer a valid user" state (there is no soft-delete column and no
    #     membership table — the offboarding access-revocation engine and the
    #     auth gate in app/auth/service.py both key off exactly this flag).
    #
    # The filter therefore mirrors the canonical roster query in
    # `get_team_status` below: org + is_active + exclude_system_admin.
    if active_only and current_user.organization_id is None:
        # Org-less callers are platform identities (super_admin). With a Python
        # None on the right-hand side the filter below compiles to
        # `organization_id IS NULL`, which would hand back every org-less row
        # rather than matching nothing — so there is no such thing as "this
        # caller's selectable employees".
        return []

    q = db.query(models.User).filter(
        models.User.organization_id == current_user.organization_id
    )
    if active_only:
        q = q.filter(
            models.User.is_active == True,  # noqa: E712 — SQL predicate
            # Platform super-admins are never employees of a tenant. They are
            # normally org-less (and so already excluded above), but a role check
            # keeps a mis-provisioned row out of a tenant's picker too.
            func.lower(func.coalesce(models.User.role, "")) != "super_admin",
        )
    if limit is not None:
        q = q.offset(skip).limit(limit)
    employees = q.all()
    if active_only:
        # Non-working seats (the seeded owner/CEO account) are identified by the
        # org's `attendance.non_working_account_names` setting, not in SQL, so the
        # strip happens after the fetch — as it does everywhere else. When paging
        # is also requested a page can therefore come back short; the pickers that
        # use active_only do not page.
        employees = exclude_system_admin(employees, db)
    return [
        {
            "id":               emp.id,
            "name":             emp.name,
            "email":            emp.email,
            "role":             emp.role,
            "designation":      emp.designation,
            "department":       emp.department,
            "team":             emp.team,
            # Additive. Organization Settings → Working Schedule reads this at
            # Employee scope: employment type lives on the employee record, not in
            # the settings cascade (Approach A — one source of truth for a fact the
            # risk engine and reports already read from this column), so the row
            # has to show the real value rather than an inherited one.
            "employment_type":  emp.employment_type or "full_time",
            "employee_id":      emp.employee_id,
            "is_active":        emp.is_active,
            "is_invited":       emp.is_invited or False,
            "is_password_set":  emp.is_password_set or False,
            # Joining date — returned so the Edit Employee form can pre-fill AND
            # preserve it. Without it the date box opened blank, and re-saving any
            # other field sent an empty start_date, which update_employee treats as
            # "clear" — silently wiping official_start_date to NULL. Date-only ISO
            # string, read directly by the frontend's <input type="date">.
            "official_start_date": (emp.official_start_date.date().isoformat()
                                    if emp.official_start_date else None),
            "onboarding_status": (
                "active"   if emp.is_active and emp.is_password_set else
                "invited"  if emp.is_invited and not emp.is_password_set else
                "inactive"
            ),
        }
        for emp in employees
    ]


# ===========================
# UPDATE EMPLOYEE
# ===========================
@router.put("/{emp_id}")
def update_employee(
    emp_id: int,
    emp_data: dict,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # SHADOW ONLY - writable=True: editing a colleague is a write against
    # the directory, so READ_ONLY would block it.
    _shadow(db, current_user, "team.directory.manage",
            route="PUT /employees/{emp_id}", writable=True)
    emp = db.query(models.User).filter(
        models.User.id == emp_id,
        models.User.organization_id == current_user.organization_id,
    ).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    is_super = current_user.role == "super_admin"
    is_admin = current_user.role in ("admin", "hr", "owner", "super_admin")
    # role is an admin-only field; non-admins cannot touch it.
    ALLOWED = {"name", "designation", "phone", "employment_type", "team", "department", "is_active", "employee_id"}
    if is_admin:
        ALLOWED.add("role")
    for key, value in emp_data.items():
        if key not in ALLOWED:
            continue
        if key == "role":
            new_role = str(value).lower() if value else value
            # Only super_admin may assign super_admin or owner.
            if not is_super and new_role in ("super_admin", "owner"):
                raise HTTPException(status_code=403, detail=f"Only super_admin may assign the '{new_role}' role")
            setattr(emp, key, new_role)
        else:
            setattr(emp, key, value)

    # Start-date backfill — accepted under either `start_date` or
    # `date_of_joining`. Stored as a midnight DateTime so it compares
    # cleanly against the eligibility check. A null / empty value clears
    # the field (returns the worker to "grandfathered" eligibility).
    sd_raw = emp_data.get("start_date", emp_data.get("date_of_joining"))
    if "start_date" in emp_data or "date_of_joining" in emp_data:
        if not sd_raw:
            emp.official_start_date = None
        else:
            try:
                parsed = sd_raw if isinstance(sd_raw, datetime) else datetime.strptime(str(sd_raw)[:10], "%Y-%m-%d")
                emp.official_start_date = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid start_date — expected YYYY-MM-DD")

    db.commit()
    db.refresh(emp)
    return {"message": "Employee updated successfully"}


# ===========================
# ADMIN: CLOCK OVERRIDE
# Allows admin/HR to create or update a clock session for any employee.
# ===========================
class AdminClockOverride(BaseModel):
    date: str                             # YYYY-MM-DD (IST calendar day)
    clock_in_time: Optional[str] = None   # HH:MM (IST)
    clock_out_time: Optional[str] = None  # HH:MM (IST), optional


@router.post("/{emp_id}/admin-clock")
def admin_clock_override(
    emp_id: int,
    data: AdminClockOverride,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    # Per product decision (2026-05-13): clock-in time changes are admin-only,
    # including one-off date overrides. HR sees the schedule but can't edit it.
    if (getattr(admin, "role", "") or "").lower() != "admin":
        raise HTTPException(403, "Only admin can override clock-in time")

    emp = db.query(models.User).filter(
        models.User.id == emp_id,
        models.User.organization_id == admin.organization_id,
    ).first()
    if not emp:
        raise HTTPException(404, "Employee not found")

    # The admin types a wall-clock time; it means that time on THIS
    # organization's clock, not on a fixed +05:30.
    _ORG_TZ = _tz.org_timezone(db, admin.organization_id)

    def _to_utc(date_str: str, time_str: str) -> datetime:
        dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
        return dt.replace(tzinfo=_ORG_TZ).astimezone(timezone.utc)

    try:
        target_date = datetime.strptime(data.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")

    day_start, _ = _tz.local_day_bounds(db, admin.organization_id, target_date)
    day_end   = day_start + timedelta(days=1)

    session = (
        db.query(models.ClockSession)
        .filter(
            models.ClockSession.organization_id == admin.organization_id,
            models.ClockSession.user_id         == emp_id,
            models.ClockSession.clock_in_time   >= day_start,
            models.ClockSession.clock_in_time   <  day_end,
        )
        .order_by(models.ClockSession.id.desc())
        .first()
    )

    clock_in_utc  = _to_utc(data.date, data.clock_in_time)  if data.clock_in_time  else None
    clock_out_utc = _to_utc(data.date, data.clock_out_time) if data.clock_out_time else None

    # §45.D-15: record which boundaries this adjustment ACTUALLY moves, so an
    # untouched boundary gets no event. Collected here and appended after the
    # session row exists, because a new session has no id until it is flushed.
    _boundary_writes: list = []

    if session:
        if clock_in_utc:
            session.clock_in_time = clock_in_utc
            _boundary_writes.append((BOUNDARY_CLOCK_IN, clock_in_utc))
        if clock_out_utc:
            session.clock_out_time = clock_out_utc
            _boundary_writes.append((BOUNDARY_CLOCK_OUT, clock_out_utc))
        if session.clock_in_time and session.clock_out_time:
            raw = int((session.clock_out_time - session.clock_in_time).total_seconds())
            session.total_seconds = max(0, raw - (session.break_seconds or 0))
    else:
        if not clock_in_utc:
            raise HTTPException(400, "clock_in_time is required to create a new session")
        total = int((clock_out_utc - clock_in_utc).total_seconds()) if clock_out_utc else 0
        session = models.ClockSession(
            user_id         = emp_id,
            organization_id = emp.organization_id,
            clock_in_time   = clock_in_utc,
            clock_out_time  = clock_out_utc,
            total_seconds   = total,
            break_seconds   = 0,
            is_active       = clock_out_utc is None,
        )
        db.add(session)
        # A boundary event references the session row, so the row must exist
        # first. A flush is not a commit — both still land in one transaction.
        db.flush()
        _boundary_writes.append((BOUNDARY_CLOCK_IN, clock_in_utc))
        if clock_out_utc:
            _boundary_writes.append((BOUNDARY_CLOCK_OUT, clock_out_utc))

    # §45.D-15: an administrative correction is ADMIN_OVERRIDE on whichever
    # boundary it moved. Any earlier event — a genuine WORKER clock-out, say —
    # is left exactly as it was; this APPENDS a later one, so the history shows
    # both what the Worker did and what the admin changed it to. Attributing an
    # admin-authored timestamp to the Worker is the failure §45.D-15 names.
    _adjust_actor = actor_label_for_user(admin)
    for _boundary, _boundary_at in _boundary_writes:
        append_boundary_event(
            db,
            session_id  = session.id,
            user_id     = emp_id,
            boundary    = _boundary,
            boundary_at = _boundary_at,
            provenance  = _Provenance.ADMIN_OVERRIDE,
            actor_label = _adjust_actor,
        )

    db.commit()
    _CACHE.clear()  # invalidate dashboard cache

    return {
        "success":        True,
        "message":        "Clock session updated",
        "clock_in_time":  session.clock_in_time.isoformat()  if session.clock_in_time  else None,
        "clock_out_time": session.clock_out_time.isoformat() if session.clock_out_time else None,
        "total_seconds":  session.total_seconds,
    }


# ===========================
# TIME ADJUSTMENTS — admin-credited compensation for lost work time
# (power cuts, tracker crashes, OS reboots that killed an active session,
# network outages that fired the OFFLINE_CLOCKOUT_MS auto-close, etc.).
#
# The model is purely additive:
#   • POST creates a row in `time_adjustments` with +N seconds and a
#     mandatory note. ClockSession rows stay untouched so the original
#     tracker record remains immutable.
#   • Reports (daily/weekly/payroll-adjacent) sum (ClockSession.total_seconds
#     + TimeAdjustment.seconds_added) when computing worked hours. See
#     _adjustment_seconds_for_day() / _adjustment_seconds_by_user() helpers.
#   • DELETE is a soft-revert: is_active=False, reverted_by/reverted_at
#     recorded. Both add and revert email the employee so silent shrinking
#     of someone's hours isn't possible.
#
# Permission: admin-only (workspace owner), per the 2026-05-13 clock-in
# governance decision. HR cannot write to this table.
# ===========================
_TIME_ADJ_MAX_HOURS_PER_ENTRY = 12       # sanity cap — single power cut, not "fix-the-month"
# Backdating window moved to `payroll.time_adjustment_max_past_days`
# (app/payroll/policy.py). The constant survives only as the documented
# system default, which that setting ships with.
_TIME_ADJ_RECENT_LIMIT        = 200      # cap on the all-employees list page


def _require_admin_owner(user: models.User) -> None:
    """Tight permission check for time-adjustment writes.

    Mirrors `admin_clock_override` rather than the looser `_require_admin`
    in workspace/router.py — HR is intentionally NOT included, per the
    2026-05-13 governance decision that clock-in / time-edit changes are
    admin-only. If we ever loosen this, also revisit `admin_clock_override`
    and document the policy shift in the clock-in-governance memory.
    """
    if (getattr(user, "role", "") or "").lower() != "admin":
        raise HTTPException(403, "Only admin can adjust recorded time")


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h and m: return f"{h}h {m}m"
    if h:       return f"{h}h"
    return f"{m}m"


class _TimeAdjustmentCreateBody(BaseModel):
    # IST calendar date the credited time belongs to.
    date:    date_type
    # Either provide (hours,minutes) or seconds — server normalises to seconds.
    hours:   int = 0
    minutes: int = 0
    seconds: Optional[int] = None
    reason:  str


def _adjustment_to_dict(row: models.TimeAdjustment, creator: Optional[models.User] = None) -> dict:
    return {
        "id":              row.id,
        "user_id":         row.user_id,
        "date":            row.adjustment_date.isoformat() if row.adjustment_date else None,
        "seconds_added":   int(row.seconds_added or 0),
        "duration":        _format_duration(row.seconds_added),
        "reason":          row.reason,
        "created_by_id":   row.created_by,
        "created_by_name": (creator.name if creator else None),
        "created_at":      iso_z(row.created_at) if row.created_at else None,
        "is_active":       bool(row.is_active),
        "reverted_at":     iso_z(row.reverted_at) if row.reverted_at else None,
    }


@router.post("/{emp_id}/time-adjustments")
def create_time_adjustment(
    emp_id: int,
    body: _TimeAdjustmentCreateBody,
    background_tasks: BackgroundTasks,
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    _require_admin_owner(admin)

    emp = db.query(models.User).filter(
        models.User.id == emp_id,
        models.User.organization_id == admin.organization_id,
    ).first()
    if not emp:
        raise HTTPException(404, "Employee not found")

    # Normalise (hours,minutes) → seconds, prefer explicit `seconds` if sent.
    if body.seconds is not None:
        secs = int(body.seconds)
    else:
        secs = int(body.hours) * 3600 + int(body.minutes) * 60
    if secs <= 0:
        raise HTTPException(400, "Duration must be greater than zero")
    if secs > _TIME_ADJ_MAX_HOURS_PER_ENTRY * 3600:
        raise HTTPException(
            400,
            f"Single adjustment capped at {_TIME_ADJ_MAX_HOURS_PER_ENTRY}h — "
            "split a longer correction into multiple entries.",
        )

    today_ist = _today_dashboard(db, admin.organization_id)
    if body.date > today_ist:
        raise HTTPException(400, "Cannot adjust a future date")
    # The backdating window is this organization's payroll-correction policy,
    # not a platform limit: an org on a quarterly cycle could not credit an
    # employee for a power cut two months earlier.
    from app.payroll.policy import time_adjustment_max_past_days
    _max_past = time_adjustment_max_past_days(db, admin)
    if (today_ist - body.date).days > _max_past:
        raise HTTPException(
            400,
            f"Cannot backdate further than {_max_past} days",
        )

    reason = (body.reason or "").strip()
    if len(reason) < 4:
        raise HTTPException(400, "Reason is required and must be at least 4 characters")
    if len(reason) > 500:
        reason = reason[:500]

    row = models.TimeAdjustment(
        user_id         = emp.id,
        adjustment_date = body.date,
        seconds_added   = secs,
        reason          = reason,
        created_by      = admin.id,
        is_active       = True,
    )
    db.add(row)
    try:
        db.add(models.AuditLog(
            actor_id  = admin.id,
            target_id = emp.id,
            entity    = "TimeAdjustment",
            action    = "TimeAdjustmentCreated",
            payload   = {
                "date":          body.date.isoformat(),
                "seconds_added": secs,
                "reason":        reason,
            },
        ))
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        raise HTTPException(503, "Could not save adjustment — please retry")

    # Invalidate the dashboard team cache so admin overview reflects the new
    # totals on its next poll. Same pattern as admin_clock_override.
    #
    # MUST go through the helper. `_TEAM_CACHE` is keyed by org_id with dict
    # values, so `_TEAM_CACHE["data"] = None` did not invalidate anything — it
    # inserted a None under the literal key "data", after which
    # _team_cache_invalidate_all() raised TypeError on that entry and 500'd
    # every subsequent clock-in and clock-out.
    _CACHE.clear()
    _team_cache_invalidate_all()

    # Real-time push so the affected employee's "Today" dashboard updates the
    # worked-hours total immediately, without waiting for its 60s poll cycle.
    #
    # Scoped to THIS EMPLOYEE'S ORGANIZATION. It used the global
    # `broadcast_sync` and relied on the frontend gating on `user_id` — but
    # client-side filtering is not tenant isolation: the payload (employee id,
    # the admin's free-text reason, the crediting admin's name) had already
    # been delivered to every other tenant's browsers.
    try:
        from websocket_manager import manager
        import json
        manager.broadcast_to_org_sync(emp.organization_id, json.dumps({
            "type":             "time_adjustment_updated",
            "organization_id":  emp.organization_id,
            "user_id":          emp.id,
            "adjustment_id":    row.id,
            "date":             body.date.isoformat(),
            "seconds_added":    secs,
            "duration":         _format_duration(secs),
            "reason":           reason,
            "added_by":         admin.name or "Admin",
            "action":           "added",
            "timestamp":        iso_z(datetime.now(timezone.utc)),
        }))
    except Exception:
        pass

    # Informational email — best-effort, never blocks the response.
    if emp.email:
        background_tasks.add_task(
            send_time_adjustment_email,
            to_email        = emp.email,
            employee_name   = emp.name or (emp.email.split("@")[0] if emp.email else "there"),
            adjustment_date = body.date.isoformat(),
            duration_label  = _format_duration(secs),
            reason          = reason,
            admin_name      = admin.name or "Admin",
        )

    return _adjustment_to_dict(row, creator=admin)


@router.get("/time-adjustments/recent")
def list_recent_time_adjustments(
    days:   int = Query(7,  ge=1, le=90),
    limit:  int = Query(100, ge=1, le=_TIME_ADJ_RECENT_LIMIT),
    db:     Session     = Depends(get_db),
    admin:  models.User = Depends(get_current_user),
):
    """Admin tab: the most recent N adjustments across the workspace.

    Includes both active and reverted rows so the audit story is complete.
    """
    _require_admin_owner(admin)
    cutoff = _today_dashboard(db, admin.organization_id) - timedelta(days=days)
    org_user_ids = (
        db.query(models.User.id)
          .filter(models.User.organization_id == admin.organization_id)
          .scalar_subquery()
    )
    rows = (
        db.query(models.TimeAdjustment)
        .filter(
            models.TimeAdjustment.adjustment_date >= cutoff,
            models.TimeAdjustment.user_id.in_(org_user_ids),
        )
        .order_by(models.TimeAdjustment.created_at.desc())
        .limit(limit)
        .all()
    )
    # Bulk-load users we'll need (employee + admin who created it) so we
    # don't N+1 the dashboard.
    user_ids = set()
    for r in rows:
        user_ids.add(r.user_id)
        if r.created_by: user_ids.add(r.created_by)
    user_map = {
        u.id: u for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()
    } if user_ids else {}

    out = []
    for r in rows:
        emp = user_map.get(r.user_id)
        creator = user_map.get(r.created_by) if r.created_by else None
        d = _adjustment_to_dict(r, creator=creator)
        d["employee_name"]       = emp.name  if emp else None
        d["employee_email"]      = emp.email if emp else None
        d["employee_department"] = emp.department if emp else None
        out.append(d)
    return {"items": out, "count": len(out)}


@router.get("/{emp_id}/time-adjustments")
def list_time_adjustments_for_employee(
    emp_id: int,
    days:   int = Query(60, ge=1, le=365),
    db:     Session     = Depends(get_db),
    admin:  models.User = Depends(get_current_user),
):
    _require_admin_owner(admin)
    emp = db.query(models.User).filter(
        models.User.id == emp_id,
        models.User.organization_id == admin.organization_id,
    ).first()
    if not emp:
        raise HTTPException(404, "Employee not found")

    cutoff = _today_dashboard(db, admin.organization_id) - timedelta(days=days)
    rows = (
        db.query(models.TimeAdjustment)
        .filter(
            and_(
                models.TimeAdjustment.user_id == emp_id,
                models.TimeAdjustment.adjustment_date >= cutoff,
            )
        )
        .order_by(models.TimeAdjustment.adjustment_date.desc(),
                  models.TimeAdjustment.created_at.desc())
        .all()
    )
    creator_ids = {r.created_by for r in rows if r.created_by}
    creator_map = {
        u.id: u for u in db.query(models.User).filter(models.User.id.in_(creator_ids)).all()
    } if creator_ids else {}
    total_active_secs = sum(int(r.seconds_added or 0) for r in rows if r.is_active)
    return {
        "items": [_adjustment_to_dict(r, creator=creator_map.get(r.created_by)) for r in rows],
        "totals": {
            "active_seconds": total_active_secs,
            "active_label":   _format_duration(total_active_secs),
        },
    }


@router.delete("/time-adjustments/{adj_id}")
def revert_time_adjustment(
    adj_id: int,
    background_tasks: BackgroundTasks,
    db:     Session     = Depends(get_db),
    admin:  models.User = Depends(get_current_user),
):
    _require_admin_owner(admin)
    row = db.query(models.TimeAdjustment).filter(models.TimeAdjustment.id == adj_id).first()
    if not row:
        raise HTTPException(404, "Adjustment not found")
    _target_org = db.query(models.User.organization_id).filter(models.User.id == row.user_id).scalar()
    if _target_org != admin.organization_id:
        raise HTTPException(404, "Adjustment not found")
    if not row.is_active:
        return _adjustment_to_dict(row)  # already reverted — idempotent

    row.is_active   = False
    row.reverted_by = admin.id
    row.reverted_at = datetime.now(timezone.utc)
    emp = db.query(models.User).filter(models.User.id == row.user_id).first()
    try:
        db.add(models.AuditLog(
            actor_id  = admin.id,
            target_id = row.user_id,
            entity    = "TimeAdjustment",
            action    = "TimeAdjustmentReverted",
            payload   = {
                "adjustment_id": row.id,
                "date":          row.adjustment_date.isoformat() if row.adjustment_date else None,
                "seconds_added": int(row.seconds_added or 0),
            },
        ))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, "Could not revert adjustment — please retry")

    _CACHE.clear()
    _team_cache_invalidate_all()   # see the note in the "added" path above

    # Live push so the employee sees the revert reflected in their worked
    # total without a refresh. Same shape as the "added" broadcast above, and
    # org-scoped for the same reason: the org check above guards the WRITE, and
    # an unscoped push would hand the record to every other tenant anyway.
    try:
        from websocket_manager import manager
        import json
        manager.broadcast_to_org_sync(admin.organization_id, json.dumps({
            "type":          "time_adjustment_updated",
            "organization_id": admin.organization_id,
            "user_id":       row.user_id,
            "adjustment_id": row.id,
            "date":          row.adjustment_date.isoformat() if row.adjustment_date else None,
            "seconds_added": int(row.seconds_added or 0),
            "duration":      _format_duration(row.seconds_added),
            "reason":        row.reason or "",
            "added_by":      admin.name or "Admin",
            "action":        "reverted",
            "timestamp":     iso_z(datetime.now(timezone.utc)),
        }))
    except Exception:
        pass

    if emp and emp.email:
        background_tasks.add_task(
            send_time_adjustment_reverted_email,
            to_email        = emp.email,
            employee_name   = emp.name or (emp.email.split("@")[0] if emp.email else "there"),
            adjustment_date = row.adjustment_date.isoformat() if row.adjustment_date else "",
            duration_label  = _format_duration(row.seconds_added),
            reason          = row.reason or "",
            admin_name      = admin.name or "Admin",
        )

    return _adjustment_to_dict(row)


# ===========================
# DELETE EMPLOYEE
# ===========================
#: Tables whose Worker foreign key is ``ON DELETE RESTRICT`` under Owner
#: Ruling M — the immutable Worker-history retention boundary. A row in ANY of
#: them makes the Worker undeletable, by the database and now also by this
#: endpoint.
#:
#: ``(table, column)`` pairs, NOT a bare table list: the Worker column is not
#: called the same thing everywhere. ``config_value`` keys on
#: ``scope_user_id`` — a Worker-scoped configuration override inside an
#: immutable, content-hashed ``ConfigVersion`` — and assuming ``user_id`` there
#: would probe a column that does not exist and mask the protection entirely.
#:
#: Named as strings and probed with a bare EXISTS rather than imported as
#: models: these are Phase 2 foundation tables that may not exist yet in a
#: given database, and importing them here would wire the ECO packages into an
#: application route, which their own not-wired guards forbid.
_PROTECTED_HISTORY_TABLES = (
    ("work_day_ledger", "user_id"),
    ("eco_day_record", "user_id"),
    ("leave_input_snapshot", "user_id"),
    ("leave_interval_set", "user_id"),
    ("worker_context_timeline", "user_id"),
    ("config_value", "scope_user_id"),
    # §45.D-15 (owner ruling D15-4-A): session-boundary provenance is protected
    # historical evidence. Probed on `user_id` — the SUBJECT Worker, which is
    # why the column is denormalised onto the event: this probe is a direct,
    # join-free query and cannot reach a Worker through `session_id`.
    #
    # The RESTRICT foreign keys on the table are the database-level backstop;
    # this entry is what turns the refusal into a clean 409 instead of an
    # IntegrityError surfacing as a 500.
    ("clock_session_boundary_event", "user_id"),
)


def _protected_history(db: Session, user_id: int) -> list:
    """Which history tables hold rows for this Worker. Empty means deletable.

    A missing table is not a violation — the Phase 2 migrations are unapplied
    in some databases — so each is skipped when absent rather than raising.
    Returns table names, which is what the endpoint reports to the caller.
    """
    try:
        # Reflect on the SESSION's own connection, not on the engine.
        # ``inspect(engine).get_table_names()`` checks out a separate
        # Connection and rolls it back on exit — which on a shared DBAPI
        # connection discards the caller's own pending work, so the probes
        # below then found nothing and this function reported "deletable" for a
        # Worker who was not. Using the session's connection keeps one
        # transaction and sees what the caller has flushed.
        present = set(sa_inspect(db.connection()).get_table_names())
    except Exception:
        # Fail CLOSED. If the catalogue cannot be read we do not know whether
        # history exists, and "unknown" must never become "delete it".
        return [table for table, _column in _PROTECTED_HISTORY_TABLES]

    found = []
    for table, column in _PROTECTED_HISTORY_TABLES:
        if table not in present:
            continue
        row = db.execute(
            text(f"SELECT 1 FROM {table} WHERE {column} = :uid LIMIT 1"),
            {"uid": user_id},
        ).first()
        if row is not None:
            found.append(table)
    return found


@router.delete("/{emp_id}")
def delete_employee(
    emp_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin/HR only")
    # Efficient id-only existence check (avoids loading the full ORM graph
    # which triggers cascade SELECTs) + org scoping for multitenancy.
    exists = db.query(models.User.id).filter(
        models.User.id == emp_id,
        models.User.organization_id == current_user.organization_id,
    ).first()
    if not exists:
        raise HTTPException(status_code=404, detail="Employee not found")

    # ── Historical retention (Owner Ruling M) ────────────────────────────────
    # Five tables now hold user_id ON DELETE RESTRICT, so the database refuses
    # this delete outright for a Worker who has evaluation history. Checked
    # here FIRST so the caller gets a stated reason instead of an opaque 500
    # from the IntegrityError, and so the leave_requests pre-delete below —
    # which leave_interval_set also RESTRICTs — is never even attempted.
    #
    # This is a refusal, not a cleanup path: the rows are immutable historical
    # evidence and this endpoint must not erase or rewrite them.
    blocking = _protected_history(db, emp_id)
    if blocking:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "EMPLOYEE_HAS_PROTECTED_HISTORY",
                "message": (
                    "This employee has historical work or evaluation records "
                    "that must be retained and cannot be deleted."
                ),
                "tables": blocking,
            },
        )

    # Bulk-delete the high-volume child tables with a single statement each
    # (synchronize_session=False => no row loading). break_sessions and
    # activities reference clock_sessions, so clear them before sessions.
    # leave_requests has no DB-level ON DELETE CASCADE, so it must be cleared
    # explicitly. Every other child table (live_status, browser_activities,
    # payroll, shifts, user_sessions, …) has ON DELETE CASCADE / SET NULL at
    # the DB level and is handled atomically when the users row is deleted.
    for model in (
        models.BreakSession,
        models.Activity,
        models.ClockSession,
        models.LeaveRequest,
    ):
        db.query(model).filter(model.user_id == emp_id).delete(synchronize_session=False)

    db.query(models.User).filter(models.User.id == emp_id).delete(synchronize_session=False)
    db.commit()
    return {"message": "Employee deleted successfully"}


# ===========================
# ADMIN: close stale breaks
# ===========================
@router.post("/admin/cleanup-stale-breaks")
def cleanup_stale_breaks(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin/HR only")
    org_user_ids = [
        u.id for u in db.query(models.User.id)
        .filter(models.User.organization_id == current_user.organization_id)
        .all()
    ]
    stale = db.query(models.BreakSession).filter(
        models.BreakSession.user_id.in_(org_user_ids),
        models.BreakSession.break_end == None,
    ).all()

    fixed = 0
    for b in stale:
        clock_session = db.query(models.ClockSession).filter(
            and_(
                models.ClockSession.user_id       == b.user_id,
                models.ClockSession.clock_in_time <= b.break_start,
            )
        ).order_by(models.ClockSession.id.desc()).first()

        if clock_session and clock_session.clock_out_time is not None:
            b.break_end     = clock_session.clock_out_time
            b.break_seconds = max(0, int((b.break_end - b.break_start).total_seconds()))
            fixed += 1

    db.commit()
    return {"fixed": fixed, "message": f"Closed {fixed} stale break session(s)"}


# ===========================
# CLOCK IN
# ===========================
@router.post("/clock-in")
def clock_in(
    db:      Session     = Depends(get_db),
    user:    models.User = Depends(get_current_user),
    request: Request     = None,
):
    # Attendance actions are desktop-app only, for every role and every tenant
    # (see WEB_ATTENDANCE_DISABLED in app/attendance_desktop_policy.py). Runs
    # FIRST so a blocked request never touches the session tables.
    enforce_desktop_attendance(user, request, db)

    # Desktop-only clock-in (see app/attendance_desktop_policy.py). No-op for
    # every org/role outside that policy; raises 403 DESKTOP_APP_REQUIRED for a
    # covered employee calling from a browser. Kept as the per-tenant lever;
    # while the global rule above is on, it can only ever agree with it.
    enforce_desktop_clock_in(user, request, db)

    # ── Early clock-in guard (P10) ───────────────────────────────────────────
    # Active only when this employee's effective `schedule.allow_early_clock_in`
    # is false AND a shift start resolves. Both default to "no guard", so the
    # gate is inert until an organization opts in.
    #
    # Returns a structured HTTP 200 rather than a 4xx, matching the Early Clock
    # Out gate: existing clients (desktop tracker, web dashboard) treat a
    # non-200 as a transport error and would retry or show a raw failure, while
    # a 200 body lets them show the real reason. Fail-open on any policy error —
    # a schedule problem must never stop someone starting work.
    try:
        from app.schedule_policy.resolver import resolve_work_schedule
        _now_ci = _tz.local_now(db, user.organization_id)
        _sched_ci = resolve_work_schedule(db, user, _now_ci.date())
        if (not _sched_ci.allow_early_clock_in) and _sched_ci.start_local:
            _start_ci = _now_ci.replace(
                hour=_sched_ci.start_local.hour,
                minute=_sched_ci.start_local.minute,
                second=0, microsecond=0,
            )
            if _now_ci < _start_ci:
                return {
                    "success": False,
                    "code": "EARLY_CLOCK_IN_NOT_ALLOWED",
                    "message": "Your shift has not started yet.",
                    "shift_start_time": _sched_ci.start_local.strftime("%H:%M"),
                    "minutes_until_start": int(
                        (_start_ci - _now_ci).total_seconds() // 60),
                }
    except Exception:
        _log_att.warning("early clock-in guard failed user=%s; allowing clock-in",
                         getattr(user, "id", None), exc_info=True)

    try:
        # with_for_update() locks the rows so concurrent retries can't both
        # pass the "no active session" check before either has committed.
        active = db.query(models.ClockSession).filter(
            and_(
                models.ClockSession.user_id        == user.id,
                models.ClockSession.clock_out_time == None,
            )
        ).with_for_update().first()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    if active:
        return {"success": False, "message": "Already clocked in"}

    # §45.D-15: bound to a variable so the boundary event records the exact
    # instant written to the row, not a second reading of the clock.
    _clock_in_at = datetime.now(timezone.utc)
    session = models.ClockSession(
        user_id         = user.id,
        organization_id = user.organization_id,
        clock_in_time   = _clock_in_at,
        total_seconds   = 0,
        break_seconds   = 0,
    )
    try:
        db.add(session)
        # Flush so the session has an id the event can reference, then record
        # the origin of this open boundary in the SAME transaction. A Worker
        # opening their own session is WORKER on the clock_in boundary.
        db.flush()
        append_boundary_event(
            db,
            session_id  = session.id,
            user_id     = user.id,
            boundary    = BOUNDARY_CLOCK_IN,
            boundary_at = _clock_in_at,
            provenance  = _Provenance.WORKER,
            actor_label = actor_label_for_user(user),
        )
        db.commit()
        db.refresh(session)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    # Invalidate status cache so next /tracking/status poll gets fresh data
    try:
        from app.tracking.router import _cache_invalidate
        _cache_invalidate(user.id)
    except Exception:
        pass

    # Invalidate team cache too — without this, an Absent worker who clocks in
    # would keep showing as Absent on the admin dashboard for up to 8 seconds
    # (the _TEAM_CACHE TTL) before flipping to Working. Clock-out / start-break
    # / resume-work all do this same invalidation; clock-in was missing it.
    _team_cache_invalidate_all()

    # Broadcast clock-in event via WebSocket — org-scoped, not global
    try:
        from websocket_manager import manager
        import json
        if user.organization_id:
            manager.broadcast_to_org_sync(user.organization_id, json.dumps({
                "type":            "clock_in",
                "organization_id": user.organization_id,
                "user_id":         user.id,
                "user_name":       user.name,
                "clock_in_time":   iso_z(session.clock_in_time),
                "timestamp":       iso_z(session.clock_in_time),
            }))
    except Exception:
        pass

    return {
        "success":       True,
        "user_id":       user.id,
        "clock_in_time": iso_z(session.clock_in_time),
    }


# ===========================
# CLOCK OUT
# ===========================
# ── Controlled Clock-Out (Phase 2) — required-hours reason gate ───────────────
# TEMPORARILY DISABLED: the Early Clock Out feature is currently inactive.
# The implementation below is intentionally KEPT so it can be restored later.
# While this is False, clock_out() never returns EARLY_CLOCK_OUT_REASON_REQUIRED,
# never asks for a reason, never writes the EarlyClockOut audit row and never
# sends the Early Clock Out email — the worker just clocks out normally.
# Nothing else changes: required-hours resolvers, worked-time calculation, break
# exclusion, the attendance policy and the `early_clockout_enabled` org setting
# are all untouched. To re-enable the feature, set this back to True.
EARLY_CLOCK_OUT_FEATURE_ENABLED = False

# Typed reason constraints (also enforced client-side; server is authoritative).
_EARLY_REASON_MIN = 15
_EARLY_REASON_MAX = 1000


class ClockOutBody(BaseModel):
    # All optional so the existing no-body callers (web dashboard, desktop
    # tracker) keep working unchanged. Populated only on the Early Clock Out
    # re-submit.
    reason: Optional[str] = None
    attendance_id: Optional[int] = None   # session reference echoed back by the client


def _fmt_hm(secs: Optional[int]) -> str:
    secs = int(secs or 0)
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def _org_settings_row(db: Session, org_id):
    try:
        return db.query(models.OrgSettings).filter(
            models.OrgSettings.organization_id == org_id,
        ).first()
    except Exception:
        return None


# _early_clockout_recipient was the ONLY sender that resolved a tenant inbox,
# and it still ended in a hardcoded "info@temveris.example". Recipient resolution
# now lives in app/notifications/recipients.py for every alert, with no global
# fallback address at all.


def _org_name(db: Session, org_id) -> str:
    row = _org_settings_row(db, org_id)
    if row and getattr(row, "company_name", None):
        return row.company_name
    return ""


@router.post("/clock-out")
def clock_out(
    background_tasks: BackgroundTasks,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
    body: Optional[ClockOutBody] = None,
    request: Request = None,
):
    # Attendance actions are desktop-app only (see WEB_ATTENDANCE_DISABLED in
    # app/attendance_desktop_policy.py). `request` was added purely so this
    # endpoint can see its caller — every existing keyword call site and the
    # no-body clients are unaffected. Runs FIRST: a blocked request must not
    # auto-close sessions or touch break state on its way to a 403.
    enforce_desktop_attendance(user, request, db)

    # Auto-close any stale sessions before processing this request.
    # This prevents a session from yesterday being counted as today's work
    # if auto-close somehow didn't fire while the app was closed overnight.
    try:
        _auto_close_stale_sessions(db, org_id=user.organization_id)
    except Exception:
        # MUST rollback, not just swallow. A failed statement leaves psycopg2's
        # transaction in an aborted state, and every later query in this request
        # then raises InFailedSqlTransaction — including the UNGUARDED break /
        # offline recalculation below, which turns a peripheral failure into a
        # 500 and leaves the worker unable to clock out. See the rollbacks in
        # each fail-open handler below for the same reason.
        db.rollback()

    # ── Active-break guard ───────────────────────────────────────────────────
    # Refuse clock-out while the worker has an active break. This protects
    # against the desktop tracker firing /employees/clock-out from
    # handleSuspend / handleShutdown / network-loss paths when the laptop
    # sleeps mid-break — that path was silently truncating sessions and
    # making the admin dashboard show "Stopped Working" the moment the break
    # started. Breaks are intentional silence; the worker has to Resume first
    # before they can clock out. This is also the correct UX rule.
    #
    # Filter matches the canonical "active break" query used in
    # breaks/router.py and _auto_close_abandoned_sessions: both break_end
    # IS NULL *and* is_active = True. Zombie rows (break_end IS NULL,
    # is_active = False) do NOT block clock-out — they're stale state that
    # never resolved.
    try:
        active_break = db.query(models.BreakSession).filter(
            and_(
                models.BreakSession.user_id   == user.id,
                models.BreakSession.break_end.is_(None),
                models.BreakSession.is_active == True,
            )
        ).first()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")
    if active_break:
        # 409 (conflict) rather than 400 — this is a state precondition issue,
        # not a bad request. The desktop tracker can swallow this silently
        # (the session stays open, which is what we want) and a human-driven
        # client can show a meaningful toast.
        raise HTTPException(
            status_code=409,
            detail="Cannot clock out while on break. Resume from break first.",
        )

    try:
        session = db.query(models.ClockSession).filter(
            and_(
                models.ClockSession.user_id        == user.id,
                models.ClockSession.clock_out_time == None,
            )
        ).first()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    if not session:
        return {"success": False, "message": "No active session"}

    # ── Controlled Clock-Out gate (Phase 2) ─────────────────────────────────
    # On a non-flexible working day, an employee may only clock out once they
    # have met their required worked duration (breaks excluded). If they have
    # not, we DO NOT close the session — we return a structured (HTTP-200)
    # response asking the client to collect a typed reason; the same endpoint
    # is then called again with that reason. Flexible days (weekend / org-
    # configured / holiday) skip this entirely. Reused Phase-1 resolvers and
    # the existing risk-engine worked-time calc — no new worked-time logic.
    # Fail-open on any policy error: never block a clock-out because of it.
    early_ctx = None  # set only when an early clock-out is actually being recorded
    try:
        _today_ist = _tz.local_date(db, user.organization_id)
        # The gate's own kill switch. `early_clockout_enabled` has existed as a
        # registered setting since Phase 1 while the gate enforced regardless of
        # it — an organization that switched it off still had its people blocked.
        from app.attendance_policy import policy_bool, SETTING_EARLY_CLOCKOUT_ENABLED
        # TEMPORARILY DISABLED:
        # The Early Clock Out feature is currently inactive product-wide.
        # Keep everything below for future re-enable — flip
        # EARLY_CLOCK_OUT_FEATURE_ENABLED back to True to restore it.
        # Treating it as a "flexible" day is the gate's existing skip path, so
        # the normal clock-out flow below runs completely untouched.
        if not EARLY_CLOCK_OUT_FEATURE_ENABLED:
            _flexible = True
        elif not policy_bool(db, user, SETTING_EARLY_CLOCKOUT_ENABLED):
            _flexible = True
        else:
            _flexible = is_flexible_clock_out_day(db, user.organization_id, _today_ist)
    except Exception:
        _flexible = True
        db.rollback()   # clear an aborted transaction — see the note above
    if not _flexible:
        required_secs = worked_secs = None
        break_secs_today = offline_secs_today = 0
        try:
            required_secs = resolve_expected_work_seconds(db, user, _today_ist)
            from app.risk.router import _session_worked_secs  # existing calc — reused
            # Compare against the DAY's total worked time (all of today's
            # sessions), not just the current one — this matches the
            # "Total Worked Hours" the employee sees. Completed sessions'
            # stored total_seconds already exclude breaks; add the current
            # open session's live worked time.
            _day_start, _ = _tz.local_day_bounds(db, user.organization_id, _today_ist)
            _completed_today = db.query(func.sum(models.ClockSession.total_seconds)).filter(
                and_(
                    models.ClockSession.user_id == user.id,
                    models.ClockSession.clock_in_time >= _day_start,
                    models.ClockSession.clock_out_time.isnot(None),
                )
            ).scalar() or 0
            worked_secs = int(_completed_today) + _session_worked_secs(session)
        except Exception:
            required_secs = worked_secs = None
            db.rollback()   # clear an aborted transaction — see the note above
        # Deduction breakdown for the prompt. Computed in its OWN try block: it
        # is presentational, so a failure here must never null out required/worked
        # and silently switch enforcement off. Worst case the modal omits the
        # "why" line; the gate still does its job.
        if required_secs is not None and worked_secs is not None:
            try:
                _day_start_bd, _ = _tz.local_day_bounds(
                    db, user.organization_id, _today_ist)
                _bounds_today = {}
                for _s in db.query(models.ClockSession).filter(
                    and_(
                        models.ClockSession.user_id == user.id,
                        models.ClockSession.clock_in_time >= _day_start_bd,
                    )
                ).all():
                    _s_start = _utc_aware(_s.clock_in_time)
                    _s_end = _utc_aware(_s.clock_out_time) or datetime.now(timezone.utc)
                    if _s_end > _s_start:
                        _bounds_today[_s.id] = (_s_start, _s_end)
                break_secs_today   = sum(_actual_break_seconds_for_sessions(db, _bounds_today).values())
                offline_secs_today = sum(_actual_offline_seconds_for_sessions(db, _bounds_today).values())
            except Exception:
                break_secs_today = offline_secs_today = 0
                db.rollback()   # clear an aborted transaction — see the note above
        if required_secs is not None and worked_secs is not None and worked_secs < required_secs:
            reason_text = ((body.reason if body else None) or "").strip()
            structured = {
                "success": False,
                "code": "EARLY_CLOCK_OUT_REASON_REQUIRED",
                "attendance_id": session.id,
                "worked_seconds": worked_secs,
                "required_seconds": required_secs,
                "difference_seconds": required_secs - worked_secs,
                # Breakdown so the client can render "you're short 1h40m: 30m
                # break + 2h36m offline were not counted".
                "break_seconds": break_secs_today,
                "offline_seconds": offline_secs_today,
                "message": "You have not completed today's required working hours.",
            }
            if not reason_text:
                return structured
            if len(reason_text) < _EARLY_REASON_MIN:
                return {**structured, "error": f"Reason must be at least {_EARLY_REASON_MIN} characters."}
            if len(reason_text) > _EARLY_REASON_MAX:
                reason_text = reason_text[:_EARLY_REASON_MAX]
            early_ctx = {
                "reason": reason_text,
                "worked_seconds": worked_secs,
                "required_seconds": required_secs,
            }

    now = datetime.now(timezone.utc)

    # Cap effective clock-in to local-day start so a session that somehow
    # spans midnight (stale auto-close missed it) never inflates the total.
    # "Local" is this organization's configured timezone.
    _now_ist = _tz.local_now(db, user.organization_id)
    _today_start_utc, _ = _tz.local_day_bounds(db, user.organization_id, _now_ist.date())
    # Treat a naive stored clock-in as UTC (matches _to_dashboard_date). Postgres
    # returns tz-aware values, but legacy rows — and SQLite — can be naive, which
    # would otherwise raise "can't compare offset-naive and offset-aware".
    _clock_in = session.clock_in_time
    if _clock_in.tzinfo is None:
        _clock_in = _clock_in.replace(tzinfo=timezone.utc)
    effective_start  = max(_clock_in, _today_start_utc)

    session.clock_out_time = now
    total                  = int((now - effective_start).total_seconds())
    # Recompute break_seconds / offline_seconds from their audited rows before
    # committing, so the cached columns used by analytics stay authoritative and
    # don't drift from the Clock Summary.
    #
    # Wrapped as a unit: these are REFINEMENTS of numbers we already hold. If any
    # of them fails, closing the session still matters far more than a perfectly
    # deducted total — an exception here used to surface as "Clock-out failed:
    # Error 500" and left the worker clocked in with no way out. Fall back to the
    # cached values and let the session close; analytics can be reconciled later.
    try:
        actual_break_at_close = _actual_break_seconds_for_sessions(
            db, {session.id: (effective_start, now)}
        ).get(session.id, session.break_seconds or 0)
        _, _offline_session_cap = _offline_caps(db, user.organization_id)
        offline_at_close = offline_seconds_for_session(
            db, session.id, session_cap_seconds=_offline_session_cap,
        )
    except Exception:
        # Roll back the aborted statement, then re-attach the session row: the
        # rollback expired it, so `session` must be re-read before we can write
        # clock_out_time on it.
        db.rollback()
        import logging as _log_co
        _log_co.getLogger(__name__).exception(
            "clock-out: break/offline recalc failed for session %s; "
            "closing with cached values", session.id,
        )
        session = db.query(models.ClockSession).filter(
            models.ClockSession.id == session.id
        ).first()
        if session is None or session.clock_out_time is not None:
            # Someone/something already closed it — nothing left to do.
            return {"success": True, "message": "Clocked out"}
        session.clock_out_time = now
        actual_break_at_close = session.break_seconds or 0
        offline_at_close = session.offline_seconds or 0

    session.break_seconds = actual_break_at_close
    session.offline_seconds = offline_at_close
    session.total_seconds = max(0, total - actual_break_at_close - offline_at_close)

    # §45.D-15 — this is a genuine Worker clock-out: WORKER on the clock_out
    # boundary, verified departure evidence.
    #
    # Deliberately appended HERE, at the point both paths above converge, and
    # not beside each `session.clock_out_time = now`. There are two such writes
    # — the primary one and the one in the post-rollback recovery branch — and
    # this single append covers both, which is what makes the invariant hold:
    #
    #   * the recompute above fails  -> `db.rollback()` discards nothing,
    #     because no event has been inserted yet, and the recovery branch then
    #     reaches this line normally;
    #   * the recovery branch returns early (the row was already closed by
    #     something else) -> no boundary was written by us, so no event is
    #     owed and none is created;
    #   * the commit below fails     -> boundary write AND event roll back
    #     together.
    #
    # Either way exactly one WORKER close event persists per successful
    # clock-out. Appending at both write sites instead would risk two.
    append_boundary_event(
        db,
        session_id  = session.id,
        user_id     = session.user_id,
        boundary    = BOUNDARY_CLOCK_OUT,
        boundary_at = now,
        provenance  = _Provenance.WORKER,
        actor_label = actor_label_for_user(user),
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    # Invalidate the desktop tracker's status cache so the next /tracking/ping
    # (and /status, /sync, /heartbeat) returns clocked_in=False immediately.
    # Without this the 60s _STATUS_CACHE keeps reporting "working" after a
    # clock-out, so the tracker keeps screenshotting, the tray stays on
    # "Working", and the "still clocked in" close-guard misfires for up to a
    # minute. Every OTHER transition (clock-in, start-break, resume-work,
    # network-pause, /break/start, /break/resume) already does this — clock-out
    # was the lone gap and the root cause of the post-clock-out desync.
    try:
        from app.tracking.router import _cache_invalidate
        _cache_invalidate(user.id)
    except Exception:
        pass

    # Invalidate team cache so next poll reflects the stopped status immediately
    _team_cache_invalidate_all()

    # Compute total worked today (all completed sessions today) for the WS broadcast.
    _day_start2, _ = _tz.local_day_bounds(
        db, user.organization_id, _tz.local_date(db, user.organization_id))
    try:
        prev_today = db.query(
            func.sum(models.ClockSession.total_seconds)
        ).filter(
            and_(
                models.ClockSession.user_id        == user.id,
                models.ClockSession.clock_in_time  >= _day_start2,
                models.ClockSession.clock_out_time.isnot(None),
            )
        ).scalar() or 0
        worked_today_secs = int(prev_today)
    except Exception:
        worked_today_secs = session.total_seconds

    # Broadcast clock-out event via WebSocket — org-scoped, not global
    try:
        from websocket_manager import manager
        import json
        hrs  = worked_today_secs // 3600
        mins = (worked_today_secs % 3600) // 60
        if user.organization_id:
            manager.broadcast_to_org_sync(user.organization_id, json.dumps({
                "type":                   "clock_out",
                "organization_id":        user.organization_id,
                "user_id":                user.id,
                "user_name":              user.name,
                "clock_out_time":         iso_z(now),
                "clock_in_time":          iso_z(effective_start),
                "break_elapsed_seconds":  session.break_seconds or 0,
                "worked_today_seconds":   worked_today_secs,
                "worked_display":         f"{hrs}h {mins}m",
                "timestamp":              iso_z(now),
            }))
    except Exception:
        pass

    # ── Record the early clock-out (Phase 2) ────────────────────────────────
    # The clock-out itself is already committed above and is byte-for-byte the
    # existing flow — this only ADDS an audit row and queues a notification.
    if early_ctx is not None:
        diff = early_ctx["required_seconds"] - early_ctx["worked_seconds"]
        # Audit — reuse the existing AuditLog framework (no new table needed).
        try:
            db.add(models.AuditLog(
                organization_id=user.organization_id,
                actor_id=user.id,
                target_id=user.id,
                entity="ClockSession",
                action="EarlyClockOut",
                payload={
                    "attendance_id":      session.id,
                    "worked_seconds":     early_ctx["worked_seconds"],
                    "required_seconds":   early_ctx["required_seconds"],
                    "difference_seconds": diff,
                    "reason":             early_ctx["reason"],
                    "clock_in_time":      iso_z(effective_start),
                    "clock_out_time":     iso_z(now),
                    "timestamp":          iso_z(now),
                },
            ))
            db.commit()
        except Exception:
            db.rollback()
        # Email — this organization's own recipients, resolved through the
        # cascade. Queued as a background task so a mail failure can never
        # block the clock-out.
        try:
            recipients = recipients_for_user(db, user, purpose="attendance.early_clockout")
            background_tasks.add_task(
                send_early_clockout_alert,
                recipients=recipients,
                employee_name=user.name or user.email,
                employee_email=user.email or "",
                employee_id=user.employee_id or "",
                employment_type=user.employment_type or "",
                department=user.department or "",
                organization_name=_org_name(db, user.organization_id),
                worked_seconds=early_ctx["worked_seconds"],
                required_seconds=early_ctx["required_seconds"],
                remaining_seconds=diff,
                clock_in=iso_z(effective_start),
                clock_out=iso_z(now),
                reason=early_ctx["reason"],
                tz_name=_tz.org_timezone_name(db, user.organization_id),
            )
        except Exception:
            pass

    return {
        "success":        True,
        "user_id":        user.id,
        "clock_out_time": iso_z(now),
        "worked_seconds": session.total_seconds,
        "early_clock_out": early_ctx is not None,
    }


# ===========================
# START BREAK
# ===========================
class _StartBreakBody(BaseModel):
    # Optional so existing callers that POST an empty body still work; new
    # clients should send "break" or "lunch" so the right overtime
    # threshold (15m vs 60m) applies.
    break_type: Optional[Literal["break", "lunch"]] = "break"


# Break entitlement is governed centrally — see app.breaks.governance. The
# governed model is per-workday (not per-shift): full-time workers get 1 Short
# + 1 Lunch, part-time 1 Short + no Lunch, contractors workspace policy
# (default 1 Short only). All counts/allowances live in governance so the
# monitor, the /break/* endpoints and the tests share one source of truth.


@router.post("/start-break")
def start_break(
    body: Optional[_StartBreakBody] = None,
    request: Request = None,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Attendance actions are desktop-app only — see WEB_ATTENDANCE_DISABLED.
    # Before any provenance row is written, so a blocked call leaves no trace.
    enforce_desktop_attendance(user, request, db)

    btype = (body.break_type if body else "break") or "break"
    if btype not in ("break", "lunch"):
        btype = "break"

    device_info    = _device_info(request)
    active_session = break_governance.get_active_session(db, user.id)
    active_break   = break_governance.get_active_break(db, user.id)
    worker_type    = break_governance.normalize_worker_type(user)
    allowed_min    = break_governance.allowance_minutes(db, btype, user.organization_id)

    # ── Provenance: record the request itself BEFORE evaluating it ───────────
    try:
        break_governance.record_break_event(
            db, user=user, event_type="BREAK_START_REQUESTED",
            session_id=active_session.id if active_session else None,
            worker_type=worker_type, break_type=btype, allowed_minutes=allowed_min,
            device_info=device_info, commit=True,
        )
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Service temporarily busy. Please try again in a moment.")

    # ── Server-side entitlement enforcement (BR-01 / BR-02 / BR-03) ──────────
    elig = break_governance.evaluate_eligibility(
        db, user, btype,
        active_session=active_session, active_break=active_break, _looked_up=True,
    )
    if not elig.allowed:
        break_governance.record_break_event(
            db, user=user, event_type="BREAK_DENIED",
            session_id=active_session.id if active_session else None,
            worker_type=worker_type, break_type=btype, allowed_minutes=allowed_min,
            eligibility_result="denied", denial_reason=elig.code,
            device_info=device_info, extra={"message": elig.message}, commit=True,
        )
        # Spec response shape ({allowed, code, message}); `success` kept so
        # existing callers/tests that check it keep working.
        return {
            "success":     False,
            "allowed":     False,
            "code":        elig.code,
            "message":     elig.message,
            "break_type":  btype,
            "worker_type": worker_type,
            "used_today":  elig.used_today,
            "entitlement": elig.entitlement,
        }

    now_break = datetime.now(timezone.utc)
    new_break = models.BreakSession(
        user_id     = user.id,
        session_id  = active_session.id,
        break_start = now_break,
        is_active   = True,
        break_type  = btype,
        overtime_notified = False,
        worker_type        = worker_type,
        allowed_minutes    = allowed_min,
        status             = "active",
        overrun_flag       = False,
        overrun_notification_sent = False,
        alert_count        = 0,
    )
    try:
        db.add(new_break)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    break_governance.record_break_event(
        db, user=user, event_type="BREAK_STARTED",
        session_id=active_session.id, break_id=new_break.id,
        worker_type=worker_type, break_type=btype, allowed_minutes=allowed_min,
        eligibility_result="allowed", device_info=device_info, commit=True,
    )

    # Invalidate status cache so next /tracking/ping gets on_break=True immediately
    try:
        from app.tracking.router import _cache_invalidate
        _cache_invalidate(user.id)
    except Exception:
        pass

    # Invalidate team cache so next /team poll returns "On Break" immediately
    _team_cache_invalidate_all()

    # Broadcast so admin dashboard switches to "On Break" — org-scoped
    try:
        from websocket_manager import manager
        import json
        if user.organization_id:
            manager.broadcast_to_org_sync(user.organization_id, json.dumps({
                "type":            "break_started",
                "organization_id": user.organization_id,
                "user_id":         user.id,
                "user_name":       user.name,
                "break_type":      btype,
                "timestamp":       iso_z(now_break),
            }))
    except Exception:
        pass

    return {
        "success":         True,
        "allowed":         True,
        "break_id":        new_break.id,
        "break_type":      btype,
        "allowed_minutes": allowed_min,
        "worker_type":     worker_type,
    }


# ===========================
# RESUME WORK
# ===========================
@router.post("/resume-work")
def resume_work(
    request: Request = None,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Attendance actions are desktop-app only — see WEB_ATTENDANCE_DISABLED.
    enforce_desktop_attendance(user, request, db)

    active = db.query(models.BreakSession).filter(
        and_(
            models.BreakSession.user_id   == user.id,
            models.BreakSession.break_end == None,
        )
    ).first()
    if not active:
        return {"success": False, "message": "No active break"}

    now                  = datetime.now(timezone.utc)
    # Coerce a naive break_start (SQLite / legacy rows) to UTC so the
    # subtraction never raises on a mixed naive/aware comparison.
    start = active.break_start if active.break_start.tzinfo else active.break_start.replace(tzinfo=timezone.utc)
    active.break_end     = now
    seconds              = int((now - start).total_seconds())
    active.break_seconds = seconds
    active.is_active     = False
    active.status        = "ended"

    # Finalize overrun provenance: a break can exceed its allowance between
    # monitor ticks, or while the monitor is not running (tests / dev).
    allowed_min = active.allowed_minutes or break_governance.allowance_minutes(
        db, active.break_type or "break", user.organization_id
    )
    overrun_seconds = max(0, seconds - allowed_min * 60)
    if overrun_seconds > 0:
        active.overrun_flag = True
        if active.status != "overdue":
            active.status = "ended"
        if active.allowance_reached_at is None:
            active.allowance_reached_at = start + timedelta(minutes=allowed_min)

    session = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_out_time == None,
        )
    ).first()
    if session:
        session.break_seconds = (session.break_seconds or 0) + seconds

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    # ── Provenance: BREAK_ENDED with the final duration + overrun summary ────
    break_governance.record_break_event(
        db, user=user, event_type="BREAK_ENDED",
        session_id=active.session_id, break_id=active.id,
        worker_type=active.worker_type, break_type=active.break_type,
        allowed_minutes=allowed_min, device_info=_device_info(request),
        extra={
            "actualDurationSeconds": seconds,
            "actualDurationMinutes": round(seconds / 60, 2),
            "overrunMinutes":        round(overrun_seconds / 60, 2),
            "overrunFlag":           bool(overrun_seconds > 0),
        },
        commit=True,
    )

    # Invalidate status cache so next /tracking/ping gets on_break=False immediately
    try:
        from app.tracking.router import _cache_invalidate
        _cache_invalidate(user.id)
    except Exception:
        pass

    # Invalidate team cache so next /team poll returns "Working" immediately
    _team_cache_invalidate_all()

    # Broadcast so admin dashboard switches to "Working" — org-scoped
    try:
        from websocket_manager import manager
        import json
        mins = seconds // 60
        secs = seconds % 60
        if user.organization_id:
            manager.broadcast_to_org_sync(user.organization_id, json.dumps({
                "type":            "break_ended",
                "organization_id": user.organization_id,
                "user_id":         user.id,
                "user_name":       user.name,
                "break_seconds":   seconds,
                "duration":        f"{mins}m {secs}s",
                "timestamp":       iso_z(now),
            }))
    except Exception:
        pass

    return {"success": True, "break_seconds": seconds}


# ===========================
# NETWORK PAUSE  /employees/network-pause
# Called by the frontend after a network-interrupt / server-crash recovery, so
# the worker isn't credited with time the tracker could not observe.
#
# This endpoint REDUCES a worker's credited (and therefore paid) time on the
# strength of a client-supplied interval, so it is treated as a privileged
# write and bounded accordingly:
#
#   * every accepted window is persisted as an OfflineInterval row — claimed vs
#     applied seconds, and whether a cap was hit — so any deduction can be
#     explained, audited and reversed;
#   * ClockSession.offline_seconds is recomputed as the UNION of those windows,
#     never a running sum, so a retrying client that re-posts an overlapping
#     window cannot inflate the deduction;
#   * a single window is capped at `disconnect_grace_minutes` and the session
#     total at `offline_max_session_minutes`. Anything longer is not a network
#     blip — it is an absence, and absences belong to idle detection /
#     auto-close, not to a silent subtraction of paid time.
#
# Before these bounds existed this endpoint added the raw claimed seconds
# straight onto break_seconds: 73 sessions in 90 days lost up to 8h20m each,
# one crediting 3 seconds of work for a 7-hour session, with no audit trail and
# nothing shown in any dashboard.
# ===========================

import logging as _logging_offline
_offline_log = _logging_offline.getLogger("zoiko.attendance.offline")

# Defaults; both overridable per-org via app_settings (see _offline_caps).
_OFFLINE_SINGLE_CAP_KEY   = "disconnect_grace_minutes"
_OFFLINE_SESSION_CAP_KEY  = "offline_max_session_minutes"
_OFFLINE_SINGLE_CAP_MIN   = 15
_OFFLINE_SESSION_CAP_MIN  = 60

_CAP_SINGLE  = "single_interval"
_CAP_SESSION = "session_total"


def _offline_caps(db: Session, org_id: Optional[int]) -> tuple[int, int]:
    """(max seconds for one window, max seconds per session) for this org."""
    def _mins(key: str, default: int) -> int:
        try:
            row = (
                db.query(models.AppSettings)
                .filter(
                    models.AppSettings.organization_id == org_id,
                    models.AppSettings.key == key,
                    models.AppSettings.team_key.is_(None),   # org level only
                    models.AppSettings.user_id.is_(None),
                )
                .first()
            )
            if row is not None and row.value is not None:
                val = int(str(row.value).strip())
                if val > 0:
                    return val
        except Exception:
            # A malformed or unreadable setting must never widen the cap.
            try:
                db.rollback()
            except Exception:
                pass
        return default

    single = _mins(_OFFLINE_SINGLE_CAP_KEY, _OFFLINE_SINGLE_CAP_MIN) * 60
    total  = _mins(_OFFLINE_SESSION_CAP_KEY, _OFFLINE_SESSION_CAP_MIN) * 60
    # A session cap below the single-window cap would be self-contradictory.
    return single, max(single, total)


def _union_seconds(intervals) -> int:
    """Total seconds covered by [start, end) intervals, counting overlaps once."""
    norm = [(_utc_aware(s), _utc_aware(e)) for s, e in intervals]
    ivs = sorted((s, e) for s, e in norm if s and e and e > s)
    if not ivs:
        return 0
    total = 0
    cur_start, cur_end = ivs[0]
    for s, e in ivs[1:]:
        if s <= cur_end:                 # overlapping / touching → extend
            cur_end = max(cur_end, e)
        else:
            total += int((cur_end - cur_start).total_seconds())
            cur_start, cur_end = s, e
    total += int((cur_end - cur_start).total_seconds())
    return max(0, total)


def offline_seconds_for_session(
    db: Session,
    session_id: int,
    *,
    session_cap_seconds: Optional[int] = None,
) -> int:
    """Authoritative offline seconds for a session — the UNION of its recorded
    OfflineInterval windows, capped at the session total. Derived on read so a
    duplicate post can never compound, mirroring how break seconds are derived
    from BreakSession rows rather than trusted from a cached counter."""
    try:
        rows = (
            db.query(models.OfflineInterval)
            .filter(models.OfflineInterval.session_id == session_id)
            .all()
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    secs = _union_seconds([(r.offline_start, r.offline_end) for r in rows])
    if session_cap_seconds is not None:
        secs = min(secs, session_cap_seconds)
    return secs


def _actual_offline_seconds_for_sessions(db: Session, session_bounds: dict) -> dict:
    """{session_id -> offline seconds}, each session's OfflineInterval windows
    unioned and CLIPPED to the supplied bounds.

    Deliberately mirrors _actual_break_seconds_for_sessions so both deductions
    are derived the same way: from their own source-of-truth rows, unioned (not
    summed), clipped to the window the caller cares about. Callers that report a
    single IST day pass day-clipped bounds so a session spanning midnight only
    has the relevant portion deducted.
    """
    if not session_bounds:
        return {}
    try:
        rows = (
            db.query(models.OfflineInterval)
            .filter(models.OfflineInterval.session_id.in_(list(session_bounds.keys())))
            .all()
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return {}

    intervals: dict = {}
    for r in rows:
        bounds = session_bounds.get(r.session_id)
        if not bounds:
            continue
        sess_start, sess_end = _utc_aware(bounds[0]), _utc_aware(bounds[1])
        start = max(_utc_aware(r.offline_start), sess_start)
        end   = min(_utc_aware(r.offline_end),   sess_end)
        if end <= start:
            continue
        intervals.setdefault(r.session_id, []).append((start, end))

    return {sid: _union_seconds(ivs) for sid, ivs in intervals.items()}


class NetworkPausePayload(BaseModel):
    offline_start: datetime
    offline_end:   datetime


@router.post("/network-pause")
def network_pause(
    payload: NetworkPausePayload,
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    start = payload.offline_start
    end   = payload.offline_end
    if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo   is None: end   = end.replace(tzinfo=timezone.utc)

    claimed_seconds = int((end - start).total_seconds())
    if claimed_seconds <= 0:
        return {"success": False, "message": "Invalid interval", "seconds": 0}

    session = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_out_time == None,
        )
    ).first()
    if not session:
        return {"success": False, "message": "Not clocked in", "seconds": 0}

    single_cap, session_cap = _offline_caps(db, user.organization_id)

    # Clamp the window to the session so we never subtract more time than the
    # worker has actually been clocked in.
    _clock_in_aware = session.clock_in_time
    if _clock_in_aware.tzinfo is None:
        _clock_in_aware = _clock_in_aware.replace(tzinfo=timezone.utc)
    clamped_start = max(start, _clock_in_aware)
    clamped_end   = min(end,   datetime.now(timezone.utc))
    if clamped_end <= clamped_start:
        return {
            "success": True, "seconds": 0,
            "offline_seconds": session.offline_seconds or 0,
            "break_seconds":   session.break_seconds or 0,
        }

    # Cap one window. Keep the EARLIEST portion — the start of a disconnect is
    # the part we have evidence for; a client holding a stale offline_start and
    # replaying hours on reconnect is exactly what this bound exists to stop.
    capped, cap_reason = False, None
    if int((clamped_end - clamped_start).total_seconds()) > single_cap:
        clamped_end = clamped_start + timedelta(seconds=single_cap)
        capped, cap_reason = True, _CAP_SINGLE

    applied_window_secs = int((clamped_end - clamped_start).total_seconds())

    try:
        db.add(models.OfflineInterval(
            user_id=user.id,
            session_id=session.id,
            organization_id=user.organization_id,
            offline_start=clamped_start,
            offline_end=clamped_end,
            claimed_seconds=claimed_seconds,
            applied_seconds=applied_window_secs,
            capped=capped,
            cap_reason=cap_reason,
        ))
        db.flush()   # make the new row visible to the union recompute below

        # Union, not sum — a duplicate/overlapping repost adds nothing.
        union_secs = offline_seconds_for_session(db, session.id)
        if union_secs > session_cap:
            union_secs = session_cap
            capped, cap_reason = True, _CAP_SESSION
        session.offline_seconds = union_secs
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    if capped:
        _offline_log.warning(
            "network-pause capped: user=%s session=%s claimed=%ss applied=%ss "
            "offline_total=%ss reason=%s — a window this long is an absence, "
            "not a connectivity blip; check the client's offline_start handling",
            user.id, session.id, claimed_seconds, applied_window_secs,
            session.offline_seconds, cap_reason,
        )

    try:
        from app.tracking.router import _cache_invalidate
        _cache_invalidate(user.id)
    except Exception:
        pass

    return {
        "success":         True,
        "seconds":         applied_window_secs,
        "claimed_seconds": claimed_seconds,
        "capped":          capped,
        "cap_reason":      cap_reason,
        "offline_seconds": session.offline_seconds,
        "break_seconds":   session.break_seconds or 0,
    }


# ===========================
# GET SELF STATUS  /employees/me
# ===========================
import time as _time
_LAST_AUTO_CLOSE: dict = {}   # user_id → last-run monotonic time
_AUTO_CLOSE_INTERVAL = 60     # run at most once per minute per user

@router.get("/me")
def get_status(
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # Run auto-close at most once per minute per user to avoid hammering the DB
    # on every 5-second poll from the React frontend.
    now_mono = _time.monotonic()
    if now_mono - _LAST_AUTO_CLOSE.get(user.id, 0) >= _AUTO_CLOSE_INTERVAL:
        _LAST_AUTO_CLOSE[user.id] = now_mono
        _auto_close_stale_sessions(db, org_id=user.organization_id)
        _auto_close_abandoned_sessions(db, org_id=user.organization_id)

    session = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_out_time == None,
        )
    ).first()

    raw_break = db.query(models.BreakSession).filter(
        and_(
            models.BreakSession.user_id   == user.id,
            models.BreakSession.break_end == None,
        )
    ).order_by(models.BreakSession.id.desc()).first()

    active_break = None
    if raw_break and session:
        if raw_break.break_start >= session.clock_in_time:
            active_break = raw_break

    # Sum all completed sessions today (IST day = UTC+5:30) for daily total.
    # Using IST so the counter resets at midnight IST, not 5:30 AM IST.
    now_ist = _tz.local_now(db, user.organization_id)
    today_start_utc, _ = _tz.local_day_bounds(db, user.organization_id, now_ist.date())
    completed_today = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_in_time  >= today_start_utc,
            models.ClockSession.clock_out_time != None,
        )
    ).all()
    # Rebuild worked_today from scratch: wall-clock per session minus actual
    # break time pulled from BreakSession records. Ignoring the cached
    # s.total_seconds / s.break_seconds avoids drift (zombie breaks, retry
    # races) and keeps this number in sync with /team and the Clock Summary.
    now_utc = datetime.now(timezone.utc)
    completed_bounds: dict = {}
    for s in completed_today:
        eff_start = max(_utc_aware(s.clock_in_time), today_start_utc)
        eff_end   = min(_utc_aware(s.clock_out_time), now_utc)
        if eff_end > eff_start:
            completed_bounds[s.id] = (eff_start, eff_end)
    completed_breaks  = _actual_break_seconds_for_sessions(db, completed_bounds)
    # Connectivity gaps are deducted here too. Omitting them made this endpoint
    # (which seeds the worker's "Total Worked Hours" tile) disagree with the
    # early-clock-out gate, so a worker could be shown 8h56m and still be told
    # they had not completed 8h.
    completed_offline = _actual_offline_seconds_for_sessions(db, completed_bounds)
    worked_today = 0
    break_today_secs = 0
    offline_today_secs = 0
    for sid, (st, en) in completed_bounds.items():
        _brk = completed_breaks.get(sid, 0)
        _off = completed_offline.get(sid, 0)
        break_today_secs   += _brk
        offline_today_secs += _off
        worked_today += max(0, int((en - st).total_seconds()) - _brk - _off)

    # Fold in admin-credited adjustments for today. /me is the source the
    # employee Dashboard reads to seed `prevWorkedSecsRef`, which the live
    # tick adds session-elapsed on top of. Without this, an admin-added
    # adjustment is invisible to the worker until they clock out + back in.
    worked_today += adjustment_seconds_for_day(db, user.id, now_ist.date())

    # For an active session, total_seconds is 0 (only set on clock-out).
    # Compute the real elapsed so the frontend timer can resume correctly.
    # Cap effective_start to IST-day midnight so a stale cross-day session
    # doesn't hand the frontend a 27h elapsed count.
    if session:
        _effective_start = max(_utc_aware(session.clock_in_time), today_start_utc)
        raw_elapsed = int((now_utc - _effective_start).total_seconds())
        # Actual break time for the current session from BreakSession records.
        active_bounds = {session.id: (_effective_start, now_utc)} if now_utc > _effective_start else {}
        active_breaks = _actual_break_seconds_for_sessions(db, active_bounds).get(session.id, 0)
        active_offline = _actual_offline_seconds_for_sessions(db, active_bounds).get(session.id, 0)
        elapsed_secs = max(0, raw_elapsed - active_breaks - active_offline)
        break_today_secs   += active_breaks
        offline_today_secs += active_offline
    else:
        elapsed_secs = 0
        active_breaks = 0
        active_offline = 0

    # How long the CURRENT break has been running (0 if not on break).
    # Returned so the frontend can resume the break timer correctly after
    # navigating away and back — without this, breakStartMsRef is reset to
    # Date.now() each reload and break time is lost.
    current_break_secs = 0
    break_gov = None
    if active_break:
        _bstart = active_break.break_start if active_break.break_start.tzinfo else active_break.break_start.replace(tzinfo=timezone.utc)
        current_break_secs = max(0, int((datetime.now(timezone.utc) - _bstart).total_seconds()))
        # Governance view of the live break: allowed / elapsed / exceeded so the
        # widget can render the status chip, banner and timer per spec.
        _btype = active_break.break_type or "break"
        _allowed_min = active_break.allowed_minutes or break_governance.allowance_minutes(db, _btype, user.organization_id)
        _grace_min = break_governance.grace_minutes(db, _btype, user.organization_id)
        _exceeded = current_break_secs >= _allowed_min * 60
        _notification_due_seconds = (_allowed_min + _grace_min) * 60
        _notification_due = current_break_secs >= _notification_due_seconds
        break_gov = {
            "break_type":        _btype,
            "allowed_minutes":   _allowed_min,
            "allowed_seconds":   _allowed_min * 60,
            "elapsed_seconds":   current_break_secs,
            "exceeded":          _exceeded,
            "exceeded_seconds":  max(0, current_break_secs - _allowed_min * 60),
            "notification_due":  _notification_due,
            "notification_delay_minutes": _grace_min,
            "notification_due_seconds": _notification_due_seconds,
            "notification_pending_seconds": max(0, _notification_due_seconds - current_break_secs),
            "status":            "overdue" if _exceeded else "active",
            "status_chip":       break_governance.status_chip(_btype, _exceeded),
            "banner":            break_governance.overrun_banner(_btype) if _exceeded else None,
            "indicator":         break_governance.governance_state(current_break_secs, _allowed_min),
        }

    return {
        "clocked_in":             session is not None,
        "on_break":               active_break is not None,
        "clock_in_time":          iso_z(_effective_start) if session else None,
        "elapsed_seconds":        elapsed_secs,
        "break_elapsed_seconds":  active_breaks  if session else 0,
        "current_break_seconds":  current_break_secs,
        "current_break_type":     (active_break.break_type or "break") if active_break else None,
        "break_governance":       break_gov,
        "worked_today_seconds":   worked_today,
        # Day-level deduction breakdown (all of today's sessions, including the
        # open one). The worker's tile can now show WHY elapsed time and worked
        # time differ instead of leaving them to discover it at clock-out.
        "break_today_seconds":    break_today_secs,
        "offline_today_seconds":  offline_today_secs,
        "employment_type":        user.employment_type or "full_time",
        # Required worked seconds for THIS worker today, resolved server-side.
        # The employee dashboard used to derive this from a duplicated 8h/4h table
        # in frontend/src/lib/attendancePolicy.js, which silently disagreed with
        # the clock-out gate as soon as an org configured anything. Same function
        # the Early Clock Out gate calls, so the two can no longer diverge.
        "expected_work_seconds":  resolve_expected_work_seconds(db, user, now_ist.date()),
        "worker_type":            break_governance.normalize_worker_type(user),
        # Desktop-only clock-in policy. Additive, and a pure in-memory predicate
        # (no query) so it costs nothing on this ~5s poll. The web dashboard
        # swaps its Clock In button for a "use the desktop app" message when
        # true; it never has to know which orgs the rule covers.
        "desktop_clock_in_required": desktop_clock_in_required(user, db),
    }


# ===========================
# DAY SUMMARY  /employees/me/day-summary?date=YYYY-MM-DD
# ===========================
# Powers the My Timesheet date navigation. Returns the same shape the
# frontend already consumes for "today" so the UI can swap data sources
# based on the selected date without reshaping its components. Day
# boundaries are IST so the row aligns with the date the user actually
# sees on their calendar.

# Cache the configured tracker tick interval (`screenshot_interval`) for
# 60s per org — keyed by organization_id so tenants don't share a stale value.
_DAY_SUMMARY_TICK_CACHE: dict = {}   # keyed by org_id
_DAY_SUMMARY_TICK_TTL = 60


def _get_day_summary_tick(db: Session, org_id=None) -> int:
    import time as _t
    now = _t.monotonic()
    cached = _DAY_SUMMARY_TICK_CACHE.get(org_id)
    if cached and now <= cached["expires"]:
        return cached["value"]
    val = 30
    try:
        row = (
            db.query(models.AppSettings)
            .filter(
                models.AppSettings.organization_id == org_id,
                models.AppSettings.key == "screenshot_interval",
                models.AppSettings.team_key.is_(None),   # org level only
                models.AppSettings.user_id.is_(None),
            )
            .first()
        )
        if row and row.value:
            n = int(str(row.value).strip())
            if 1 <= n <= 3600:
                val = n
    except Exception:
        pass
    _DAY_SUMMARY_TICK_CACHE[org_id] = {"value": val, "expires": now + _DAY_SUMMARY_TICK_TTL}
    return val


@router.get("/me/day-summary")
def get_day_summary(
    date: str = Query(..., description="IST calendar date, YYYY-MM-DD"),
    db:   Session     = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    today_ist = _tz.local_date(db, user.organization_id)
    if target > today_ist:
        # Future dates have no data — return an empty payload rather than 4xx
        # so the frontend can render "No data" without special-casing errors.
        return {
            "date":                 date,
            "is_today":             False,
            "has_data":             False,
            "clocked_in":           False,
            "clock_in_time":        None,
            "clock_out_time":       None,
            "worked_seconds":       0,
            "active_seconds":       0,
            "worked_days":          0,
        }

    # Local midnight bounds → UTC for DB queries (clock_in_time is tz-aware UTC).
    day_start_ist_utc, day_end_ist_utc = _tz.local_day_bounds(
        db, user.organization_id, target)
    now_utc           = datetime.now(timezone.utc)

    # All sessions that overlap the IST day.
    sessions = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.user_id        == user.id,
            models.ClockSession.clock_in_time   <  day_end_ist_utc,
        )
    ).all()
    sessions = [
        s for s in sessions
        if (s.clock_out_time is None and target == today_ist)
           or (s.clock_out_time is not None
               and s.clock_out_time > day_start_ist_utc)
    ]

    # Build session bounds clipped to the IST day for accurate worked time.
    bounds: dict = {}
    first_in_utc = None
    last_out_utc = None
    any_open     = False
    for s in sessions:
        s_in_aware  = _utc_aware(s.clock_in_time)
        if s.clock_out_time is None:
            any_open    = True
            s_out_aware = now_utc
        else:
            s_out_aware = _utc_aware(s.clock_out_time)
        eff_start = max(s_in_aware,  day_start_ist_utc)
        eff_end   = min(s_out_aware, day_end_ist_utc)
        if eff_end <= eff_start:
            continue
        bounds[s.id] = (eff_start, eff_end)
        # Track first clock-in / last clock-out anchored to the original
        # session times (so the displayed "IN"/"OUT" reflect what actually
        # happened, not the IST-day clip).
        if first_in_utc is None or s_in_aware < first_in_utc:
            first_in_utc = s_in_aware
        if s.clock_out_time is not None:
            if last_out_utc is None or s_out_aware > last_out_utc:
                last_out_utc = s_out_aware

    # Worked time excludes BOTH deductions. Offline time was previously omitted
    # here, so this endpoint reported raw elapsed-minus-breaks as "Total Worked
    # Hours" while the early-clock-out gate compared against the real figure —
    # a worker could be shown 8h56m and still be told they hadn't done 8h, with
    # no way to see the 2h36m connectivity gap that explained the difference.
    # Both are returned separately below so the UI can show the breakdown.
    actual_breaks  = _actual_break_seconds_for_sessions(db, bounds)
    actual_offline = _actual_offline_seconds_for_sessions(db, bounds)
    worked = 0
    break_total = 0
    offline_total = 0
    for sid, (st, en) in bounds.items():
        elapsed  = int((en - st).total_seconds())
        brk      = actual_breaks.get(sid, 0)
        off      = actual_offline.get(sid, 0)
        break_total   += brk
        offline_total += off
        worked += max(0, elapsed - brk - off)

    # Active seconds — sum effective time per activity row.
    #
    # Each row represents one tracker tick whose nominal length is the
    # configured `screenshot_interval` (1800s in prod). Hard-coding 30s
    # under-counted active time by 60× and made full days of work show
    # up as 11 minutes (BUG-007). For accuracy we:
    #   * read the tick length from settings dynamically,
    #   * cap each tick at the gap to the next tick (so a clock-out
    #     mid-interval doesn't credit a full 30 min),
    #   * subtract the per-tick `idle_seconds` so within-tick idle
    #     doesn't inflate active time. The tracking router writes
    #     `idle_seconds` as idle-WITHIN-tick (interval − OS active
    #     seconds), so this subtraction reflects real workstation idle.
    #     is_active fires on ANY input signal (clicks, movement, scroll,
    #     keystrokes, or OS-reported active seconds), so reading- /
    #     scrolling-heavy intervals are no longer misclassified.
    # ── Active Time (redesigned engine) ──
    # The authoritative Active Time comes from the Active Time Engine's daily
    # score (continuity-aware, screenshot-correlated, break-aware, anti-inflated;
    # see app/active_time/). We fall back to the legacy per-tick boolean loop
    # only for days with no engine windows (pre-engine history / legacy trackers
    # that never sent window-grade signals).
    active_pct = 0.0
    active_seconds = 0
    _used_engine_band = False
    try:
        from app.active_time import service as _at_service
        from app.active_time.config import load_config as _at_load_config
        _daily = _at_service.get_daily(db, user.id, target)
    except Exception:
        _daily = None

    if _daily is not None and (_daily.window_count or 0) > 0:
        active_seconds = min(int(_daily.active_seconds or 0), worked)
        # Displayed % = the same individual, never-below-floor value the admin
        # card shows (activity-biased, spread, per (user, day)). active_seconds
        # stays real for the hours/duration views.
        if _daily.active_pct is not None:
            try:
                from app.active_time.engine import display_active_pct as _disp_fn
                active_pct = float(_disp_fn(user.id, str(target), _daily.active_pct, _at_load_config(db, user.organization_id)))
            except Exception:
                active_pct = round(float(_daily.active_pct), 2)
            _used_engine_band = True
        else:
            _cap = _at_load_config(db, user.organization_id).daily_cap
            raw_pct = (100.0 * active_seconds / worked) if worked > 0 else 0.0
            active_pct = round(min(raw_pct, _cap), 2)
    else:
        # ── Legacy fallback ── per-tick effective length minus idle-within-tick.
        tick_max = _get_day_summary_tick(db)
        acts = (
            db.query(
                models.Activity.timestamp,
                models.Activity.is_active,
                models.Activity.idle_seconds,
            )
            .filter(
                and_(
                    models.Activity.user_id   == user.id,
                    models.Activity.timestamp >= day_start_ist_utc,
                    models.Activity.timestamp <  day_end_ist_utc,
                )
            )
            .order_by(models.Activity.timestamp)
            .all()
        )
        n_acts = len(acts)
        for i, a in enumerate(acts):
            tick = tick_max
            if i + 1 < n_acts:
                gap = int((acts[i + 1].timestamp - a.timestamp).total_seconds())
                if 0 < gap < tick:
                    tick = gap
            idle_in_tick = max(0, min(tick, int(a.idle_seconds or 0)))
            if a.is_active:
                active_seconds += tick - idle_in_tick
        # Active can never exceed worked time — clamp so a stale tick from
        # before clock-out can't inflate the displayed value past the actual
        # session length.
        active_seconds = min(active_seconds, worked)
        active_pct = round((100.0 * active_seconds / worked) if worked > 0 else 0.0, 2)

    # Fold in admin-credited time adjustments for this date (active only).
    # See app/_time_adjustments.py — adjustments compensate for tracker
    # outages (power cuts, system crashes) and are additive, so they
    # increase `worked_seconds` but NOT `active_seconds` (we have no
    # tracker activity data for the lost interval).
    adj_secs = adjustment_seconds_for_day(db, user.id, target)
    worked  += adj_secs

    # Recompute the % against the final worked denominator (adjustments grow
    # worked but not active) so active_pct == active_seconds / worked_seconds,
    # then apply the engine's anti-inflation cap. Skipped when the displayed %
    # is the engine band score (activity-based, not a ratio of worked time).
    if not _used_engine_band:
        try:
            _cap = _at_load_config(db, user.organization_id).daily_cap
        except Exception:
            _cap = 100.0
        active_pct = round(min((100.0 * active_seconds / worked) if worked > 0 else 0.0, _cap), 2)

    has_data = bool(bounds) or adj_secs > 0

    return {
        "date":              date,
        "is_today":          target == today_ist,
        "has_data":          has_data,
        "clocked_in":        any_open and target == today_ist,
        "clock_in_time":     iso_z(first_in_utc),
        "clock_out_time":    None if (any_open and target == today_ist) else iso_z(last_out_utc),
        "worked_seconds":    worked,
        # The two deductions that separate elapsed time from worked time, so the
        # UI can explain the gap instead of leaving the worker to guess.
        "break_seconds":     break_total,
        "offline_seconds":   offline_total,
        "elapsed_seconds":   worked + break_total + offline_total,
        "active_seconds":    int(active_seconds),
        "active_pct":        active_pct,
        "adjustment_seconds": adj_secs,
        "tracker_seconds":   max(0, worked - adj_secs),
        "worked_days":       1 if has_data else 0,
    }


# ===========================
# ADMIN TEAM STATUS  /employees/team
# ===========================
def _actual_break_seconds_for_sessions(
    db: Session,
    session_bounds: dict,
) -> dict:
    # Recompute break seconds for each ClockSession from BreakSession records,
    # which are the source of truth. ClockSession.break_seconds is a cached
    # denormalized field that drifts due to retry races, zombie breaks
    # (break_end never saved), auto-close edge cases, and ghost breaks (no
    # break_end but session superseded). Trusting it makes "Hours Worked"
    # disagree with the Clock Summary panel, which builds from BreakSession.
    #
    # Each break contributes (end − start) clipped to the session's span.
    # Zombie breaks (break_end=None, is_active=False) contribute 0.
    # Ghost breaks (break_end=None on a closed session) are capped at
    # clock_out. Ongoing breaks on still-active sessions run to `now`.
    #
    # session_bounds: {session_id -> (sess_start_naive, sess_end_naive)}
    # Returns {session_id -> actual_break_seconds}.
    if not session_bounds:
        return {}
    session_ids = list(session_bounds.keys())
    breaks = (
        db.query(models.BreakSession)
        .filter(models.BreakSession.session_id.in_(session_ids))
        .all()
    )
    # Collect each break as a [start, end) interval per session, then take the
    # UNION (not the sum). Raw summing double-counted break time whenever a
    # client retry race left two BreakSession rows with the same break_start
    # and break_end=NULL — both got extended to `now`, halving the worker's
    # displayed Hours Worked. The Clock Summary panel renders only the last
    # open break as ongoing (earlier ones collapse to ghost rows), so the
    # union here gives the panel's number too.
    intervals: dict = {}  # session_id -> list[(start, end)]
    for b in breaks:
        bounds = session_bounds.get(b.session_id)
        if not bounds:
            continue
        sess_start, sess_end = bounds
        # Normalize before comparing — see _utc_aware. b.break_start /
        # break_end come from the DB and can be naive; sess_start/sess_end are
        # aware. This raised on every read path that includes an open break.
        bs = _utc_aware(b.break_start)
        if b.break_end is not None:
            be = _utc_aware(b.break_end)
        elif b.is_active is False:
            # Zombie break — ended without recording break_end. Don't count.
            continue
        else:
            # Ongoing break: run it to the session's effective end (now for
            # still-active session; clock_out for a closed session means this
            # is a ghost break and we cap it at clock_out).
            be = sess_end
        start = max(bs, sess_start)
        end   = min(be, sess_end)
        if end <= start:
            continue
        intervals.setdefault(b.session_id, []).append((start, end))

    result: dict = {}
    for sess_id, ivs in intervals.items():
        ivs.sort(key=lambda iv: iv[0])
        cur_start, cur_end = ivs[0]
        total = 0
        for s, e in ivs[1:]:
            if s <= cur_end:
                # Overlapping or touching — extend the merged interval.
                if e > cur_end:
                    cur_end = e
            else:
                total += int((cur_end - cur_start).total_seconds())
                cur_start, cur_end = s, e
        total += int((cur_end - cur_start).total_seconds())
        result[sess_id] = total
    return result


def _auto_close_stale_sessions(db: Session, org_id: Optional[int] = None,
                               now: datetime | None = None) -> None:
    """Any still-open ClockSession from a previous ORGANIZATION-LOCAL day is
    treated as the user having forgotten to clock out. We close it at the end of
    its own local day so hours stop accumulating past 24 and admin dashboards
    don't show 70+ hour numbers.
    Also closes any still-open BreakSession that belongs to such a session.

    org_id scopes the cleanup to a single tenant so one org's trigger cannot
    modify another org's sessions.

    Both ends are this organization's clock. The selection already was; the
    CLOSE INSTANT was not — it used UTC calendar midnight of the clock-in's UTC
    date, so an Indian tenant's forgotten session closed at 05:29 the next local
    morning (a 19½-hour day) and a New York tenant's overnight session closed
    around 20:00 the following local evening. Now both come from
    ``_tz.local_day_bounds`` for the local date the session actually started on.
    """
    # "Before today" means before local midnight for the organization being
    # swept. A UTC boundary closed an Indian tenant's sessions 5.5h early and a
    # US tenant's 5h late.
    today_start_utc, _ = _tz.local_day_bounds(db, org_id, _tz.local_date(db, org_id))
    stale_q = db.query(models.ClockSession).filter(
        and_(
            models.ClockSession.clock_out_time.is_(None),
            models.ClockSession.clock_in_time < today_start_utc,
        )
    )
    if org_id is not None:
        stale_q = stale_q.filter(models.ClockSession.organization_id == org_id)
    stale = stale_q.all()
    if not stale:
        return
    for s in stale:
        # Treat a naive stored clock-in as UTC — the same guard clock_out uses.
        # Postgres returns aware values; legacy rows and SQLite can be naive, and
        # subtracting one from an aware boundary raises.
        clock_in = s.clock_in_time
        if clock_in.tzinfo is None:
            clock_in = clock_in.replace(tzinfo=timezone.utc)

        # End of the ORGANIZATION-LOCAL day this session started on, expressed
        # as the correct UTC instant. `local_day_bounds` returns [start, end)
        # where end is the next local midnight — the exclusive boundary is what
        # we want here: the session is closed exactly as the local day rolls
        # over, never a fraction of a day early or hours late.
        _org = s.organization_id or org_id
        _local_in = _tz.to_local(db, _org, clock_in)
        _, day_end = _tz.local_day_bounds(db, _org, _local_in.date())
        s.clock_out_time = day_end
        total = int((day_end - clock_in).total_seconds())
        # Close any break that was left open inside this session FIRST, so the
        # recompute below includes it.
        open_break = db.query(models.BreakSession).filter(
            and_(
                models.BreakSession.user_id   == s.user_id,
                models.BreakSession.break_end.is_(None),
                models.BreakSession.break_start >= s.clock_in_time,
                models.BreakSession.break_start <  day_end,
            )
        ).first()
        if open_break:
            open_break.break_end     = day_end
            _bs = open_break.break_start
            if _bs.tzinfo is None:
                _bs = _bs.replace(tzinfo=timezone.utc)
            open_break.break_seconds = int((day_end - _bs).total_seconds())
        db.flush()  # make the updated break visible to the recompute query
        # Recompute authoritative break seconds from BreakSession records.
        actual_breaks = _actual_break_seconds_for_sessions(
            db, {s.id: (s.clock_in_time, day_end)}
        ).get(s.id, s.break_seconds or 0)
        s.break_seconds = actual_breaks
        s.total_seconds = max(0, total - actual_breaks)

        # §45.D-15 — a synthesized closure at the organization-local end of
        # the day the session started on. The Worker never departed at this
        # instant; nothing observed them leave. STALE_AUTOCLOSE says exactly
        # that, and `actor_label` is SYSTEM because no person performed it.
        # SYSTEM is the actor, never the provenance.
        append_boundary_event(
            db,
            session_id  = s.id,
            user_id     = s.user_id,
            boundary    = BOUNDARY_CLOCK_OUT,
            boundary_at = day_end,
            provenance  = _Provenance.STALE_AUTOCLOSE,
            actor_label = SYSTEM_ACTOR_LABEL,
        )
    db.commit()


import logging as _logging
from app.commercial.enforcement import shadow_capability as _shadow
_log = _logging.getLogger("zoiko.attendance.autoclose")

# Whether to auto-close abandoned sessions, and how long the tracker may stay
# silent first, are BOTH per-organization policy:
#
#   attendance_policy.disconnect_detection_enabled   (default true)
#   attendance_policy.disconnect_grace_minutes       (default 15)
#
# Both settings already existed and were documented as "not consumed yet" while
# this module used a hard-coded 15 minutes and a module-level True. They are
# consumed now. An organization whose staff work in low-connectivity locations
# widens the grace; one that never wants automatic closure switches it off.
#
# History on the enable flag: auto-close was DISABLED 2026-06-19 because a
# server/network outage makes EVERY tracker go silent at once, and the old code
# then clocked out everyone. It is safe to run again because the sweep SKIPS
# itself when a large fraction of an org's monitored sessions are silent
# simultaneously (that pattern == an infrastructure outage, not individual
# laptops dying). A single/few silent sessions (real laptop shutdowns) are still
# closed — at the LAST HEARTBEAT time, so hours are accurate and never inflated.

# Circuit breaker: only trip (skip the whole sweep) when BOTH hold — at least
# this many sessions are silent AND they are at least this fraction of the
# org's monitored (not-on-break) open sessions. Small counts = real laptop
# deaths → close them; mass silence = outage → leave everyone alone.
_ABANDONED_MASS_MIN = 4
_ABANDONED_MASS_RATIO = 0.5

def _auto_close_abandoned_sessions(db: Session, org_id: Optional[int] = None,
                                   now: datetime | None = None) -> None:
    """For every user with an OPEN clock session, check when their desktop
    tracker last heartbeated. If silence exceeds the grace window, close the
    session at the time of the last heartbeat (or the clock-in time if we've
    never seen a heartbeat). This is the safety net for the close-button
    dialog we can't ship to installed Electron apps without a rebuild.

    org_id scopes the cleanup to a single tenant so one org's trigger cannot
    modify another org's sessions.
    """
    # Both the switch and the window are this organization's policy.
    from app.attendance_policy import (
        policy_bool, policy_int,
        SETTING_DISCONNECT_AUTOCLOSE, SETTING_DISCONNECT_GRACE_MINUTES,
    )
    _probe = models.User(organization_id=org_id)
    if not policy_bool(db, _probe, SETTING_DISCONNECT_AUTOCLOSE):
        return
    grace_minutes = policy_int(db, _probe, SETTING_DISCONNECT_GRACE_MINUTES)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=grace_minutes)

    open_q = db.query(models.ClockSession).filter(
        models.ClockSession.clock_out_time.is_(None)
    )
    if org_id is not None:
        open_q = open_q.filter(models.ClockSession.organization_id == org_id)
    open_sessions = open_q.all()
    if not open_sessions:
        return

    user_ids = [s.user_id for s in open_sessions]
    live_map: dict = {
        ls.user_id: ls
        for ls in db.query(models.LiveStatus)
                    .filter(models.LiveStatus.user_id.in_(user_ids))
                    .all()
    }

    # Build a set of user_ids who are currently on an ACTIVE break (is_active=True).
    # We must NEVER auto-close a session while the employee is on break —
    # breaks are intentional silence (no heartbeats) and can be longer than
    # the grace window. Auto-closing during a break causes "Stopped Work"
    # to appear in the admin dashboard after a 10-minute break.
    # NOTE: We filter is_active=True so that zombie/stale BreakSessions
    # (break_end=None but is_active=False from an old failed resume) do NOT
    # prevent today's session from being auto-closed.
    on_break_user_ids = {
        row.user_id
        for row in db.query(models.BreakSession.user_id).filter(
            and_(
                models.BreakSession.user_id.in_(user_ids),
                models.BreakSession.break_end.is_(None),
                models.BreakSession.is_active == True,
            )
        ).all()
    }

    # Live DB rows can be naive (TIMESTAMP WITHOUT TIME ZONE on prod, and SQLite)
    # while `now`/`cutoff` are tz-aware. Comparing the two in Python raises
    # TypeError, so coerce every stored datetime to aware-UTC before comparing —
    # the same shim clock_out uses.
    def _aw(dt):
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    # ── Pass 1: gather candidates (silent past grace), each with its close time.
    # We do NOT mutate anything yet — the circuit breaker below may veto the
    # whole sweep, and we must never partially clock people out during an outage.
    candidates: list = []  # (session, clock_in_aware, close_at)
    for s in open_sessions:
        # Skip users who are currently on a break — silence is expected.
        if s.user_id in on_break_user_ids:
            continue

        live = live_map.get(s.user_id)
        last_beat = _aw(live.updated_at) if live and live.updated_at else None
        cin = _aw(s.clock_in_time)

        # Only auto-close if the tracker was alive DURING THIS SESSION and
        # has been quiet past the grace window.
        if last_beat is None or last_beat >= cutoff:
            continue
        if last_beat < cin:
            continue  # heartbeat predates this session — tracker wasn't active

        # Never clock them out BEFORE they clocked in.
        close_at = max(last_beat, cin)

        # Handle break logic — close no earlier than the last completed break,
        # and skip if that break ended within the grace window (still recent).
        last_completed_break = db.query(models.BreakSession).filter(
            and_(
                models.BreakSession.user_id    == s.user_id,
                models.BreakSession.session_id == s.id,
                models.BreakSession.break_end.isnot(None),
            )
        ).order_by(models.BreakSession.break_end.desc()).first()

        if last_completed_break and last_completed_break.break_end:
            bend = _aw(last_completed_break.break_end)
            close_at = max(close_at, bend)
            if bend >= cutoff:
                continue

        # And never after "now" (defensive).
        if close_at > now:
            close_at = now

        # Final safety check for active breaks
        live_break = db.query(models.BreakSession).filter(
            and_(
                models.BreakSession.user_id   == s.user_id,
                models.BreakSession.break_end.is_(None),
                models.BreakSession.is_active == True,
            )
        ).first()
        if live_break:
            continue

        candidates.append((s, cin, close_at))

    if not candidates:
        return

    # ── CIRCUIT BREAKER ──────────────────────────────────────────────────────
    # If a large fraction of this org's MONITORED (not-on-break) open sessions
    # went silent at once, it is almost certainly a server/network-side outage,
    # not many laptops dying simultaneously. Closing them would mass-clock-out
    # active employees — the exact failure that got this disabled. Skip entirely
    # and log; the next healthy sweep (or the overnight backstop) will handle any
    # genuinely stale ones. A single/few silent sessions fall below the
    # threshold and ARE closed.
    monitored = sum(1 for s in open_sessions if s.user_id not in on_break_user_ids)
    if (len(candidates) >= _ABANDONED_MASS_MIN
            and len(candidates) >= _ABANDONED_MASS_RATIO * max(1, monitored)):
        _log.warning(
            "abandoned-close SKIPPED (circuit breaker): %d of %d monitored open "
            "sessions silent for org=%s — treating as an outage, not laptop "
            "shutdowns; no sessions closed.",
            len(candidates), monitored, org_id,
        )
        return

    # ── Pass 2: close the vetted candidates at their last-verified time. ──────
    for s, cin, close_at in candidates:
        s.clock_out_time = close_at
        total = int((close_at - cin).total_seconds())
        actual_breaks = _actual_break_seconds_for_sessions(
            db, {s.id: (cin, close_at)}
        ).get(s.id, s.break_seconds or 0)
        s.break_seconds = actual_breaks
        s.total_seconds = max(0, total - actual_breaks)

        # §45.D-15 — a synthesized closure at the last verified heartbeat.
        # `close_at` is `max(last_beat, cin)`, so it can equal the clock-in
        # instant exactly: a zero-length session that looks identical, BY
        # VALUE, to a Worker who clocked out immediately. This event is the
        # only thing that distinguishes them, which is why the origin has to be
        # recorded here and cannot be reconstructed later.
        append_boundary_event(
            db,
            session_id  = s.id,
            user_id     = s.user_id,
            boundary    = BOUNDARY_CLOCK_OUT,
            boundary_at = close_at,
            provenance  = _Provenance.ABANDONED_AUTOCLOSE,
            actor_label = SYSTEM_ACTOR_LABEL,
        )

    db.commit()


@router.get("/early-clock-outs")
def early_clock_outs(
    date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Early Clock Out reasons for the org on a given day (default today, IST),
    for the Admin Dashboard so a manager can see WHY someone left early.

    Admin/HR only, strictly org-scoped. Reads the existing AuditLog
    EarlyClockOut rows written at clock-out time (Phase 2) — no new storage.
    Returns both a flat list and a by_user map (keyed by str(user_id), latest
    wins) so the dashboard can attach the reason to each worker card.
    """
    if (user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admins or HR only")

    org_id = user.organization_id
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid date. Use YYYY-MM-DD")
    else:
        day = _tz.local_date(db, user.organization_id)

    # AuditLog.created_at is stored as naive UTC (datetime.utcnow); compare
    # against naive UTC bounds derived from the organization-local calendar day.
    _ds, _de = _tz.local_day_bounds(db, org_id, day)
    day_start = _ds.replace(tzinfo=None)
    day_end = _de.replace(tzinfo=None)
    try:
        rows = db.query(models.AuditLog).filter(
            models.AuditLog.organization_id == org_id,
            models.AuditLog.action == "EarlyClockOut",
            models.AuditLog.created_at >= day_start,
            models.AuditLog.created_at < day_end,
        ).order_by(models.AuditLog.created_at.desc()).all()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")

    by_user: dict = {}
    items: list = []
    for r in rows:
        p = r.payload or {}
        rec = {
            "user_id":            r.target_id,
            "attendance_id":      p.get("attendance_id"),
            "reason":             p.get("reason"),
            "worked_seconds":     p.get("worked_seconds"),
            "required_seconds":   p.get("required_seconds"),
            "difference_seconds": p.get("difference_seconds"),
            "clock_in_time":      p.get("clock_in_time"),
            "clock_out_time":     p.get("clock_out_time"),
            "timestamp":          p.get("timestamp") or iso_z(r.created_at),
        }
        items.append(rec)
        key = str(r.target_id) if r.target_id is not None else None
        if key and key not in by_user:   # desc order → most recent wins
            by_user[key] = rec

    # ── "Latest clock-out was early" rule ────────────────────────────────────
    # An early-clock-out audit only earns a badge if it corresponds to the
    # user's MOST RECENT session. If they later clocked in again (new open
    # session) or clocked out normally (a newer closed session), the badge must
    # not show. One extra batch query over just the affected users (no N+1):
    # find each user's latest session id and keep only matching records.
    affected = [int(k) for k in by_user.keys()]
    if affected:
        latest_session_by_user: dict = {}
        try:
            sess_rows = (
                db.query(models.ClockSession.id, models.ClockSession.user_id)
                .filter(
                    models.ClockSession.user_id.in_(affected),
                    models.ClockSession.organization_id == org_id,
                )
                .order_by(models.ClockSession.user_id,
                          models.ClockSession.clock_in_time.desc())
                .all()
            )
        except Exception:
            sess_rows = []
        for s in sess_rows:
            if s.user_id not in latest_session_by_user:   # first row per user = latest
                latest_session_by_user[s.user_id] = s.id
        for key in list(by_user.keys()):
            rec = by_user[key]
            if rec.get("attendance_id") != latest_session_by_user.get(int(key)):
                del by_user[key]   # a newer session exists → not the latest clock-out

    return {"date": day.isoformat(), "items": items, "by_user": by_user}


@router.get("/team")
def team_status(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    now = datetime.now(timezone.utc)
    cached = _team_cache_get(current_org.id)
    if cached is not None:
        return cached

    # Defer the two janitor passes until AFTER we've returned the response.
    # Both functions write to the DB (closing stale + abandoned sessions) and
    # together added ~200–800 ms of serial query time to every cache-miss
    # request — i.e. the perceived "load workers" delay on the Overview tab.
    # Their job is purely cleanup; the next poll (≤ 30 s later) will read the
    # cleaned-up rows. Using FastAPI BackgroundTasks runs them on the same
    # process right after the response is sent, with a fresh DB session.
    from database.database import SessionLocal as _TeamSessionLocal

    _bg_org_id = current_org.id  # capture before background task runs

    def _run_auto_close():
        s = _TeamSessionLocal()
        try:
            _auto_close_stale_sessions(s, _bg_org_id, now)
            _auto_close_abandoned_sessions(s, _bg_org_id, now)
        except Exception:
            s.rollback()
        finally:
            s.close()

    background_tasks.add_task(_run_auto_close)

    # The seeded "Administrator" (CEO seat) is not an employee — strip it
    # before we compute every aggregate the Overview reads (Working /
    # On Break / Awaiting Clock-In / Stopped Working / Requires Attention /
    # Absent / Total Employees / Avg Hours).
    users = exclude_system_admin(
        db.query(models.User)
        .filter(models.User.organization_id == current_org.id)
        .filter(models.User.is_active == True)
        .all(),
        db,
    )

    # Empty-workspace fast path: skip every downstream aggregation (clock
    # sessions, break recomputes, today's activity ticks, idle alerts, leave,
    # audit log) because they all key off `users` being non-empty. New
    # workspaces hit this on every admin dashboard load — without it the
    # Overview tab burns ~hundreds of ms running queries that always return
    # empty result sets.
    if not users:
        result: list = []
        _team_cache_set(current_org.id, result)
        return result

    # Used as a join key for tables that lack an organization_id column in the
    # actual DB (break_sessions, leave_requests, audit_logs, live_statuses).
    # Scoping by user_id is equivalent to scoping by org because users already
    # filtered to current_org.id above.
    user_ids: list = [u.id for u in users]

    # Fetch only the LATEST clock session per user instead of dragging the
    # entire ClockSession table into memory. As the table grows this scales
    # from O(total_sessions) to O(num_users).
    latest_session_subq = (
        db.query(
            models.ClockSession.user_id,
            func.max(models.ClockSession.id).label("max_id"),
        )
        .filter(models.ClockSession.organization_id == current_org.id)
        .group_by(models.ClockSession.user_id)
        .subquery()
    )
    sessions = (
        db.query(models.ClockSession)
        .join(
            latest_session_subq,
            models.ClockSession.id == latest_session_subq.c.max_id,
        )
        .all()
    )
    session_map: dict = {s.user_id: s for s in sessions}

    # Admin dashboard must show the EMPLOYEE'S FIRST clock-in of the day,
    # not the most recent one. Otherwise every break/clock-out→clock-in cycle
    # makes the "started at" time jump forward. The first clock-in resets at
    # IST midnight via today_start_utc.
    today_ist = _tz.local_date(db, current_org.id)
    day_start_utc, _ = _tz.local_day_bounds(db, current_org.id, today_ist)
    first_today_rows = (
        db.query(
            models.ClockSession.user_id,
            func.min(models.ClockSession.clock_in_time).label("first_in"),
        )
        .filter(
            models.ClockSession.organization_id == current_org.id,
            models.ClockSession.clock_in_time >= day_start_utc,
        )
        .group_by(models.ClockSession.user_id)
        .all()
    )
    first_today_map: dict = {r.user_id: r.first_in for r in first_today_rows}

    # Sum of today's COMPLETED session seconds per user (used to compute
    # worked_today_seconds accurately when an employee has clocked in/out
    # multiple times in one day, e.g. worked 12:41am–1:30am then again 9am–now).
    # Without this, using (now − first_clock_in) overcounts all the offline gaps.
    #
    # NOTE: We deliberately recompute from actual clock_in/clock_out timestamps
    # rather than trusting the stored total_seconds field, which may be inflated
    # due to auto-close miscalculations or duplicate sessions created by retry
    # races. Each session is also capped to [day_start_utc, now] so a session
    # that somehow spans IST midnight never inflates today's total.
    # A SANITY guard against inflated rows (auto-close miscalculations,
    # duplicate sessions from retry races) — see the note above. It is not a
    # policy about how long anyone may work, and it must never truncate a
    # legitimate long shift: an org running 16-hour or double shifts saw its
    # dashboard silently cut every such day back to 14h.
    #
    # So it DERIVES from the employee's own resolved expected hours with
    # generous headroom, and only falls back to the historical 14h when no
    # expectation is configured. Bounded by the resolver's own 24h ceiling.
    # The stored ClockSession.total_seconds is never touched by this.
    def _sanity_cap_secs(u) -> int:
        # Reads the expected seconds out of `_sched_map`, which is built ONCE
        # below from a single batched org-wide resolve (annotate ->
        # resolve_work_schedules -> build_context, ~5 queries for the whole
        # organization). This is only ever called from the per-employee loop,
        # which runs after that map exists.
        #
        # It used to call resolve_expected_work_seconds(db, u, ...) here, which
        # re-entered resolve_work_schedule WITHOUT the prefetched context —
        # once per employee. Each of those re-ran the whole cascade against the
        # database: a ShiftAssignment lookup, a holiday lookup, an org-setting
        # read and the full settings-tier merge. Measured on a 57-employee
        # workspace that turned /employees/team into 738 statements and ~102 s,
        # so the browser's 15 s timeout always fired and the dashboard silently
        # kept painting its last localStorage snapshot — days-old data with no
        # error shown.
        expected = 0
        try:
            expected = int((_sched_map.get(getattr(u, "id", None)) or {})
                           .get("expected_seconds") or 0)
        except Exception:
            expected = 0
        if not expected:
            # Degraded path only: the batched resolve failed or omitted this
            # user. Same answer as before, still per-user, but now confined to
            # the rows the batch could not cover instead of every row.
            try:
                from app.attendance_policy import resolve_expected_work_seconds
                expected = resolve_expected_work_seconds(db, u, today_ist_date) or 0
            except Exception:
                expected = 0
        return int(min(24 * 3600, max(14 * 3600, expected * 2)))

    _MAX_DAY_SECS = 14 * 3600  # fallback only; per-employee value used below
    today_completed_raw = (
        db.query(
            models.ClockSession.id,
            models.ClockSession.user_id,
            models.ClockSession.clock_in_time,
            models.ClockSession.clock_out_time,
        )
        .filter(
            and_(
                models.ClockSession.organization_id == current_org.id,
                models.ClockSession.clock_in_time  >= day_start_utc,
                models.ClockSession.clock_out_time.isnot(None),
            )
        )
        .all()
    )
    # Deduplicate by (user_id, clock_in minute) before summing — retry races
    # can create multiple identical ClockSession rows which inflate the total.
    # Keep the row with the largest clock_out_time (most complete data).
    _dedup: dict = {}  # (user_id, minute_key) → row
    for row in today_completed_raw:
        minute_key = row.clock_in_time.strftime("%Y%m%d%H%M")
        key = (row.user_id, minute_key)
        existing = _dedup.get(key)
        if existing is None or row.clock_out_time > existing.clock_out_time:
            _dedup[key] = row

    # Recompute break seconds from BreakSession records (source of truth).
    # See _actual_break_seconds_for_sessions for the reasoning.
    completed_bounds: dict = {}
    for row in _dedup.values():
        # Normalize before comparing: `day_start_utc` is tz-aware (it comes
        # from _tz.local_day_bounds) while a stored timestamp can be naive on
        # legacy rows and under SQLite, and max()/min() on a mixed pair raises
        # "can't compare offset-naive and offset-aware" — a 500 on the main
        # dashboard endpoint. _utc_aware is the module's existing guard.
        eff_start = max(_utc_aware(row.clock_in_time), day_start_utc)
        eff_end   = min(_utc_aware(row.clock_out_time), now)
        if eff_end > eff_start:
            completed_bounds[row.id] = (eff_start, eff_end)
    completed_break_map = _actual_break_seconds_for_sessions(db, completed_bounds)

    today_completed_map: dict = {}
    for row in _dedup.values():
        bounds = completed_bounds.get(row.id)
        if not bounds:
            continue
        eff_start, eff_end = bounds
        break_secs = completed_break_map.get(row.id, 0)
        net = max(0, int((eff_end - eff_start).total_seconds()) - break_secs)
        today_completed_map[row.user_id] = today_completed_map.get(row.user_id, 0) + net

    # Admin-credited time adjustments for TODAY, keyed by user_id. Folded
    # into worked_today_seconds in each status branch below so the admin
    # "My Team" cards and the worker's own dashboard agree. Bulk-fetched
    # in one query (vs. per-user in the loop) for the same reason every
    # other map here is bulk-fetched — /team is the hottest poll endpoint.
    user_id_list = [u.id for u in users]
    today_adj_map = adjustment_seconds_for_day_by_user(db, user_id_list, today_ist)

    # Only currently-open break rows are needed for status, and there's at
    # most one per user, so this stays tiny regardless of history size.
    # Filter by user_id instead of organization_id — the break_sessions table
    # does not have an organization_id column in the DB yet.
    open_breaks = (
        db.query(models.BreakSession)
        .filter(
            models.BreakSession.user_id.in_(user_ids),
            models.BreakSession.break_end.is_(None),
        )
        .all()
    )
    break_map: dict = {}
    for b in open_breaks:
        if b.user_id not in break_map or b.id > break_map[b.user_id].id:
            break_map[b.user_id] = b

    # ── Leaves covering today ──────────────────────────────────────────────────
    # Authorisation model per product spec (updated 2026-06-08):
    #   • "On Leave"  → employee has an APPROVED LeaveRequest covering today.
    #                   A leave only shields the worker from the Absent bucket
    #                   once HR/Admin has approved it. Merely submitting it (or a
    #                   manager moving it to under_review) is NOT enough — until
    #                   approval the day behaves like any other: the worker is
    #                   expected to clock in, else falls through to
    #                   Yet-to-Start / Absent like everyone else.
    #   • "Absent"    → not approved-on-leave AND not clocked in past threshold.
    # Rejected/draft/locked/PENDING rows are all excluded — only `approved`
    # counts. (Supersedes the 2026-05-25 rule that also shielded submitted /
    # under_review leaves; this now matches the daily report, which has always
    # counted only approved leaves — see reports/router.py.)
    _now_local = _tz.local_now(db, current_org.id)
    today_ist_date = _now_local.date()
    today_start_naive = datetime.combine(today_ist_date, datetime.min.time(), tzinfo=timezone.utc)
    today_end_naive   = datetime.combine(today_ist_date, datetime.max.time(), tzinfo=timezone.utc)

    # Only approved leaves affect employee status — submitted/under_review are still pending.
    active_leave_statuses = ("approved",)
    approved_leaves = db.query(models.LeaveRequest).filter(
        and_(
            models.LeaveRequest.user_id.in_(user_ids),
            models.LeaveRequest.status.in_(active_leave_statuses),
            models.LeaveRequest.from_date <= today_end_naive,
            models.LeaveRequest.to_date   >= today_start_naive,
        )
    ).all()
    on_leave_user_ids: set = {lr.user_id for lr in approved_leaves}
    # Approved half-day leave today: user_id -> "first_half" | "second_half".
    # A half-day worker usually clocks in for the other half, so their status
    # stays Working / Stopped; the card shows this as a badge instead.
    half_day_leave_map: dict = {
        lr.user_id: (lr.half_day_period or "half")
        for lr in approved_leaves if getattr(lr, "is_half_day", False)
    }

    # ── Working day + absence cutoff ───────────────────────────────────────────
    # Both come from THIS organization's effective schedule, evaluated on THIS
    # organization's wall clock. Nothing here is a constant any more:
    #
    #   working day     schedule.working_days + the org's holiday calendar
    #   absence cutoff  schedule.absent_after_hour, per employee via the cascade
    #   "now"           OrgSettings.default_timezone
    #
    # The org-level probe answers the org-level question (is today a working day
    # at all) in one resolve; the per-employee cutoff comes from `_sched_map`
    # below, so a team or an individual can carry their own.
    now_ist = _now_local
    _org_sched = None
    try:
        from app.schedule_policy.resolver import resolve_work_schedule
        _org_sched = resolve_work_schedule(
            db, models.User(organization_id=current_org.id), today_ist_date)
    except Exception:
        # resolve_work_schedule fails open internally, so reaching here means
        # something structural broke. Treat today as a working day rather than
        # silently marking a whole workspace non-working.
        _log_att.warning("org schedule resolve failed org=%s; treating today as "
                         "a working day", current_org.id, exc_info=True)

    if _org_sched is not None:
        is_holiday = _org_sched.is_holiday
        is_weekend = not _org_sched.is_working_day and not is_holiday
    else:
        is_holiday = False
        is_weekend = False

    # Users we've already notified as Absent today — prevents duplicate emails
    # when /team is polled every 30s.
    already_notified_absent: set = set()
    if not is_weekend and not is_holiday:
        day_start_ist_utc, _ = _tz.local_day_bounds(db, current_org.id, today_ist_date)
        # Filter by target_id — audit_logs lacks organization_id in the DB.
        prior_alerts = db.query(models.AuditLog.target_id).filter(
            and_(
                models.AuditLog.target_id.in_(user_ids),
                models.AuditLog.entity == "User",
                models.AuditLog.action == "EmployeeMarkedAbsent",
                models.AuditLog.created_at >= day_start_ist_utc,
            )
        ).all()
        already_notified_absent = {row.target_id for row in prior_alerts if row.target_id}

    # Current foreground app + tracker last-seen per user — LiveStatus is updated
    # by the desktop tracker every ~15s via /tracking/heartbeat.
    # If a user has no LiveStatus row yet (tracker never pinged), fall back to
    # the most recent Activity row so we still show something useful.
    current_app_map: dict = {}
    tracker_last_seen_map: dict = {}  # user_id → ISO timestamp string of last heartbeat
    # Per-user browser tab context — populated by the Temveris browser
    # extension via /browser/heartbeat. Surfaces alongside `current_app` so
    # the admin card can render "Chrome / ChatGPT" instead of bare "Chrome".
    current_tab_map: dict = {}

    # Scope to the team members we're actually rendering rather than every
    # LiveStatus row in the system.
    _team_user_ids = {u.id for u in users}
    live_rows = (
        db.query(models.LiveStatus).filter(models.LiveStatus.user_id.in_(_team_user_ids)).all()
        if _team_user_ids else []
    )
    for ls in live_rows:
        if ls.app_name:
            current_app_map[ls.user_id] = ls.app_name
        if ls.updated_at:
            tracker_last_seen_map[ls.user_id] = iso_z(ls.updated_at)
        # Only attach tab context when the extension has actually pinged
        # (tab_title or domain present) — otherwise the frontend would
        # render an awkward "Chrome / —" pair.
        if ls.tab_title or ls.domain:
            current_tab_map[ls.user_id] = {
                "tab_title":    ls.tab_title or None,
                "domain":       ls.domain or None,
                "browser":      ls.browser or None,
                "favicon_url":  ls.favicon_url or None,
                "window_title": ls.window_title or None,
            }

    # Fallback for the card chip: when a user's foreground app is a browser
    # but LiveStatus has no tab info (extension not heartbeating right now),
    # surface the most recent BrowserActivity row from today so the card can
    # still render "Chrome / app.grammarly.com" instead of bare "Chrome".
    # Guarded on the app name being a browser so we don't decorate non-browser
    # apps (VS Code, Slack) with a stale website.
    _BROWSER_RE = re.compile(r"\b(chrome|edge|firefox|safari|brave|opera|vivaldi|arc)\b", re.I)
    browser_fallback_ids = [
        uid for uid, app in current_app_map.items()
        if uid not in current_tab_map and app and _BROWSER_RE.search(app)
    ]
    if browser_fallback_ids:
        latest_ba_ts_subq = (
            db.query(
                models.BrowserActivity.user_id,
                func.max(models.BrowserActivity.ended_at).label("latest_ended"),
            )
            .filter(
                models.BrowserActivity.user_id.in_(browser_fallback_ids),
                models.BrowserActivity.ended_at >= day_start_utc,
            )
            .group_by(models.BrowserActivity.user_id)
            .subquery()
        )
        latest_ba_rows = (
            db.query(models.BrowserActivity)
            .join(
                latest_ba_ts_subq,
                and_(
                    models.BrowserActivity.user_id  == latest_ba_ts_subq.c.user_id,
                    models.BrowserActivity.ended_at == latest_ba_ts_subq.c.latest_ended,
                ),
            )
            .all()
        )
        for ba in latest_ba_rows:
            if ba.tab_title or ba.domain:
                current_tab_map[ba.user_id] = {
                    "tab_title":   ba.tab_title or None,
                    "domain":      ba.domain or None,
                    "browser":     ba.browser or None,
                    "favicon_url": ba.favicon_url or None,
                    "window_title": None,
                }

    # Fallback: most recent Activity for users without a LiveStatus entry
    missing_ids = [u.id for u in users if u.id not in current_app_map]
    if missing_ids:
        latest_ts_subq = (
            db.query(
                models.Activity.user_id,
                func.max(models.Activity.timestamp).label("latest_ts"),
            )
            .filter(
                models.Activity.app_name.isnot(None),
                models.Activity.user_id.in_(missing_ids),
            )
            .group_by(models.Activity.user_id)
            .subquery()
        )
        latest_acts = (
            db.query(models.Activity)
            .join(
                latest_ts_subq,
                and_(
                    models.Activity.user_id   == latest_ts_subq.c.user_id,
                    models.Activity.timestamp == latest_ts_subq.c.latest_ts,
                ),
            )
            .all()
        )
        for a in latest_acts:
            if a.user_id and a.app_name:
                current_app_map[a.user_id] = a.app_name

    # Most recent COMPLETED break end per user today. Used by the idle-anchor
    # logic below: when a worker returns from a break, the idle clock should
    # reset to the moment they resumed — otherwise a 30-min break makes the
    # worker look like they've been idle for 30+ min the second they sit
    # back down. We filter to today only, so a break from yesterday can't
    # leak in. NULL break_end means an OPEN break (worker still on break,
    # status="On Break" path already skips the alert), so exclude those.
    # Filter by user_id — break_sessions lacks organization_id in the DB.
    latest_break_end_rows = (
        db.query(
            models.BreakSession.user_id,
            func.max(models.BreakSession.break_end).label("latest_end"),
        )
        .filter(
            and_(
                models.BreakSession.user_id.in_(user_ids),
                models.BreakSession.break_end.isnot(None),
                models.BreakSession.break_end >= day_start_utc,
            )
        )
        .group_by(models.BreakSession.user_id)
        .all()
    )
    latest_break_end_map: dict = {r.user_id: r.latest_end for r in latest_break_end_rows}

    # Pre-compute actual break seconds for every currently-OPEN session so the
    # Working / On Break branches below use the BreakSession source-of-truth
    # rather than the drift-prone session.break_seconds column.
    open_session_bounds: dict = {}
    for sess in sessions:
        if sess.clock_out_time is not None:
            continue
        # Same guard as the completed-session loop above: a stored timestamp
        # can be naive (legacy rows, and SQLite) while day_start_utc is aware,
        # and max() on a mixed pair raises. Without this /employees/team 500s
        # for the whole organization as soon as one person is clocked in.
        effective_cin = max(_utc_aware(sess.clock_in_time), day_start_utc)
        if now > effective_cin:
            open_session_bounds[sess.id] = (effective_cin, now)
    open_break_map = _actual_break_seconds_for_sessions(db, open_session_bounds)

    # Bulk per-user active-seconds for today. Used by the dashboard to render
    # an "Active Time %" that measures real workstation interaction during
    # working time — NOT a denominator that grows during breaks. The
    # percentage is computed on the frontend as
    # active_today_seconds / worked_today_seconds, so approved breaks (which
    # add to neither) do not reduce it.
    #
    # Per-tick model (matches /me/day-summary so the live card and the
    # per-day summary agree):
    #   • tick length    = min(screenshot_interval, gap to next activity row)
    #   • idle_in_tick   = the row's `idle_seconds` column. The tracking
    #                      router now stores this as idle-WITHIN-tick
    #                      (= interval - OS active-seconds in that interval),
    #                      NOT the raw cumulative OS idle clock.
    #   • active_in_tick = tick - idle_in_tick, summed across all
    #                      is_active=True rows.
    # `is_active` itself is set true whenever ANY interaction signal fires in
    # the interval — clicks, movement, scroll, keystrokes, or OS-reported
    # active-seconds — so reading/scrolling-heavy sessions are no longer
    # misclassified as idle (the old "44% while actively working" bug).
    # The query stays cached at the /team layer (8s TTL), so this runs at
    # most ~once every 8s regardless of poll frequency.
    day_end_utc = day_start_utc + timedelta(days=1)

    # ── Active Time (redesigned engine) — ONE indexed read, not an org-wide scan ──
    # Active seconds per user come from the pre-computed ActivityDailyScore rows
    # (maintained by the reconcile worker). This replaces the old query that
    # pulled EVERY Activity row for the whole company into Python on each poll
    # (millions of rows at a 30s interval). active_pct is the authoritative,
    # capped figure the UI shows.
    active_today_map: dict = {}
    active_pct_map: dict = {}          # banded display % (keyboard+mouse, floor..100)
    daily_flags_map: dict = {}
    _at_display_floor = 65.0
    _at_cfg_obj = None
    import hashlib as _hashlib
    try:
        from app.active_time import service as _at_service
        from app.active_time.config import load_config as _at_load_config
        from app.active_time.engine import display_active_pct as _at_display_active_pct
        _at_cfg_obj = _at_load_config(db, current_org.id)
        _at_cap = _at_cfg_obj.daily_cap
        _at_display_floor = _at_cfg_obj.display_floor
        _at_day_key = str(_at_service.local_today(db, current_org.id))
        for uid, daily in _at_service.get_daily_map_for_today(
                db, current_org.id).items():
            active_today_map[uid] = int(daily.active_seconds or 0)
            if daily.active_pct is not None:
                active_pct_map[uid] = float(daily.active_pct)
            daily_flags_map[uid] = daily.flags or None
    except Exception:
        _at_cap = 100.0
        # Safe fallbacks so the per-user loop never NameErrors if the import failed.
        # NOTE: no local alias named `_tz` here — that shadows the module-level
        # app._tz import for the whole function and breaks every timezone call.
        _at_day_key = str(_tz.local_date(db, current_org.id))
        def _at_display_active_pct(_uid, _dk, _band=None, _cfg=None):  # noqa: E306
            return int(round(_band)) if _band is not None else 65

    # Live idle-alert signals still come from raw Activity rows, but via cheap
    # DB-side aggregates (1 row/user) instead of materialising every row:
    #   * last_active_map[uid] = MAX(timestamp) where is_active   (idle anchor)
    #   * tracker_reporting_users = DISTINCT user_id with any row  (alert gate)
    #
    # Both are scoped by `user_ids` — the org's active users, resolved above.
    # They previously filtered on the timestamp window ALONE, so each request
    # aggregated today's `activities` rows for EVERY tenant on the platform and
    # one org's dashboard load scaled with total platform activity rather than
    # its own team size. Scoping by user_id (rather than Activity.organization_id)
    # matches how every other bulk map in this endpoint is scoped and keeps
    # legacy rows whose organization_id was left NULL, so the values read per
    # user are byte-for-byte what they were: both maps are only ever consulted
    # as last_active_map[u.id] / (u.id in tracker_reporting_users) for a `u`
    # that is already in `users`.
    #
    # This also repairs a knock-on effect of the org-scoped daily-score map:
    # `missing_uids` below subtracts active_today_map (org-scoped) from this set,
    # so while this set was platform-wide, the legacy fallback query underneath
    # it was re-reading other tenants' Activity rows on every cache miss.
    last_active_map: dict = {}
    try:
        from sqlalchemy import func as _func
        for uid, ts in (
            db.query(models.Activity.user_id, _func.max(models.Activity.timestamp))
            .filter(
                and_(
                    models.Activity.user_id.in_(user_ids),
                    models.Activity.timestamp >= day_start_utc,
                    models.Activity.timestamp <  day_end_utc,
                    models.Activity.is_active == True,  # noqa: E712
                )
            )
            .group_by(models.Activity.user_id)
            .all()
        ):
            if uid is not None:
                last_active_map[uid] = ts
        tracker_reporting_users = {
            uid for (uid,) in (
                db.query(models.Activity.user_id)
                .filter(
                    and_(
                        models.Activity.user_id.in_(user_ids),
                        models.Activity.timestamp >= day_start_utc,
                        models.Activity.timestamp <  day_end_utc,
                    )
                )
                .distinct()
                .all()
            )
            if uid is not None
        }
    except Exception:
        tracker_reporting_users = set()

    # ── Legacy fallback (scoped) ──
    # Users who are reporting activity today but have NO engine daily-score row
    # yet — the transition window right after deploy (windows only exist for
    # screenshots taken since the upgrade) and any tracker that never produced
    # window-grade signals. We compute their active seconds with the old
    # per-tick loop so their cards don't read 0% while the engine backfills.
    # Scoped to ONLY the missing users, so once everyone is scored this set is
    # empty and no extra Activity query runs (steady-state stays a single
    # indexed read).
    missing_uids = [uid for uid in tracker_reporting_users if uid not in active_today_map]
    if missing_uids:
        try:
            tick_max = _get_day_summary_tick(db)
            legacy_rows = (
                db.query(
                    models.Activity.user_id,
                    models.Activity.timestamp,
                    models.Activity.is_active,
                    models.Activity.idle_seconds,
                )
                .filter(
                    and_(
                        models.Activity.user_id.in_(missing_uids),
                        models.Activity.timestamp >= day_start_utc,
                        models.Activity.timestamp <  day_end_utc,
                    )
                )
                .order_by(models.Activity.user_id, models.Activity.timestamp)
                .all()
            )
            legacy_by_user: dict = {}
            for a in legacy_rows:
                if a.user_id is not None:
                    legacy_by_user.setdefault(a.user_id, []).append(a)
            for uid, rows in legacy_by_user.items():
                secs = 0
                n = len(rows)
                for i, a in enumerate(rows):
                    tick = tick_max
                    if i + 1 < n:
                        gap = int((rows[i + 1].timestamp - a.timestamp).total_seconds())
                        if 0 < gap < tick:
                            tick = gap
                    idle_in_tick = max(0, min(tick, int(a.idle_seconds or 0)))
                    if a.is_active:
                        secs += tick - idle_in_tick
                active_today_map[uid] = secs
        except Exception:
            pass

    # ── Idle-alert threshold ─────────────────────────────────────────────────
    # Surface the live "currently idle" duration so the admin dashboard can
    # show a warning chip + ring on the employee card, AND queue an HR email
    # the moment a worker crosses the threshold. Dedup via AuditLog so the
    # 8-second poll cache doesn't fire one email every refresh.
    #
    # The threshold is `attendance.idle_alert_minutes` for this organization,
    # not a hard-coded 15 minutes. It is deliberately a SEPARATE setting from
    # the tracker's `idle_threshold_minutes`: that one only tags a screenshot
    # as captured-while-idle, whereas this one emails HR about a person.
    from app.attendance_policy import policy_int as _policy_int, SETTING_IDLE_ALERT_MINUTES

    # Every override row for this ONE key, all three scopes, in ONE query —
    # then each employee is resolved in memory below.
    #
    # `policy_int(db, u, ...)` per employee went through
    # settings.scope.resolve_effective, which issues three scope queries EACH
    # (org + team + employee). On a 57-employee workspace that was ~170 settings
    # queries for a single value, and the org-tier query was byte-identical all
    # 57 times. Precedence is unchanged: employee > team > org > system default.
    _idle_org_raw = None
    _idle_team_raw: dict = {}
    _idle_emp_raw: dict = {}
    try:
        for _r in (
            db.query(models.AppSettings)
            .filter(models.AppSettings.organization_id == current_org.id,
                    models.AppSettings.key == SETTING_IDLE_ALERT_MINUTES)
            .all()
        ):
            if _r.user_id is not None:
                _idle_emp_raw[_r.user_id] = _r.value
            elif _r.team_key is not None:
                _idle_team_raw[_r.team_key] = _r.value
            else:
                _idle_org_raw = _r.value
    except Exception:
        # Never let a settings read break the endpoint; a failed statement also
        # leaves the transaction aborted, which would 500 every query after it.
        db.rollback()

    def _idle_threshold_secs(u) -> int:
        """This employee's own idle-alert threshold.

        Resolved from the prefetch above rather than a per-employee settings
        cascade. A team or employee override of `attendance.idle_alert_minutes`
        still wins over the org value, which is why this is per employee at all.
        """
        raw = None
        uid = getattr(u, "id", None)
        tkey = (getattr(u, "team", None) or "").strip() or None
        if uid in _idle_emp_raw:
            raw = _idle_emp_raw[uid]
        elif tkey is not None and tkey in _idle_team_raw:
            raw = _idle_team_raw[tkey]
        elif _idle_org_raw is not None:
            raw = _idle_org_raw
        try:
            return max(60, int(str(raw).strip()) * 60)
        except Exception:
            # No row, or an unparseable one — fall back to the org-tier resolve
            # exactly as before (one query, not one per employee).
            return IDLE_ALERT_THRESHOLD_SECS

    # Org-tier value, used only for logging and as the fallback below.
    IDLE_ALERT_THRESHOLD_SECS = max(
        60, _policy_int(db, models.User(organization_id=current_org.id),
                        SETTING_IDLE_ALERT_MINUTES) * 60)

    # Build a "have we already alerted this user during their current idle
    # streak?" set by reading recent AuditLog entries. We compare against the
    # user's last-active timestamp: an audit log entry NEWER than last_active
    # means "already alerted in this streak — skip." When the user becomes
    # active again, last_active moves forward and old audit rows fall out of
    # scope, so the next idle streak gets its own alert.
    # Filter by target_id — audit_logs lacks organization_id in the DB.
    today_audit_rows = (
        db.query(models.AuditLog.target_id, models.AuditLog.created_at)
        .filter(
            and_(
                models.AuditLog.target_id.in_(user_ids),
                models.AuditLog.entity     == "User",
                models.AuditLog.action     == "EmployeeIdleAlert",
                models.AuditLog.created_at >= day_start_utc,
            )
        )
        .all()
    )
    # latest_idle_alert_map[uid] = timestamp of most recent idle alert today
    latest_idle_alert_map: dict = {}
    for r in today_audit_rows:
        if r.target_id is None:
            continue
        existing = latest_idle_alert_map.get(r.target_id)
        if existing is None or r.created_at > existing:
            latest_idle_alert_map[r.target_id] = r.created_at

    # ── Per-employee resolved schedule ────────────────────────────────────────
    # arrival_status / late_minutes / expected_seconds / grace / shift window /
    # absent_after_hour are served on EVERY request: the admin dashboard renders
    # them rather than deriving them in React, so omitting them would strip the
    # Late badge from every worker with a configured shift.
    #
    # Resolved PER EMPLOYEE, not per organization, so a team- or employee-scope
    # override of any schedule key actually reaches the dashboard. Batched into
    # one prefetch (five queries for the whole org) by
    # resolver.resolve_work_schedules.
    _sched_map: dict = {}
    try:
        from app.schedule_policy import annotate as _annotate
        _sched_map = _annotate.annotate(
            db, users, today_ist_date,
            # Reuse the clock-in times already loaded for this request.
            {uid: s.clock_in_time for uid, s in session_map.items() if s},
        )
    except Exception:
        _log_att.warning("schedule annotate failed org=%s", current_org.id,
                         exc_info=True)
        _sched_map = {}

    def _past_absent_cutoff(user_id) -> bool:
        """Is this employee past their own absence cutoff?

        There is exactly ONE implementation of that question —
        ``schedule_policy.classify.classify_arrival`` — and ``annotate`` above
        has already run it for every employee in this response, on this
        organization's clock, against this employee's resolved schedule
        (including the shift-relative ``absent_after_minutes`` cutoff and the
        never-before-the-shift guard).

        This used to re-derive the rule here as ``now_ist.hour >= hour``. Two
        implementations of one question is how a part-timer could read "Absent"
        on the dashboard while the same payload's ``status`` said "Yet to
        Start"; it is also why the noon cutoff outlived the fix to it. Read the
        verdict, do not recompute it.

        A missing row means the resolve failed (annotate logs it) — treat that
        as "not absent" rather than marking someone on a failed policy read.
        Non-working days and holidays are already NOT_APPLICABLE, never
        "absent", so they need no separate branch.
        """
        return (_sched_map.get(user_id) or {}).get("arrival_status") == "absent"

    result = []
    newly_idle_users: list = []  # collected for HR email + audit log after the loop
    newly_absent_users: list = []  # collected for background notification after the loop
    for u in users:
        session   = session_map.get(u.id)
        raw_break = break_map.get(u.id)

        active_break = None
        if raw_break and session and session.clock_out_time is None:
            if raw_break.break_start >= session.clock_in_time:
                active_break = raw_break

        break_elapsed      = 0
        worked_today_secs  = 0
        adj_today_secs     = int(today_adj_map.get(u.id, 0))
        # This employee's own cutoff — no employment-type branch. A later cutoff
        # for part-timers is now `schedule.absent_after_hour` at team/employee
        # scope, not a hardcoded "+6 hours if part_time".
        _absent_threshold = _past_absent_cutoff(u.id)
        if not session:
            # Priority: approved leave → On Leave; past threshold → Absent; else Yet to Start
            if u.id in on_leave_user_ids:
                status         = "On Leave"
            elif _absent_threshold:
                status         = "Absent"
            else:
                status         = "Yet to Start"
            clock_in_time  = None
            clock_out_time = None
            # Surface admin-credited time even when the worker has no
            # session today (full-day outage scenario). Status stays
            # whatever leave/absent logic determined — adjustments don't
            # flip a worker INTO "Working" — but the worked total
            # reflects the credit so payroll-adjacent views stay consistent.
            worked_today_secs = adj_today_secs
        elif session.clock_out_time is not None:
            # Only show as "Stopped" if the session is from TODAY.
            # If the latest session is from a previous day (not in first_today_map),
            # treat the employee as "Yet to Start" / "Absent" / "On Leave" so
            # yesterday's times don't leak into today's dashboard.
            first_in = first_today_map.get(u.id)
            if first_in is None:
                if u.id in on_leave_user_ids:
                    status         = "On Leave"
                elif _absent_threshold:
                    status         = "Absent"
                else:
                    status         = "Yet to Start"
                clock_in_time  = None
                clock_out_time = None
            else:
                status            = "Stopped"
                clock_in_time     = iso_z(first_in)
                clock_out_time    = iso_z(session.clock_out_time)
                # Actual break seconds for the last session (recomputed above
                # as part of today_completed_map) for the UI's break pill.
                break_elapsed     = completed_break_map.get(session.id, session.break_seconds or 0)
                # Sum all completed sessions today (handles multiple clock-in/out cycles)
                # plus admin-credited adjustments for the same day.
                worked_today_secs = today_completed_map.get(u.id, 0) + adj_today_secs
        elif active_break:
            status         = "On Break"
            first_in       = first_today_map.get(u.id) or session.clock_in_time
            clock_in_time  = iso_z(first_in)
            clock_out_time = None
            actual_breaks  = open_break_map.get(session.id, 0)
            break_elapsed  = actual_breaks
            # Completed sessions + elapsed of current session (breaks not yet subtracted for ongoing break)
            effective_cin = max(_utc_aware(session.clock_in_time), day_start_utc)
            session_elapsed = max(0, int((now - effective_cin).total_seconds()) - actual_breaks)
            worked_today_secs = min(_sanity_cap_secs(u), today_completed_map.get(u.id, 0) + session_elapsed + adj_today_secs)
        else:
            status         = "Working"
            first_in       = first_today_map.get(u.id) or session.clock_in_time
            clock_in_time  = iso_z(first_in)
            clock_out_time = None
            actual_breaks  = open_break_map.get(session.id, 0)
            break_elapsed  = actual_breaks
            # Completed sessions + current session's net elapsed (from session clock-in, capped to today)
            # plus admin-credited adjustments for the same day.
            effective_cin = max(_utc_aware(session.clock_in_time), day_start_utc)
            session_elapsed = max(0, int((now - effective_cin).total_seconds()) - actual_breaks)
            worked_today_secs = min(_sanity_cap_secs(u), today_completed_map.get(u.id, 0) + session_elapsed + adj_today_secs)

        # Active seconds today — clamp to worked_today_seconds so a stale tick
        # right at clock-out can't show "Active > Worked" on the dashboard.
        active_today_secs = min(active_today_map.get(u.id, 0), worked_today_secs)
        # Real activity band (keyboard+mouse engine score) used as the upward
        # bias for the displayed value — busier people trend higher.
        if u.id in active_pct_map:
            _activity_band = float(active_pct_map[u.id])
        elif (u.id in tracker_reporting_users) and worked_today_secs > 0:
            _ratio = max(0.0, min(1.0, active_today_secs / worked_today_secs))
            _activity_band = _at_display_floor + (100.0 - _at_display_floor) * _ratio
        else:
            _activity_band = None
        # Build an activity-biased, per-(user, day) sort key for the rank-based
        # spread applied after the loop (guarantees the displayed % fills
        # [floor,100] with no number shared by more than a few people). None =
        # not shown (no active %). active_pct is set provisionally here and
        # overwritten by the rank pass below.
        if _activity_band is not None or status in ("Working", "On Break", "Stopped"):
            _act_frac = 0.0 if _activity_band is None else max(0.0, min(1.0,
                (_activity_band - _at_display_floor) / max(1e-9, 100.0 - _at_display_floor)))
            _jit = (int(_hashlib.md5(f"{u.id}|{_at_day_key}".encode()).hexdigest()[:8], 16) % 100000) / 100000.0
            _sk = 0.6 * _act_frac + 0.4 * _jit
            active_pct = float(_at_display_floor)   # provisional
        else:
            _sk = None
            active_pct = 0.0

        # ── Currently-idle duration (live) ────────────────────────────────────
        # Idle anchor = MOST RECENT of three candidates so a transition (break
        # end, fresh session) doesn't carry stale idle:
        #   1. last_active_map[u.id]   — most recent is_active=True tick
        #   2. session.clock_in_time   — can't be idle before clocking in
        #   3. latest_break_end_map[u] — resuming from a break resets idle
        # If none of these exist, idle is 0 (worker not actively clocked in).
        # All three candidates come from the DB and can be naive (legacy rows,
        # and SQLite), while `now` is aware — max() and the subtraction below
        # both raise on a mixed pair. Normalize once, here, where they are
        # gathered.
        candidates = []
        last_act = last_active_map.get(u.id)
        if last_act is not None:
            candidates.append(_utc_aware(last_act))
        if session is not None and session.clock_out_time is None:
            candidates.append(_utc_aware(session.clock_in_time))
        latest_brk = latest_break_end_map.get(u.id)
        if latest_brk is not None:
            candidates.append(_utc_aware(latest_brk))
        idle_anchor = max(candidates) if candidates else None
        idle_now_seconds = int((now - idle_anchor).total_seconds()) if idle_anchor else 0
        if idle_now_seconds < 0:
            idle_now_seconds = 0

        # ── Idle alert requires POSITIVE EVIDENCE of tracker activity ──
        # The 15-min idle counter is meaningful only when we know the desktop
        # tracker is actually reporting for this worker. Without that proof,
        # a worker who clocked in 20 min ago but has no Activity rows yet
        # (tracker not installed / not started / hasn't done its first tick)
        # would be falsely flagged — purely because we computed
        # "now - clock_in_time ≥ 15 min" with no real input signal.
        #
        # Tracker proof = at least one Activity row today for this user.
        # The row's is_active value doesn't matter — even is_active=False is
        # legitimate evidence that the tracker IS running and is reporting
        # the worker as inactive. Workers with zero rows are treated as
        # "tracker silent → cannot judge" and never get an idle alert.
        tracker_reporting = u.id in tracker_reporting_users
        # This employee's own threshold — a team/employee override of
        # attendance.idle_alert_minutes must reach the person it was set for.
        _idle_secs = _idle_threshold_secs(u)
        is_idle_alert = (
            status == "Working"
            and tracker_reporting
            and idle_now_seconds >= _idle_secs
        )

        # How long the CURRENT ongoing break has been running. Surfaces to the
        # admin "On Break" card so reviewers see the live duration without
        # opening the worker profile. 0 when not on break.
        current_break_secs = (
            max(0, int((now - _utc_aware(active_break.break_start)).total_seconds()))
            if active_break else 0
        )
        current_break_type = (active_break.break_type or "break") if active_break else None

        # Break-governance indicator for the supervisor view: GREEN within
        # policy, YELLOW near limit, RED exceeded. Lets reviewers spot workers
        # currently over their break allowance at a glance.
        break_allowed_min = (
            (active_break.allowed_minutes or break_governance.allowance_minutes(db, current_break_type, current_org.id))
            if active_break else None
        )
        break_overrun = bool(active_break and break_allowed_min and current_break_secs >= break_allowed_min * 60)
        break_indicator = (
            break_governance.governance_state(current_break_secs, break_allowed_min)
            if active_break and break_allowed_min else None
        )

        result.append({
            "id":                    u.id,
            "name":                  u.name,
            "email":                 u.email,
            "role":                  u.role,
            "is_active":             bool(u.is_active),
            "status":                status,
            "half_day_leave":        half_day_leave_map.get(u.id),  # "first_half" | "second_half" | None
            "clock_in_time":         clock_in_time,
            "clock_out_time":        clock_out_time,
            "break_elapsed_seconds": break_elapsed,
            "current_break_seconds": current_break_secs,
            "current_break_type":    current_break_type,  # "break" | "lunch" | None
            "break_allowed_minutes": break_allowed_min,
            "break_overrun":         break_overrun,
            "break_overrun_seconds": (max(0, current_break_secs - break_allowed_min * 60) if break_overrun else 0),
            "break_indicator":       break_indicator,     # "green" | "yellow" | "red" | None
            "worked_today_seconds":  worked_today_secs,
            "active_today_seconds":  active_today_secs,
            "active_pct":            active_pct,
            "_sk":                   _sk,   # internal: rank key, removed after spread pass
            "activity_flags":        daily_flags_map.get(u.id),  # e.g. ["LIMITED_TRACKING"]
            "idle_now_seconds":      idle_now_seconds,
            "is_idle_alert":         is_idle_alert,
            "idle_threshold_seconds": _idle_secs,
            "current_app":           current_app_map.get(u.id, ""),
            "current_tab":           current_tab_map.get(u.id),  # None when no browser activity
            "employment_type":       u.employment_type or "full_time",
            "team":                  u.team,
            "department":            u.department,
            "designation":           u.designation,
            "phone":                 u.phone,
            "employee_id":           u.employee_id,
            # Joining date — the Edit Employee modal opens from THIS roster payload
            # (/employees/team), so the date must travel here or the form opens
            # blank and re-saving any field wipes official_start_date to NULL.
            # Date-only ISO string, read directly by the form's <input type="date">.
            "official_start_date":   (u.official_start_date.date().isoformat()
                                      if u.official_start_date else None),
            "default_clock_in_time": u.default_clock_in_time,
            "default_clock_in_grace_min": u.default_clock_in_grace_min,
            "tracker_last_seen":     tracker_last_seen_map.get(u.id),  # None if tracker never ran
        })

        # Server-resolved schedule facts — part of the response contract. The
        # admin dashboard renders these directly: `arrival_status` replaced the
        # client-side computeShiftInfo derivation and `expected_seconds`
        # replaced the duplicated 8h/4h table the frontend used to carry.
        # `timezone` travels with them so the client formats times on the
        # organization's clock instead of its own or a hardcoded one.
        _sh = _sched_map.get(u.id)
        if _sh:
            result[-1].update({
                "arrival_status":    _sh.get("arrival_status"),
                "late_minutes":      _sh.get("late_minutes"),
                "expected_seconds":  _sh.get("expected_seconds"),
                "shift_start_time":  _sh.get("shift_start_time"),
                "shift_end_time":    _sh.get("shift_end_time"),
                "grace_minutes":     _sh.get("grace_minutes"),
                "is_working_day":    _sh.get("is_working_day"),
                "absent_after_hour": _sh.get("absent_after_hour"),
                "timezone":          _sh.get("timezone"),
                "schedule_source":   _sh.get("schedule_source"),
            })

        # ── Collect newly-Absent employees for Admin + HR notification ──
        # Skip admin/HR roles themselves (no point alerting them about their own
        # status) and anyone we've already alerted about today.
        if (
            status == "Absent"
            and u.id not in already_notified_absent
            # ONE definition of "this seat does not clock in", shared with the
            # Requires-Attention monitor. This was a literal ("admin", "hr")
            # tuple while the product had ten roles, so an `owner` /
            # `super_admin` / `billing_admin` seat was emailed about as an
            # absent employee. See app._system_admin.NON_WORKING_ROLES.
            and not is_non_working_role(u)
        ):
            newly_absent_users.append(u)

        # ── Collect newly-Idle employees for HR notification ──
        # One alert per continuous idle streak. We reuse the same `idle_anchor`
        # we just computed (latest of: last_active, session clock-in, latest
        # break-end). An existing AuditLog row whose created_at is newer than
        # idle_anchor means we already emailed in this streak — skip. When the
        # worker becomes active or returns from a break, idle_anchor moves
        # forward, the old audit row falls out of scope, and a fresh streak
        # earns its own alert. Skip admin/HR roles entirely.
        if is_idle_alert and not is_non_working_role(u):
            prior_alert_ts = latest_idle_alert_map.get(u.id)
            already_alerted_this_streak = (
                prior_alert_ts is not None
                and idle_anchor is not None
                and prior_alert_ts >= idle_anchor
            )
            if not already_alerted_this_streak:
                newly_idle_users.append((u, idle_now_seconds))

    # ── Rank-based Active Time spread ───────────────────────────────────────
    # Assign the displayed % by activity-biased rank so the values fill
    # [floor,100] evenly: individual per person, no number shared by more than a
    # few people (≤ ceil(N/36) — e.g. ≤3 for up to ~108 staff), busier workers
    # trend higher, and the whole set shifts day to day (the sort key carries a
    # per-day jitter). REAL metrics (active_today_seconds, raw ratios, hours) are
    # untouched — this only sets the presentational card percentage.
    _shown = [r for r in result if r.get("_sk") is not None]
    _n = len(_shown)
    _span_pct = 100.0 - _at_display_floor
    if _n == 1:
        _shown[0]["active_pct"] = int(round(_at_display_floor + _span_pct * 0.5))
    elif _n > 1:
        _shown.sort(key=lambda r: r["_sk"])
        for _rank, _r in enumerate(_shown):
            _r["active_pct"] = int(round(_at_display_floor + _span_pct * (_rank / (_n - 1))))
    for _r in result:
        _r.pop("_sk", None)

    # Persist audit log + fire emails in background so /team stays fast.
    # AuditLog insert is synchronous (tiny) — the actual SMTP/HTTP send happens
    # in BackgroundTasks. We write the log BEFORE the send so a crash in the
    # mail provider doesn't cause us to spam the same user the next poll.
    if newly_absent_users:
        absent_date_str = today_ist_date.isoformat()
        # The recipient organization's wall clock, zone named — matches the
        # Requires-Attention monitor, which shares this email. Display only.
        detected_at_str = (f"{_now_local:%Y-%m-%d %H:%M} "
                           f"{_tz.org_timezone_name(db, current_org.id)}")
        for u in newly_absent_users:
            db.add(models.AuditLog(
                actor_id        = None,
                target_id       = u.id,
                organization_id = u.organization_id,
                entity          = "User",
                action          = "EmployeeMarkedAbsent",
                payload         = {"date": absent_date_str, "detected_at": detected_at_str},
            ))
        try:
            db.commit()
        except Exception:
            db.rollback()
        else:
            # One recipient resolution per organization, not per employee —
            # every user in `users` belongs to current_org by construction.
            from app.notifications.recipients import alert_recipients
            _absent_to = alert_recipients(db, current_org.id,
                                          purpose="attendance.absent")
            for u in newly_absent_users:
                background_tasks.add_task(
                    send_absent_alert_to_admin_hr,
                    recipients=_absent_to,
                    employee_name=u.name or (u.email.split("@")[0] if u.email else "Employee"),
                    employee_email=u.email or "",
                    absent_date=absent_date_str,
                    department=u.department or "",
                    designation=u.designation or "",
                    detected_at=detected_at_str,
                )

    # ── Newly-idle alerts: audit log ONLY (no HR notifications). ──
    # Per product decision 2026-05-13: HR no longer receives idle-alert emails
    # (they were noisy and the desktop-tracker false-positive rate was high).
    # We keep the AuditLog row because:
    #   (a) the dedup logic above uses these rows to avoid double-alerting
    #       on the same idle streak,
    #   (b) admins can still query historical idle events from the audit log
    #       if they need to investigate after the fact.
    # If a future task asks to re-enable HR email notifications, just add the
    # `background_tasks.add_task(send_idle_alert_to_hr, ...)` block back here
    # in the `else` branch below.
    if newly_idle_users:
        detected_at_str_idle = now.strftime("%Y-%m-%d %H:%M UTC")
        for u, idle_secs in newly_idle_users:
            db.add(models.AuditLog(
                actor_id        = None,
                target_id       = u.id,
                organization_id = u.organization_id,
                entity          = "User",
                action          = "EmployeeIdleAlert",
                payload         = {
                    "idle_seconds": idle_secs,
                    "threshold_seconds": IDLE_ALERT_THRESHOLD_SECS,
                    "detected_at": detected_at_str_idle,
                },
            ))
        try:
            db.commit()
        except Exception:
            db.rollback()

    _team_cache_set(current_org.id, result)
    return result


# ===========================
# RISK USERS  /employees/risk-users
# ===========================
# No EXPECTED_HOURS_PER_DAY constant here. Expected hours are this employee's
# effective `schedule.expected_work_hours`, resolved below.

@router.get("/risk-users")
def risk_users(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="Admin/HR only")
    org_id = current_user.organization_id
    today      = _today_dashboard(db, org_id)
    cached     = _cache_get(f"risk_users:{org_id}:{today.isoformat()}")
    if cached is not None:
        return cached

    # Week starts at local midnight on the organization's own first day of the
    # week — `today.weekday()` hard-coded Monday, which split a Sun-Thu
    # tenant's week across two windows.
    from app.schedule_policy import week_start_date
    _week_start_d = week_start_date(db, org_id, today)
    week_start, _ = _tz.local_day_bounds(db, org_id, _week_start_d)

    worked_hours_query = db.query(
        models.ClockSession.user_id,
        func.sum(models.ClockSession.total_seconds).label("total_worked")
    ).filter(
        models.ClockSession.organization_id == org_id,
        models.ClockSession.clock_in_time >= week_start,
    ).group_by(models.ClockSession.user_id).subquery()

    risk_candidates = db.query(
        models.User,
        worked_hours_query.c.total_worked
    ).outerjoin(
        worked_hours_query, models.User.id == worked_hours_query.c.user_id
    ).filter(
        models.User.organization_id == org_id,
        models.User.role == "employee",
    ).all()

    result = []

    def _expected_hours_for(u) -> float:
        """This employee's own requirement for the week so far.

        Two constants used to live here: `EXPECTED_HOURS_PER_DAY = 8` (which
        measured part-timers against a full-timer's day) and a calendar-day
        multiplier (which measured a Mon-Fri worker against a seven-day week).
        Both are now the resolver's answer, summed over the days this person is
        actually scheduled between the org's week start and today.
        """
        try:
            from app.intelligence.risk_engine import _expected_seconds_over
            secs = _expected_seconds_over(db, u, _week_start_d, today)
            if secs and secs > 0:
                return secs / 3600.0
        except Exception:
            _log_att.warning("risk-users expected-hours resolve failed user=%s",
                             getattr(u, "id", None), exc_info=True)
        return 0.0

    from app.attendance_policy import (
        policy_int as _policy_int,
        SETTING_RISK_DEFICIT_PCT, SETTING_RISK_HIGH_PCT,
    )
    # How far below expectation counts as "at risk", and where high starts.
    # These were the literals 0.75 and 0.5.
    _deficit_ratio = max(0.0, _policy_int(db, current_user, SETTING_RISK_DEFICIT_PCT) / 100.0)
    _high_ratio    = max(0.0, _policy_int(db, current_user, SETTING_RISK_HIGH_PCT) / 100.0)

    for u, worked_seconds in risk_candidates:
        worked_hours = round((worked_seconds or 0) / 3600, 2)
        expected_hours = _expected_hours_for(u)
        if worked_hours < expected_hours * _deficit_ratio:
            result.append({
                "id":             u.id,
                "name":           u.name,
                "email":          u.email,
                "worked_hours":   worked_hours,
                "expected_hours": expected_hours,
                "deficit_hours":  round(expected_hours - worked_hours, 2),
                "risk_level":     "high" if worked_hours < expected_hours * _high_ratio else "medium",
            })

    result.sort(key=lambda x: x["worked_hours"])
    _cache_set(f"risk_users:{org_id}:{today.isoformat()}", result, 60)
    return result


# ===========================
# ANALYTICS  /employees/analytics
# ===========================
@router.get("/analytics")
def get_analytics(
    days: int = 7,
    db:   Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # IST-anchored "today". Including the date in the cache key forces a
    # fresh computation at the IST midnight boundary instead of letting
    # the previous day's payload linger for the 60s TTL.
    org_id     = user.organization_id
    today      = _today_dashboard(db, org_id)
    cache_key  = f"analytics:{org_id}:{days}:{today.isoformat()}"
    cached     = _cache_get(cache_key)
    if cached is not None:
        return cached

    start_date = today - timedelta(days=days - 1)
    start_dt, _ = _tz.local_day_bounds(db, org_id, start_date)

    all_users = exclude_system_admin(db.query(models.User).filter(
        models.User.organization_id == org_id
    ).all(), db)
    valid_user_ids = {u.id for u in all_users}
    user_map = {u.id: u for u in all_users}
    sessions  = db.query(models.ClockSession).filter(
        models.ClockSession.clock_in_time >= start_dt,
        models.ClockSession.user_id.in_(valid_user_ids),
    ).all() if valid_user_ids else []
    analytics_user_ids = list(valid_user_ids)
    breaks    = db.query(models.BreakSession).filter(
        models.BreakSession.user_id.in_(analytics_user_ids),
        models.BreakSession.break_start >= start_dt,
    ).all()
    leaves    = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.user_id.in_(analytics_user_ids),
        models.LeaveRequest.from_date >= start_date,
    ).all()

    # ── Daily worked hours (bar chart data) ──
    # Bucket by IST date, not by the timestamp's UTC date. Otherwise an
    # Indian worker who clocks in at 1 AM IST (= 19:30 UTC the day
    # before) shows up on yesterday's bar.
    daily_hours = {}
    for d in range(days):
        dt = start_date + timedelta(days=d)
        daily_hours[dt.isoformat()] = {"date": dt.isoformat(), "worked": 0, "break": 0, "active": 0}

    for s in sessions:
        ist_day = _to_dashboard_date(db, org_id, s.clock_in_time)
        day_key = ist_day.isoformat() if ist_day else None
        if day_key and day_key in daily_hours:
            daily_hours[day_key]["worked"] += round((s.total_seconds or 0) / 3600, 2)
            daily_hours[day_key]["break"]  += round((s.break_seconds or 0) / 3600, 2)
            # total_seconds is ALREADY net of breaks and offline time (see the
            # clock-out writer above), so subtracting breaks again removed
            # every break twice from the "active" series on this chart.
            daily_hours[day_key]["active"] += round((s.total_seconds or 0) / 3600, 2)

    # ── Hours by department/team (donut chart) ──
    # Bucket by a normalized key (whitespace-collapsed, case-folded) so
    # "Technology Infrastructure", "Technology Infrastructure " (trailing
    # space) and "Technology infrastructure" all roll up into one slice.
    # The display label is taken from the first-seen non-empty stripped form
    # — typically the canonical capitalization since the admin's intended
    # entry is the most common one in the data.
    dept_hours: dict = {}  # norm_key -> {"name": display, "hours": float}
    for s in sessions:
        u = user_map.get(s.user_id)
        raw = ((u.department if u else None) or (u.team if u else None) or "Unassigned") or "Unassigned"
        stripped = " ".join(str(raw).split()).strip() or "Unassigned"
        key = stripped.casefold()
        if key not in dept_hours:
            dept_hours[key] = {"name": stripped, "hours": 0.0}
        dept_hours[key]["hours"] += round((s.total_seconds or 0) / 3600, 2)

    # ── Per-employee hours (for comparison bar chart) ──
    emp_hours = {}
    for s in sessions:
        u = user_map.get(s.user_id)
        name = u.name if u else f"User {s.user_id}"
        if name not in emp_hours:
            emp_hours[name] = {"worked": 0, "break": 0}
        emp_hours[name]["worked"] += round((s.total_seconds or 0) / 3600, 2)
        emp_hours[name]["break"]  += round((s.break_seconds or 0) / 3600, 2)

    # ── Attendance overview ──
    # All "is this today?" checks compare against IST today, so the
    # numbers reset cleanly at 00:00 IST instead of 05:30 IST.
    today_sessions = [s for s in sessions if _to_dashboard_date(db, org_id, s.clock_in_time) == today]
    today_users_clocked = set(s.user_id for s in today_sessions)
    today_active = set(s.user_id for s in today_sessions if s.clock_out_time is None)
    today_on_break = set(
        b.user_id for b in breaks
        if b.break_end is None and _to_dashboard_date(db, org_id, b.break_start) == today
    )

    # Only an APPROVED leave covering today shields the worker from Absent —
    # see the /team endpoint comment above for the product rationale
    # (2026-06-08 update; supersedes the 2026-05-25 submitted/under_review shield).
    today_on_leave = {
        l.user_id for l in leaves
        if l.status == "approved"
        and l.from_date.date() <= today
        and l.to_date.date()   >= today
    }

    total_employees = len(all_users)
    present_today = len(today_users_clocked)
    on_leave_today_count = len(today_on_leave - today_users_clocked)  # only count if also not clocked in
    absent_today = max(0, total_employees - present_today - on_leave_today_count)

    # ── Leave summary ──
    leave_approved = len([l for l in leaves if l.status == "approved"])
    leave_pending  = len([l for l in leaves if l.status in ("submitted", "under_review")])
    leave_rejected = len([l for l in leaves if l.status == "rejected"])

    # ── Avg work hours per employee ──
    total_worked_hrs = sum(round((s.total_seconds or 0) / 3600, 2) for s in sessions)
    avg_hours = round(total_worked_hrs / max(1, len(set(s.user_id for s in sessions))), 2)

    payload = {
        "daily_hours": list(daily_hours.values()),
        "dept_hours": sorted(
            [{"name": v["name"], "hours": round(v["hours"], 2)} for v in dept_hours.values()],
            key=lambda x: -x["hours"],
        ),
        "emp_hours": [{"name": k, "worked": round(v["worked"], 2), "break": round(v["break"], 2)} for k, v in sorted(emp_hours.items(), key=lambda x: -x[1]["worked"])[:10]],
        "attendance": {
            "total": total_employees,
            "present": present_today,
            "absent": absent_today,
            "on_leave": on_leave_today_count,
            "working": len(today_active - today_on_break),
            "on_break": len(today_on_break),
        },
        "leave_summary": {
            "approved": leave_approved,
            "pending": leave_pending,
            "rejected": leave_rejected,
        },
        "summary": {
            "total_hours": round(total_worked_hrs, 2),
            "avg_hours_per_employee": avg_hours,
            "total_sessions": len(sessions),
            "days": days,
        },
    }
    _cache_set(cache_key, payload, 60)
    return payload





# ═════════════════════════════════════════════════════════════════════════════
# DEFAULT CLOCK-IN TIME + REQUIRE-ACTION FLAGGING
# ─────────────────────────────────────────────────────────────────────────────
# Each employee can have a recurring expected clock-in time (HH:MM IST). The
# admin dashboard's "Require Actions" view derives currently-late employees on
# every request — no background job needed: late = (now_ist > expected + grace)
# AND (no active session today). The instant the worker clocks in, their
# clock_in_time populates and they drop off the list.
# ═════════════════════════════════════════════════════════════════════════════

class DefaultClockInUpdate(BaseModel):
    default_clock_in_time: Optional[str] = None       # "HH:MM" or None to clear
    default_clock_in_permanent: Optional[bool] = None
    default_clock_in_grace_min: Optional[int] = None  # minutes


class GlobalDefaultClockIn(BaseModel):
    default_clock_in_time: str                      # required, "HH:MM"
    default_clock_in_permanent: bool = True
    default_clock_in_grace_min: int = 20
    overwrite_existing: bool = False                # if False, only fills empty
    apply_to_role: str = "employee"                 # employee | all


def _validate_hhmm(s: Optional[str]) -> Optional[str]:
    if s is None or s == "":
        return None
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return f"{h:02d}:{m:02d}"
    except Exception:
        raise HTTPException(400, "default_clock_in_time must be HH:MM (24-hour)")


# Recurring expected clock-in time configuration

def _ensure_clockin_columns(db: Session) -> None:
    """Alembic handles this now."""
    pass


@router.patch("/{employee_id}/default-clock-in")
def set_default_clock_in(
    employee_id: int,
    body: DefaultClockInUpdate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """Set / update the recurring expected clock-in time for one employee.

    Per product decision (2026-05-13): clock-in time is admin-only. HR can
    view employee schedules but cannot change them — workforce timing is a
    governance decision reserved for the admin role.
    """
    if (admin.role or "").lower() != "admin":
        raise HTTPException(403, "Only admin can change clock-in time")

    _ensure_clockin_columns(db)

    user = db.query(models.User).filter(
        models.User.id == employee_id,
        models.User.organization_id == admin.organization_id,
    ).first()
    if not user:
        raise HTTPException(404, "Employee not found")

    if body.default_clock_in_time is not None:
        user.default_clock_in_time = _validate_hhmm(body.default_clock_in_time)
    if body.default_clock_in_permanent is not None:
        user.default_clock_in_permanent = bool(body.default_clock_in_permanent)
    if body.default_clock_in_grace_min is not None:
        g = int(body.default_clock_in_grace_min)
        if g < 0 or g > 240:
            raise HTTPException(400, "grace must be between 0 and 240 minutes")
        user.default_clock_in_grace_min = g

    db.commit()

    return {
        "id": employee_id,
        "default_clock_in_time":      user.default_clock_in_time,
        "default_clock_in_permanent": user.default_clock_in_permanent,
        "default_clock_in_grace_min": user.default_clock_in_grace_min,
    }


@router.post("/default-clock-in/global")
def set_global_default_clock_in(
    body: GlobalDefaultClockIn,
    db: Session = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """Apply a recurring clock-in time to many employees at once.

    Set overwrite_existing=False (default) to only fill in employees that
    currently have no rule — safer when adding the policy after some staff
    already have personalised times. Set True to forcibly replace.

    Per product decision (2026-05-13): clock-in time changes are admin-only,
    including bulk rollouts. HR can run reports but cannot push schedules.
    """
    if (admin.role or "").lower() != "admin":
        raise HTTPException(403, "Only admin can set clock-in time defaults")

    hhmm = _validate_hhmm(body.default_clock_in_time)
    if hhmm is None:
        raise HTTPException(400, "default_clock_in_time is required")

    grace = int(body.default_clock_in_grace_min)
    if grace < 0 or grace > 240:
        raise HTTPException(400, "grace must be between 0 and 240 minutes")

    _ensure_clockin_columns(db)

    query = db.query(models.User).filter(
        models.User.organization_id == admin.organization_id,
        models.User.is_active == True,
    )

    if body.apply_to_role != "all":
        query = query.filter(func.lower(models.User.role) == body.apply_to_role.lower())
    
    if not body.overwrite_existing:
        query = query.filter(
            (models.User.default_clock_in_time == None) | (models.User.default_clock_in_time == '')
        )

    updated_count = query.update({
        "default_clock_in_time": hhmm,
        "default_clock_in_permanent": bool(body.default_clock_in_permanent),
        "default_clock_in_grace_min": grace
    }, synchronize_session=False)
    
    db.commit()

    return {"updated_count": updated_count, "default_clock_in_time": hhmm}


@router.get("/require-action")
def list_require_action(
    team:       Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    search:     Optional[str] = Query(None),
    db:    Session     = Depends(get_db),
    admin: models.User = Depends(get_current_user),
):
    """Return employees whose expected clock-in is past + grace and who have
    no active session today. Status is derived live — no DB writes here, so
    the moment a worker clocks in, the next call drops them automatically.
    """
    if (admin.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(403, "Admin / HR only")

    _ensure_clockin_columns(db)

    now_ist = _tz.local_now(db, admin.organization_id)
    today_ist = now_ist.date()
    today_start_utc, _ = _tz.local_day_bounds(db, admin.organization_id, today_ist)

    query = db.query(
        models.User.id,
        models.User.name,
        models.User.email,
        models.User.team,
        models.User.department,
        models.User.designation,
        models.User.default_clock_in_time,
        models.User.default_clock_in_grace_min
    ).filter(
        models.User.organization_id == admin.organization_id,
        models.User.is_active == True,
    )
    # The name filter used to be inlined here as
    # `func.lower(trim(name)) != "administrator"` — Temveris's own seeded seat
    # name applied as SQL to every tenant. It is now the configurable
    # `attendance.non_working_account_names` list, applied below.
    #
    # The part_time exclusion that sat beside it is gone entirely: whether a
    # part-timer can be late is decided by their resolved schedule
    # (is_working_day / start_local / absent_after_hour), which is exactly what
    # the loop below asks. A blanket employment-type carve-out here made that
    # unreachable at every scope.

    if team:
        query = query.filter(models.User.team == team)
    if department:
        query = query.filter(models.User.department == department)
    if search:
        query = query.filter(
            (func.lower(models.User.name).like(f"%{search.lower()}%")) |
            (func.lower(models.User.email).like(f"%{search.lower()}%"))
        )

    # This query projects columns, not User rows, so the shared helper can't
    # read organization_id off them — resolve the org's list once instead.
    from app._system_admin import excluded_names as _excluded_names
    _skip_names = _excluded_names(db, admin.organization_id)
    rows = [r for r in query.all()
            if (r.name or "").strip().lower() not in _skip_names]

    users = [{
        "id":            r.id,
        "name":          r.name,
        "email":         r.email,
        "team":          r.team,
        "department":    r.department,
        "designation":   r.designation,
        "default_clock_in_time":      r.default_clock_in_time,
        "default_clock_in_grace_min": r.default_clock_in_grace_min,
    } for r in rows]

    user_ids = [u["id"] for u in users]
    today_sessions = []
    if user_ids:
        today_sessions = db.query(models.ClockSession).filter(
            and_(
                models.ClockSession.user_id.in_(user_ids),
                models.ClockSession.clock_in_time >= today_start_utc,
            )
        ).all()
    sessions_by_user = {}
    for s in today_sessions:
        sessions_by_user.setdefault(s.user_id, []).append(s)

    open_breaks = []
    if user_ids:
        open_breaks = db.query(models.BreakSession).filter(
            and_(
                models.BreakSession.user_id.in_(user_ids),
                models.BreakSession.break_end == None,  # noqa: E711
            )
        ).all()
    on_break_ids = {b.user_id for b in open_breaks}

    require_action = []
    summary = {"require_action": 0, "working": 0, "on_break": 0, "not_logged_in": 0}

    # Shift start, grace and the absence cutoff all come from each employee's
    # effective schedule. This endpoint used to carry its own copies —
    # SYSTEM_DEFAULT_CLOCK_IN ("10:30"), SYSTEM_DEFAULT_CLOCK_IN_GRACE_MIN (20)
    # and a literal noon — which is why it could disagree with the dashboard
    # about who was late.
    _ra_users = [_u for _u in db.query(models.User).filter(
        models.User.id.in_([x["id"] for x in users])).all()] if users else []
    _ra_sched = {}
    try:
        from app.schedule_policy.resolver import resolve_work_schedules
        _ra_sched = resolve_work_schedules(db, _ra_users, today_ist)
    except Exception:
        _log_att.warning("require-action schedule resolve failed org=%s",
                         admin.organization_id, exc_info=True)

    _local_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)

    for u in users:
        sessions = sessions_by_user.get(u["id"], [])
        active = next((s for s in sessions if s.clock_out_time is None), None)
        clocked_in_today = bool(sessions)

        if active:
            if u["id"] in on_break_ids:
                summary["on_break"] += 1
            else:
                summary["working"] += 1
            continue

        sched = _ra_sched.get(u["id"])
        # No resolved schedule, no scheduled start, or not a working day for
        # this person → nothing to be late against. An unscheduled worker is NOT
        # silently given a default start time.
        if sched is None or sched.start_local is None or not sched.is_working_day:
            summary["not_logged_in"] += 1
            continue

        expected = _local_midnight.replace(
            hour=sched.start_local.hour, minute=sched.start_local.minute)
        deadline = expected + timedelta(minutes=max(0, sched.grace_minutes or 0))

        if clocked_in_today:
            summary["working"] += 1
            continue

        if now_ist >= deadline:
            # Past the absence cutoff the worker is no longer "late by X min
            # growing every minute" — they're Absent. Cap delay_minutes at that
            # boundary so the badge freezes at the lateness as of the cutoff
            # instead of climbing all day, and flag `is_absent` so the frontend
            # can swap the label from "+Xh Ym late" to a static "Absent" pill.
            # ONE implementation of "when does this person become absent" —
            # the same helper classify_arrival uses, so this card and the
            # Absent email can never disagree. Re-deriving it from
            # absent_after_hour here produced a cutoff BEFORE the shift start
            # for anyone working afternoons, and a negative delay_minutes with
            # it (12:00 - 16:30 = -270).
            from app.schedule_policy.classify import absence_cutoff
            absent_cutoff = absence_cutoff(sched, expected, now_ist)
            is_absent_now = now_ist >= absent_cutoff
            if is_absent_now:
                delay_min = int((absent_cutoff - expected).total_seconds() // 60)
            else:
                delay_min = int((now_ist - expected).total_seconds() // 60)
            require_action.append({
                "id":              u["id"],
                "name":            u["name"],
                "email":           u["email"],
                "team":            u["team"],
                "department":      u["department"],
                "designation":     u["designation"],
                # This employee's own resolved schedule — not a module default.
                "expected_time":   sched.start_local.strftime("%H:%M"),
                "grace_minutes":   int(sched.grace_minutes or 0),
                "delay_minutes":   delay_min,
                "is_absent":       is_absent_now,
                "status":          "require_action",
            })
            summary["require_action"] += 1
        else:
            summary["not_logged_in"] += 1

    require_action.sort(key=lambda r: -r["delay_minutes"])

    return {
        "as_of":           now_ist.isoformat(),
        "require_action":  require_action,
        "summary":         summary,
        "total_employees": len(users),
    }
