"""Workspace Settings backend — Tabs 4-10 of the Workspace Settings spec.

Every endpoint here returns LIVE data from the database (or from existing
service modules). No static payloads — that's the spec invariant the user
asked us to honor.

Endpoints:
    GET  /api/workspace/roles                    Roles & Permissions tab
    GET  /api/workspace/permissions              Permission matrix
    POST /api/workspace/roles/{role}/users       Assign a user to a role
    GET  /api/workspace/policies                 Policies tab — aggregated state
    PUT  /api/workspace/policies/{group}         Update a policy block
    GET  /api/workspace/billing-summary          Billing & Subscription tab
    GET  /api/workspace/security                 Security tab — summary
    GET  /api/workspace/security/sessions        Active workspace sessions
    POST /api/workspace/security/sessions/{id}/revoke
    GET  /api/workspace/integrations             Connected Systems tab
    PUT  /api/workspace/integrations/{key}       Mark integration as connected
    GET  /api/workspace/audit                    Audit & Compliance tab
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from app.auth.service import get_current_user
# Service-interface import (ENGINEERING_STANDARDS): the billable-seat definition
# and the "billing requires a real organization" rule live in one place.
from app.billing import service as billing_service
# Same rule for holidays: the role matrix, the country registry and the calendar
# lookup each have exactly one definition, in app/holidays/.
from app.holidays import catalog as holiday_catalog
from app.holidays import permissions as holiday_permissions
from app.holidays import service as holiday_service
from app.commercial.enforcement import (
    shadow_capability as _shadow,
    require_capability,
    require_writable_capability,
)

_log = logging.getLogger("zoiko.workspace")

router = APIRouter(prefix="/api/workspace", tags=["Workspace Settings"])


def _require_admin(user: models.User) -> None:
    if user.role not in ("admin", "hr", "owner", "billing_admin", "super_admin"):
        raise HTTPException(403, "Admin or HR access required")


def _billing_org(user: models.User) -> int:
    """The caller's organization for billing reads.

    Billing is tenant-scoped: there is no shared/default organization. Reuses
    the billing service's rule so "a real organization is required" is defined
    once (service interface, not a cross-context router import).
    """
    try:
        return billing_service.require_org_id(user.organization_id)
    except billing_service.MissingOrganization:
        raise HTTPException(
            400,
            "Your account is not linked to an organization, so billing is unavailable.",
        )


# Roles that only super_admin may assign. Tenant admins (admin, hr, owner)
# cannot elevate a user to these roles via any workspace endpoint.
_SUPER_ADMIN_ONLY_ROLES = {"super_admin", "owner"}


# ──────────────────────────────────────────────────────────────────────────
# §4 — Roles & Permissions
# ──────────────────────────────────────────────────────────────────────────
# The backend owns the permission matrix. The frontend renders what we
# return — no UI-side capability strings.
# Access Governance Center — built-in role catalog.
#
# Every entry is rendered as a card in /api/workspace/access-governance and
# carries two governance signals the UI reads directly:
#   • `tier`     drives the right-side badge ("Owner", "High privilege",
#                 "Moderate", "Scoped", "Read-heavy"). The Access Governance
#                 stat cards count privileged users by tier.
#   • `mfa_tier` drives the lock indicator + the MFA & access controls tab
#                 ("required", "enforced", "recommended", "optional").
#
# `tier` and `mfa_tier` are *spec* fields — they are part of the contract
# with the frontend Access Governance Center and must not be removed without
# also updating WorkspaceSettingsTabs.jsx.
ROLE_CATALOG: List[Dict[str, Any]] = [
    {
        "code": "owner",
        "label": "Workspace Owner",
        "description": "Ultimate accountable owner — billing, legal ownership, emergency access, and final administrative authority.",
        "is_system": True,
        "tier": "owner",
        "mfa_tier": "required",
        "permissions": [
            "workspace.profile.edit", "workspace.transfer", "billing.manage",
            "billing.invoices.view", "billing.payment_methods.manage",
            "roles.manage", "users.manage", "users.delete", "policies.manage",
            "security.manage", "integrations.manage", "audit.view",
            "departments.manage", "data.export",
        ],
    },
    {
        "code": "super_admin",
        "label": "Super Administrator",
        "description": "Broad platform configuration authority. Cannot transfer billing ownership or execute destructive legal actions.",
        "is_system": True,
        "tier": "high",
        "mfa_tier": "enforced",
        "permissions": [
            "workspace.profile.edit", "billing.invoices.view",
            "billing.payment_methods.manage", "billing.manage",
            "roles.manage", "users.manage", "users.delete", "policies.manage",
            "security.manage", "integrations.manage", "audit.view",
            "departments.manage", "data.export",
        ],
    },
    {
        "code": "admin",
        "label": "Workspace Administrator",
        "description": "Operational admin for workspace profile, departments, workers, holidays, leave setup and general configuration.",
        "is_system": True,
        "tier": "moderate",
        "mfa_tier": "recommended",
        "permissions": [
            "workspace.profile.edit", "billing.invoices.view",
            "roles.view", "users.manage", "policies.manage",
            "audit.view", "departments.manage", "data.export",
        ],
    },
    {
        "code": "hr",
        "label": "HR Administrator",
        "description": "Worker records, onboarding/offboarding, leave administration, employment attributes, and HR-facing reporting.",
        "is_system": True,
        "tier": "moderate",
        "mfa_tier": "recommended",
        "permissions": [
            "users.manage", "policies.view", "policies.manage",
            "departments.manage", "audit.view", "data.export",
        ],
    },
    {
        "code": "people_manager",
        "label": "People Manager",
        "description": "Manages assigned teams, approves timesheets and leave. Strictly scoped to assigned departments or direct reports.",
        "is_system": True,
        "tier": "scoped",
        "mfa_tier": "optional",
        "permissions": [
            "self.profile.edit", "self.timesheet.view", "self.leave.submit",
            "policies.view", "audit.view",
        ],
    },
    {
        "code": "payroll_finance_admin",
        "label": "Payroll & Finance Admin",
        "description": "Billing, invoices, finance exports, approved payable time. Separated from HR and disciplinary controls by default.",
        "is_system": True,
        "tier": "high",
        "mfa_tier": "enforced",
        "permissions": [
            "billing.invoices.view", "billing.payment_methods.manage",
            "billing.manage", "data.export", "audit.view",
        ],
    },
    {
        "code": "compliance_audit_officer",
        "label": "Compliance & Audit Officer",
        "description": "Audit trails, retention, evidence packages. Read-only — cannot silently alter source records.",
        "is_system": True,
        "tier": "read_heavy",
        "mfa_tier": "recommended",
        "permissions": [
            "audit.view", "policies.view", "roles.view", "data.export",
        ],
    },
    {
        "code": "it_security_admin",
        "label": "IT & Security Administrator",
        "description": "MFA, SSO, session controls, domain settings, SCIM, device/IP policies, and integration security.",
        "is_system": True,
        "tier": "high",
        "mfa_tier": "enforced",
        "permissions": [
            "security.manage", "integrations.manage", "audit.view",
            "roles.view", "data.export",
        ],
    },
    {
        "code": "billing_admin",
        "label": "Billing Administrator",
        "description": "Subscription, plan, invoices, payment methods, tax profile, seats, credits, and dunning status.",
        "is_system": True,
        "tier": "moderate",
        "mfa_tier": "recommended",
        "permissions": [
            "billing.invoices.view", "billing.payment_methods.manage",
            "billing.manage",
        ],
    },
    {
        "code": "employee",
        "label": "Worker",
        "description": "Standard workspace member — view own data, submit leave, view own timesheet.",
        "is_system": True,
        "tier": "scoped",
        "mfa_tier": "optional",
        "permissions": [
            "self.profile.edit", "self.timesheet.view", "self.leave.submit",
        ],
    },
]

# Tier metadata — drives the badge label, colour, and ordering on the
# Access Governance Center role library cards.
TIER_META: Dict[str, Dict[str, Any]] = {
    "owner":      {"label": "Owner",          "rank": 0},
    "high":       {"label": "High privilege", "rank": 1},
    "moderate":   {"label": "Moderate",       "rank": 2},
    "scoped":     {"label": "Scoped",         "rank": 3},
    "read_heavy": {"label": "Read-heavy",     "rank": 4},
}

# MFA tier metadata — drives the "MFA required / enforced / recommended /
# optional" tag rendered alongside the user-count footer on each role card.
MFA_TIER_META: Dict[str, Dict[str, Any]] = {
    "required":    {"label": "MFA required",    "icon": "lock",   "rank": 0},
    "enforced":    {"label": "MFA enforced",    "icon": "lock",   "rank": 1},
    "recommended": {"label": "MFA recommended", "icon": "shield", "rank": 2},
    "optional":    {"label": "Optional",        "icon": "shield", "rank": 3},
}

# Roles whose `tier` is considered privileged. Used by the Access Governance
# Center "Privileged access" stat card — `tier` lookup keeps that count
# stable when the catalog evolves (vs. hard-coding role codes).
_PRIVILEGED_TIERS = {"owner", "high"}

CUSTOM_ROLES_KEY = "workspace.custom_roles_json"
HOLIDAY_PROFILES_KEY = "workspace.public_holiday_profiles_json"

# Permission catalog — the union of every permission key referenced above.
_PERMISSION_DESCRIPTIONS = {
    "workspace.profile.edit": "Edit the workspace identity, primary country, time zone, currency, owner.",
    "workspace.transfer":     "Transfer workspace ownership to another user.",
    "billing.manage":         "Change plan, billing cycle, seat count, cancel subscription.",
    "billing.invoices.view":  "View invoices and receipts.",
    "billing.payment_methods.manage": "Add, replace, or remove payment methods and tax info.",
    "roles.manage":           "Create, rename, and delete roles; reassign permissions.",
    "roles.view":             "View the role catalog and assignments.",
    "users.manage":           "Add, invite, edit, or deactivate workers and users.",
    "users.delete":           "Permanently delete users.",
    "policies.view":          "View workforce policies.",
    "policies.manage":        "Configure workforce, monitoring, leave, and risk policies.",
    "security.manage":        "Configure MFA, SSO, session controls, and IP rules.",
    "integrations.manage":    "Connect and disconnect payroll, HRIS, calendar, comms.",
    "audit.view":             "View audit logs, evidence exports, legal holds.",
    "departments.manage":     "Create, rename, and delete departments and teams.",
    "data.export":            "Export workspace data (subject to entitlements).",
    "self.profile.edit":      "Edit own profile and preferences.",
    "self.timesheet.view":    "View own clock sessions and timesheets.",
    "self.leave.submit":      "Submit leave requests for self.",
}


def _decorate_role(role: Dict[str, Any], user_count: int, mfa_enrolled_count: int = 0) -> Dict[str, Any]:
    """Attach tier / mfa metadata onto a role record so the frontend can
    render the badge + lock indicator without reproducing the catalog."""
    tier = role.get("tier") or "moderate"
    mfa_tier = role.get("mfa_tier") or "optional"
    tier_meta = TIER_META.get(tier, TIER_META["moderate"])
    mfa_meta = MFA_TIER_META.get(mfa_tier, MFA_TIER_META["optional"])
    return {
        **role,
        "tier":            tier,
        "tier_label":      tier_meta["label"],
        "tier_rank":       tier_meta["rank"],
        "mfa_tier":        mfa_tier,
        "mfa_tier_label":  mfa_meta["label"],
        "mfa_tier_icon":   mfa_meta["icon"],
        "user_count":      int(user_count),
        "mfa_enrolled":    int(mfa_enrolled_count),
        "is_privileged":   tier in _PRIVILEGED_TIERS,
    }


def _user_role_counts(db: Session, org_id: Optional[int] = None) -> Dict[str, int]:
    q = (
        db.query(models.User.role, func.count(models.User.id))
          .filter(models.User.is_active == True)  # noqa: E712
    )
    if org_id is not None:
        q = q.filter(models.User.organization_id == org_id)
    rows = q.group_by(models.User.role).all()
    return {role: int(cnt) for role, cnt in rows if role}


def _resolve_roles(db: Session, org_id=None) -> List[Dict[str, Any]]:
    """Catalog + custom + orphan roles, decorated with tier/MFA metadata
    and live user counts. Single source of truth for the role library."""
    counts = _user_role_counts(db, org_id)
    custom_roles = _load_custom_roles(db, org_id)
    catalog_codes = {r["code"] for r in ROLE_CATALOG}

    out: List[Dict[str, Any]] = []
    for r in ROLE_CATALOG:
        out.append(_decorate_role(r, counts.get(r["code"], 0)))
    for r in custom_roles:
        out.append(_decorate_role({**r, "is_system": False}, counts.get(r["code"], 0)))
    for role_value, cnt in counts.items():
        if role_value not in catalog_codes and not any(r["code"] == role_value for r in custom_roles):
            out.append(_decorate_role({
                "code": role_value, "label": role_value.title(),
                "description": "Custom role detected on a user account. Review and reassign or formalise via Create custom role.",
                "is_system": False, "permissions": [],
                "tier": "scoped", "mfa_tier": "optional",
            }, cnt))
    return out


@router.get("/roles")
def list_roles(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """List every role in the workspace plus its live user count, tier
    classification, MFA enforcement tier, and permission set. The
    Access Governance Center renders the entire role library from this."""
    _require_admin(current_user)
    return {"roles": _resolve_roles(db, current_user.organization_id), "permissions": _permission_list()}


@router.get("/permissions")
def permission_matrix(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Permission catalog + the role × permission matrix."""
    _require_admin(current_user)
    return {
        "permissions": _permission_list(),
        "matrix": [
            {
                "role": r["code"],
                "label": r["label"],
                "permissions": r["permissions"],
            }
            for r in ROLE_CATALOG
        ],
    }


def _permission_list() -> List[Dict[str, str]]:
    return [
        {"key": k, "description": v}
        for k, v in _PERMISSION_DESCRIPTIONS.items()
    ]


def _role_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "custom_role"


def _load_custom_roles(db: Session, org_id=None) -> List[Dict[str, Any]]:
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == CUSTOM_ROLES_KEY,
    ).one_or_none()
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        if not isinstance(data, list):
            return []
        valid_permissions = {p["key"] for p in _permission_list()}
        roles = []
        for item in data:
            if not isinstance(item, dict):
                continue
            code = _role_slug(str(item.get("code") or item.get("label") or "custom_role"))
            roles.append({
                "code": code,
                "label": str(item.get("label") or code.replace("_", " ").title()),
                "description": str(item.get("description") or "Custom workspace role."),
                "permissions": [p for p in (item.get("permissions") or []) if p in valid_permissions],
                "created_at": item.get("created_at"),
                "created_by": item.get("created_by"),
            })
        return roles
    except Exception:
        _log.exception("custom role catalog parse failed")
        return []


def _save_custom_roles(db: Session, roles: List[Dict[str, Any]], actor_id: Optional[int], org_id=None) -> None:
    payload = json.dumps(roles)
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == CUSTOM_ROLES_KEY,
    ).one_or_none()
    if row is None:
        db.add(models.AppSettings(
            organization_id=org_id,
            key=CUSTOM_ROLES_KEY,
            value=payload,
            description="Workspace custom roles catalog",
            updated_by=actor_id,
        ))
    else:
        row.value = payload
        row.updated_by = actor_id


class CreateRoleRequest(BaseModel):
    label: str
    description: Optional[str] = None
    permissions: List[str] = []
    tier: Optional[str] = None       # "owner" | "high" | "moderate" | "scoped" | "read_heavy"
    mfa_tier: Optional[str] = None   # "required" | "enforced" | "recommended" | "optional"


@router.post("/roles")
def create_role(
    body: CreateRoleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # SHADOW ONLY - writable=True, so a READ_ONLY decision is recorded as a
    # block here even though it would not be on a read route.
    _shadow(db, current_user, "workspace.settings.rbac_advanced",
            route="POST /workspace/roles", writable=True)
    _require_admin(current_user)
    label = (body.label or "").strip()
    if not label:
        raise HTTPException(400, "Role name is required")

    code = _role_slug(label)
    if any(r["code"] == code for r in ROLE_CATALOG):
        raise HTTPException(409, "A system role already uses that name")

    roles = _load_custom_roles(db, current_user.organization_id)
    if any(r["code"] == code for r in roles):
        raise HTTPException(409, "A custom role already uses that name")

    valid_permissions = {p["key"] for p in _permission_list()}
    permissions = [p for p in body.permissions if p in valid_permissions]
    tier = body.tier if body.tier in TIER_META else "moderate"
    mfa_tier = body.mfa_tier if body.mfa_tier in MFA_TIER_META else "recommended"
    role = {
        "code": code,
        "label": label,
        "description": (body.description or "").strip() or "Custom workspace role.",
        "permissions": permissions,
        "tier": tier,
        "mfa_tier": mfa_tier,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": current_user.email,
    }
    roles.append(role)
    _save_custom_roles(db, roles, current_user.id, org_id=current_user.organization_id)
    db.add(models.AuditLog(
        actor_id=current_user.id,
        organization_id=current_user.organization_id,
        entity="WorkspaceRole",
        action="RoleCreated",
        payload={"role": code, "label": label, "permissions": permissions, "tier": tier, "mfa_tier": mfa_tier},
    ))
    db.commit()
    return _decorate_role({**role, "is_system": False}, 0)


@router.delete("/roles/{code}")
def delete_role(
    code: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Delete a custom role. System roles cannot be removed."""
    _require_admin(current_user)
    if any(r["code"] == code for r in ROLE_CATALOG):
        raise HTTPException(400, "Cannot delete a built-in role")
    roles = _load_custom_roles(db, current_user.organization_id)
    if not any(r["code"] == code for r in roles):
        raise HTTPException(404, "Role not found")
    # Block deletion if any active user still holds the role — admin should
    # reassign first. This is the same pattern the user-deletion flow uses
    # for "cannot delete a workspace owner".
    in_use = (
        db.query(func.count(models.User.id))
          .filter(models.User.role == code, models.User.is_active == True)  # noqa: E712
          .scalar() or 0
    )
    if in_use:
        raise HTTPException(409, f"{in_use} active user(s) still use this role — reassign them first.")
    roles = [r for r in roles if r["code"] != code]
    _save_custom_roles(db, roles, current_user.id, org_id=current_user.organization_id)
    db.add(models.AuditLog(
        actor_id=current_user.id,
        organization_id=current_user.organization_id,
        entity="WorkspaceRole",
        action="RoleDeleted",
        payload={"role": code},
    ))
    db.commit()
    return {"ok": True, "code": code}


class AssignRoleRequest(BaseModel):
    user_id: int


@router.post("/roles/{role}/users")
def assign_role(
    role: str,
    body: AssignRoleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Reassign a user to a different system role."""
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    custom_roles = _load_custom_roles(db, current_user.organization_id)
    if not any(r["code"] == role for r in ROLE_CATALOG) and not any(r["code"] == role for r in custom_roles):
        raise HTTPException(400, f"Unknown role: {role}")
    # Only super_admin may elevate someone to super_admin or owner.
    if not is_super and role in _SUPER_ADMIN_ONLY_ROLES:
        raise HTTPException(403, f"Only super_admin may assign the '{role}' role")
    user = db.query(models.User).filter(models.User.id == body.user_id).one_or_none()
    if user is None:
        raise HTTPException(404, "User not found")
    # Tenant admins may only assign roles within their own organization.
    if not is_super and user.organization_id != current_user.organization_id:
        raise HTTPException(403, "Cannot assign roles to users outside your organization")
    old_role = user.role
    user.role = role
    db.add(models.AuditLog(
        actor_id=current_user.id, target_id=user.id,
        organization_id=current_user.organization_id,
        entity="User", action="RoleReassigned",
        payload={"from": old_role, "to": role, "user_email": user.email},
    ))
    db.commit()
    return {"user_id": user.id, "role": role, "previous_role": old_role}


class BulkAssignRequest(BaseModel):
    user_ids: List[int]
    role: str


@router.post("/access-governance/assignments/bulk")
def bulk_assign_role(
    body: BulkAssignRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Reassign multiple users to the same role in one round-trip.
    Used by the Access Governance Center Assignments tab."""
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    custom_roles = _load_custom_roles(db, current_user.organization_id)
    if not any(r["code"] == body.role for r in ROLE_CATALOG) and not any(r["code"] == body.role for r in custom_roles):
        raise HTTPException(400, f"Unknown role: {body.role}")
    if not body.user_ids:
        raise HTTPException(400, "Select at least one user")
    # Only super_admin may bulk-elevate to super_admin or owner.
    if not is_super and body.role in _SUPER_ADMIN_ONLY_ROLES:
        raise HTTPException(403, f"Only super_admin may assign the '{body.role}' role")
    users_q = db.query(models.User).filter(models.User.id.in_(body.user_ids))
    # Tenant admins may only reassign users within their own organization.
    if not is_super:
        users_q = users_q.filter(models.User.organization_id == current_user.organization_id)
    users = users_q.all()
    updated = []
    for u in users:
        old_role = u.role
        u.role = body.role
        db.add(models.AuditLog(
            actor_id=current_user.id, target_id=u.id,
            organization_id=current_user.organization_id,
            entity="User", action="RoleReassigned",
            payload={"from": old_role, "to": body.role, "user_email": u.email, "bulk": True},
        ))
        updated.append({"user_id": u.id, "email": u.email, "previous_role": old_role})
    db.commit()
    return {"updated": updated, "role": body.role, "count": len(updated)}


# ──────────────────────────────────────────────────────────────────────────
# Access Governance Center — overview, MFA controls, assignments, audit
# ──────────────────────────────────────────────────────────────────────────
def _mfa_settings(db: Session, org_id=None) -> Dict[str, str]:
    rows = (
        db.query(models.AppSettings)
          .filter(
              models.AppSettings.organization_id == org_id,
              models.AppSettings.key.like("security.mfa_%"),
          )
          .all()
    )
    return {r.key: r.value for r in rows}


def _users_with_mfa(db: Session) -> Dict[str, int]:
    """Return per-role count of active users that have a verified MFA
    factor enrolled. Falls back to 0 for every role if the MFA factor
    table isn't present in this deployment (fresh install, pre-feature)."""
    try:
        if not hasattr(models, "UserMfaFactor"):
            return {}
        rows = (
            db.query(models.User.role, func.count(models.User.id))
              .join(models.UserMfaFactor, models.UserMfaFactor.user_id == models.User.id)
              .filter(models.User.is_active == True)  # noqa: E712
              .filter(models.UserMfaFactor.verified_at.isnot(None))
              .group_by(models.User.role)
              .all()
        )
        return {role: int(cnt) for role, cnt in rows if role}
    except Exception:
        return {}


@router.get("/access-governance/overview")
def access_governance_overview(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Three stat-card payload for the Access Governance Center header.

      • privileged_access — active users on an Owner / High-privilege tier
                            who have NOT enrolled MFA yet (the actual gap)
      • mfa_coverage      — % of admin-tier users that have a verified
                            MFA factor + count of exception logs
      • custom_roles      — count of custom roles + last-changed timestamp
    """
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    overview_org_id = None if is_super else current_user.organization_id
    roles = _resolve_roles(db, current_user.organization_id)
    role_by_code = {r["code"]: r for r in roles}
    counts = _user_role_counts(db, overview_org_id)
    mfa_counts = _users_with_mfa(db)

    # Privileged access — users on owner/high-privilege roles without MFA.
    privileged_total = 0
    privileged_missing_mfa = 0
    admin_tier_total = 0
    admin_tier_with_mfa = 0
    for code, total in counts.items():
        role = role_by_code.get(code)
        if not role:
            continue
        tier = role.get("tier") or "moderate"
        enrolled = mfa_counts.get(code, 0)
        if tier in _PRIVILEGED_TIERS:
            privileged_total += total
            privileged_missing_mfa += max(0, total - enrolled)
        # MFA coverage denominator: everything but pure "scoped" worker roles
        if tier in ("owner", "high", "moderate", "read_heavy"):
            admin_tier_total += total
            admin_tier_with_mfa += min(total, enrolled)

    coverage_pct = int(round((admin_tier_with_mfa / admin_tier_total) * 100)) if admin_tier_total else 0

    # MFA exception log entries from the last 30 days (any audit row
    # tagged as an MFA bypass / exception). The query is best-effort —
    # missing tables / mis-named entities should not 500 the overview.
    mfa_exceptions = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        mfa_exceptions = int(
            db.query(func.count(models.AuditLog.id))
              .filter(models.AuditLog.created_at >= cutoff)
              .filter(models.AuditLog.entity.in_(("MfaException", "UserMfaFactor", "Security")))
              .filter(models.AuditLog.action.in_(("MfaExceptionGranted", "MfaResetForced", "MfaBypassUsed")))
              .scalar() or 0
        )
    except Exception:
        mfa_exceptions = 0

    custom_roles = [r for r in roles if not r.get("is_system")]
    last_role_change = None
    try:
        row = (
            db.query(models.AuditLog)
              .filter(models.AuditLog.entity.in_(("WorkspaceRole", "User")))
              .filter(models.AuditLog.action.in_(("RoleCreated", "RoleDeleted", "RoleReassigned")))
              .order_by(desc(models.AuditLog.created_at))
              .first()
        )
        if row and row.created_at:
            last_role_change = row.created_at.isoformat()
    except Exception:
        pass

    # Captions describe real workspace state — no "all protected" when there
    # are zero privileged users, no "active" when there are zero custom roles.
    if privileged_total == 0:
        privileged_caption = "no privileged users yet"
    elif privileged_missing_mfa:
        privileged_caption = (
            f"{privileged_missing_mfa} of {privileged_total} missing MFA enforcement"
        )
    else:
        privileged_caption = f"{privileged_total} protected · all enrolled"

    if admin_tier_total == 0:
        mfa_caption = "no admin-tier users yet"
    else:
        mfa_caption = (
            f"{admin_tier_with_mfa} of {admin_tier_total} enrolled · "
            f"{mfa_exceptions} exception{'s' if mfa_exceptions != 1 else ''} logged"
        )

    if not custom_roles:
        custom_caption = "none configured"
    elif last_role_change:
        custom_caption = f"active · last changed {last_role_change.split('T')[0]}"
    else:
        custom_caption = "active"

    return {
        "privileged_access": {
            "value": privileged_missing_mfa,
            "total": privileged_total,
            "label": "Privileged access",
            "caption": privileged_caption,
        },
        "mfa_coverage": {
            "value": coverage_pct,
            "covered": admin_tier_with_mfa,
            "total": admin_tier_total,
            "exceptions": mfa_exceptions,
            "label": "MFA coverage",
            "caption": mfa_caption,
        },
        "custom_roles": {
            "value": len(custom_roles),
            "label": "Custom roles",
            "caption": custom_caption,
            "last_changed": last_role_change,
        },
    }


@router.get("/access-governance/assignments")
def access_governance_assignments(
    limit: int = Query(500, ge=1, le=2000),
    search: Optional[str] = None,
    role_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Per-user role assignments for the Access Governance Center
    Assignments tab. Includes MFA enrollment state so the same view can
    drive the MFA exceptions audit."""
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id = current_user.organization_id

    mfa_enrolled_ids: set = set()
    try:
        if hasattr(models, "UserMfaFactor"):
            mfa_q = (
                db.query(models.UserMfaFactor.user_id)
                  .filter(models.UserMfaFactor.verified_at.isnot(None))
            )
            if not is_super:
                mfa_q = mfa_q.join(models.User, models.User.id == models.UserMfaFactor.user_id).filter(
                    models.User.organization_id == org_id
                )
            mfa_enrolled_ids = {row[0] for row in mfa_q.all()}
    except Exception:
        pass

    q = db.query(models.User).filter(models.User.is_active == True)  # noqa: E712
    if not is_super:
        q = q.filter(models.User.organization_id == org_id)
    if role_filter:
        q = q.filter(models.User.role == role_filter)
    if search:
        like = f"%{search.lower()}%"
        q = q.filter(
            (func.lower(models.User.name).like(like))
            | (func.lower(models.User.email).like(like))
        )
    users = q.order_by(models.User.name.asc()).limit(limit).all()

    roles = _resolve_roles(db, current_user.organization_id)
    role_by_code = {r["code"]: r for r in roles}

    items = []
    for u in users:
        role = role_by_code.get(u.role) or {}
        items.append({
            "user_id":       u.id,
            "name":          u.name,
            "email":         u.email,
            "role":          u.role,
            "role_label":    role.get("label") or u.role,
            "tier":          role.get("tier") or "moderate",
            "mfa_required": (role.get("mfa_tier") in ("required", "enforced")),
            "mfa_enrolled": u.id in mfa_enrolled_ids,
            "last_login_at": (u.last_login_at.isoformat() if getattr(u, "last_login_at", None) else None),
        })
    return {"items": items, "count": len(items)}


class MfaControlsRequest(BaseModel):
    """All four tiers are configurable but only required / enforced enforce
    server-side. recommended and optional are presentational defaults the UI
    surfaces to admins so they know which roles to nudge."""
    required_tiers: Optional[List[str]] = None   # e.g. ["owner", "high"]
    enforce_grace_days: Optional[int] = None
    allow_exceptions:   Optional[bool] = None


@router.get("/access-governance/mfa-controls")
def get_mfa_controls(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Workspace-wide MFA enforcement matrix. Each tier maps to the
    minimum MFA stance the UI should surface in the role library."""
    _require_admin(current_user)
    # SHADOW ONLY - account security is essential and must survive every
    # impaired lifecycle state.
    _shadow(db, current_user, "identity.mfa",
            route="GET /workspace/access-governance/mfa-controls")
    cfg = _mfa_settings(db, current_user.organization_id)
    try:
        required_tiers = json.loads(cfg.get("security.mfa_required_tiers_json", "[\"owner\",\"high\"]"))
        if not isinstance(required_tiers, list):
            required_tiers = ["owner", "high"]
    except Exception:
        required_tiers = ["owner", "high"]
    return {
        "tiers": [
            {"code": code, **meta}
            for code, meta in TIER_META.items()
        ],
        "mfa_tiers": [
            {"code": code, **meta}
            for code, meta in MFA_TIER_META.items()
        ],
        "required_tiers": required_tiers,
        "enforce_grace_days": int(cfg.get("security.mfa_enforce_grace_days", "7") or 7),
        "allow_exceptions":   cfg.get("security.mfa_allow_exceptions", "true") == "true",
    }


@router.put("/access-governance/mfa-controls")
def update_mfa_controls(
    body: MfaControlsRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    delta = {}
    if body.required_tiers is not None:
        clean = [t for t in body.required_tiers if t in TIER_META]
        delta["security.mfa_required_tiers_json"] = json.dumps(clean)
    if body.enforce_grace_days is not None:
        delta["security.mfa_enforce_grace_days"] = str(max(0, min(90, int(body.enforce_grace_days))))
    if body.allow_exceptions is not None:
        delta["security.mfa_allow_exceptions"] = "true" if body.allow_exceptions else "false"

    mfa_org_id = current_user.organization_id
    for key, value in delta.items():
        row = db.query(models.AppSettings).filter(
            models.AppSettings.organization_id == mfa_org_id,
            models.AppSettings.key == key,
        ).one_or_none()
        if row is None:
            db.add(models.AppSettings(
                organization_id=mfa_org_id,
                key=key, value=value,
                description="Access Governance Center MFA controls",
                updated_by=current_user.id,
            ))
        else:
            row.value = value
            row.updated_by = current_user.id
    if delta:
        db.add(models.AuditLog(
            actor_id=current_user.id,
            organization_id=current_user.organization_id,
            entity="MfaControls",
            action="MfaControlsUpdated",
            payload={"fields": list(delta.keys())},
        ))
    db.commit()
    return get_mfa_controls(db, current_user)


@router.get("/access-governance/audit-trail")
def access_governance_audit(
    limit: int = Query(100, ge=1, le=500),
    since_hours: int = Query(720, ge=1, le=8760),   # default 30 days
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Role + MFA-related audit events feeding the Access Governance Center
    Audit trail tab. Filters the global audit feed to entities admins care
    about for governance — roles, user role assignments, MFA controls."""
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    audit_q = (
        db.query(models.AuditLog, models.User)
          .outerjoin(models.User, models.User.id == models.AuditLog.actor_id)
          .filter(models.AuditLog.created_at >= cutoff)
          .filter(models.AuditLog.entity.in_((
              "WorkspaceRole", "User", "MfaControls", "MfaException",
              "UserMfaFactor", "Security",
          )))
    )
    if not is_super:
        audit_q = audit_q.filter(models.AuditLog.organization_id == current_user.organization_id)
    rows = audit_q.order_by(desc(models.AuditLog.created_at)).limit(limit).all()
    events = []
    for r, u in rows:
        events.append({
            "id":          r.id,
            "occurred_at": r.created_at.isoformat() if r.created_at else None,
            "actor_email": u.email if u else None,
            "actor_name":  u.name if u else None,
            "actor_role":  u.role if u else None,
            "entity":      r.entity,
            "action":      r.action,
            "payload":     r.payload,
            "ip_address":  r.ip_address,
        })
    return {"since_hours": since_hours, "events": events, "count": len(events)}


def _load_holiday_profiles(db: Session, org_id=None) -> List[Dict[str, Any]]:
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == HOLIDAY_PROFILES_KEY,
    ).one_or_none()
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_holiday_profiles(db: Session, profiles: List[Dict[str, Any]], actor_id: Optional[int], org_id=None) -> None:
    payload = json.dumps(profiles)
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == HOLIDAY_PROFILES_KEY,
    ).one_or_none()
    if row is None:
        db.add(models.AppSettings(
            organization_id=org_id,
            key=HOLIDAY_PROFILES_KEY,
            value=payload,
            description="Workspace public holiday country profiles",
            updated_by=actor_id,
        ))
    else:
        row.value = payload
        row.updated_by = actor_id


def _list_holidays(db: Session, current_user: models.User) -> list:
    """The caller's stored holidays, through the shared organization calendar.

    Kept column-scoped and normalized by ``app.holidays.org_calendar`` so this
    surface and the Manage Holidays page cannot drift into two different holiday
    shapes. The platform-wide branch for ``super_admin`` is intentionally the
    one exception to org scoping and stays explicit here.
    """
    from app.holidays import catalog as _holiday_catalog
    from app.holidays import org_calendar as _holiday_calendar

    if current_user.role != "super_admin":
        return _holiday_calendar.list_org_holidays(db, current_user.organization_id)[:250]

    rows = (
        db.query(
            models.Holiday.id, models.Holiday.name, models.Holiday.date,
            models.Holiday.holiday_type, models.Holiday.description,
            models.Holiday.country_code,
        )
        .order_by(models.Holiday.date.asc())
        .limit(250)
        .all()
    )
    out = []
    for r in rows:
        entry = _holiday_catalog.normalize_entry(
            {
                "id": r.id,
                "name": r.name,
                "date": r.date.strftime("%Y-%m-%d") if r.date else "",
                "holiday_type": r.holiday_type,
                "description": r.description,
            },
            source="organization",
        )
        entry["country_code"] = r.country_code
        out.append(entry)
    return out


@router.get("/public-holidays")
def public_holiday_profiles(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    # SHADOW ONLY - holidays.multicountry is genuinely Sovereign+ in the shipped
    # manifest, so this is the family's true-negative probe. Observes only; never
    # blocks. Main's holiday-policy permission check below is preserved unchanged.
    _shadow(db, current_user, "holidays.multicountry",
            route="GET /workspace/public-holidays")
    # Reading the holiday configuration follows the holiday policy, not the
    # broader workspace-admin list — see app/holidays/permissions.py for the
    # single matrix both routers now share.
    holiday_permissions.require_holiday_write(current_user)
    return {
        "profiles": _load_holiday_profiles(db, current_user.organization_id),
        "local_holidays": _list_holidays(db, current_user),
    }


class HolidayProfileUpdate(BaseModel):
    country_code: str
    country_name: Optional[str] = None
    year: Optional[int] = None
    region: Optional[str] = None


@router.put("/public-holidays/profile",
            dependencies=[Depends(require_writable_capability("holidays.multicountry"))])
def upsert_public_holiday_profile(
    body: HolidayProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    holiday_permissions.require_holiday_write(current_user)
    code = (body.country_code or "").strip().upper()
    if not re.match(r"^[A-Z]{2}$", code):
        raise HTTPException(400, "country_code must be a 2-letter ISO code")

    # Reject a country Temveris cannot actually produce a calendar for. The
    # audit's headline bug was that this endpoint validated only the ISO-2
    # *shape*: an admin saved an "IN" profile here, and Manage Holidays then
    # could not render it because the external provider has no India. The
    # profile and the calendar now agree on what is selectable.
    support = holiday_service.country_support(code)
    if support == holiday_catalog.SUPPORT_UNSUPPORTED:
        raise HTTPException(
            400,
            f"No public-holiday calendar is available for '{code}'. "
            "Pick a supported country, or add its holidays manually in "
            "Manage Holidays.",
        )

    year = int(body.year or datetime.now(timezone.utc).year)
    profile = {
        "country_code": code,
        "country_name": (body.country_name or code).strip(),
        "year": year,
        "region": (body.region or "").strip() or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": current_user.email,
    }
    profiles = [
        p for p in _load_holiday_profiles(db, current_user.organization_id)
        if not (p.get("country_code") == code and int(p.get("year") or 0) == year)
    ]
    profiles.insert(0, profile)
    profiles = profiles[:25]
    _save_holiday_profiles(db, profiles, current_user.id, org_id=current_user.organization_id)
    db.add(models.AuditLog(
        actor_id=current_user.id,
        organization_id=current_user.organization_id,
        entity="PublicHolidayProfile",
        action="HolidayProfileUpdated",
        payload=profile,
    ))
    db.commit()
    return {"profile": profile, "profiles": profiles}


# ──────────────────────────────────────────────────────────────────────────
# §5 — Policies (aggregated)
# ──────────────────────────────────────────────────────────────────────────
@router.get("/policies")
def policies(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Aggregate workforce + monitoring + leave + risk policies in one payload.

    All values come from real tables — DEFAULTS in app.settings, the
    leave_policies table, etc. Modules that don't have rows yet return
    their defaults so the tab always renders truthfully.
    """
    _require_admin(current_user)

    # Tracker + risk settings from app_settings (PUT /settings/update is
    # the canonical mutator — we just expose a read-side aggregate here).
    from app.settings.router import _all_settings, DESCRIPTIONS as SETTING_DESCRIPTIONS

    s = _all_settings(db, current_user.organization_id)

    # Leave policies — one row per configured leave type.
    leave_policies = []
    try:
        lp_q = db.query(models.LeavePolicy).filter(models.LeavePolicy.is_active == True)  # noqa: E712
        if current_user.role != "super_admin":
            lp_q = lp_q.filter(models.LeavePolicy.organization_id == current_user.organization_id)
        rows = lp_q.all()
        for p in rows:
            leave_policies.append({
                "leave_type":       p.leave_type,
                "annual_limit":     p.annual_limit,
                "monthly_accrual":  p.monthly_accrual,
                "max_carry_forward": p.max_carry_forward,
                "requires_document": p.requires_document,
                "min_notice_days":  p.min_notice_days,
                "approval_chain":   p.approval_chain,
            })
    except Exception:
        _log.exception("LeavePolicy read failed")

    return {
        "groups": [
            {
                "key":   "monitoring",
                "title": "Monitoring & Tracker",
                "description": "Desktop tracker — screenshots, idle detection, monitoring posture.",
                "endpoint_get": "/settings",
                "endpoint_put": "/settings/update",
                "fields": [
                    _field("screenshot_enabled",     s, "boolean", SETTING_DESCRIPTIONS),
                    _field("screenshot_interval",    s, "integer", SETTING_DESCRIPTIONS),
                    _field("screenshot_blur",        s, "boolean", SETTING_DESCRIPTIONS),
                    _field("idle_threshold_minutes", s, "integer", SETTING_DESCRIPTIONS),
                ],
            },
            {
                "key":   "risk",
                "title": "Risk & Behavioral Alerts",
                "description": "Triggers HR risk alerts when daily worked time drops below the threshold.",
                "endpoint_get": "/settings",
                "endpoint_put": "/settings/update",
                "fields": [
                    _field("risk_alert_enabled",   s, "boolean", SETTING_DESCRIPTIONS),
                    _field("work_hours_threshold", s, "duration", SETTING_DESCRIPTIONS),
                ],
            },
            {
                "key":   "leave",
                "title": "Leave Policies",
                "description": "Per-type leave configuration — accrual, carry-forward, notice, approvals.",
                "endpoint_get": "/leave/types",
                "endpoint_put": "/leave/admin/types",
                "policies": leave_policies,
                "policy_count": len(leave_policies),
            },
            {
                "key":   "jurisdiction",
                "title": "Jurisdiction-Specific Policies",
                "description": "Public holidays drive jurisdiction policy defaults.",
                "endpoint_get": "/holidays",
                "policy_count": _safe_count(db, models.Holiday),
            },
        ],
    }


def _field(key: str, store: Dict[str, str], type_hint: str, descriptions: Dict[str, str]):
    return {
        "key":         key,
        "value":       store.get(key),
        "type":        type_hint,
        "description": descriptions.get(key, ""),
    }


def _safe_count(db: Session, model) -> int:
    try:
        return int(db.query(func.count(model.id)).scalar() or 0)
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────────────
# §7 — Billing & Subscription summary
# ──────────────────────────────────────────────────────────────────────────
@router.get("/billing-summary")
def billing_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Compact billing snapshot for the tab. Drills down into /api/admin/billing/*."""
    _require_admin(current_user)
    org_id = _billing_org(current_user)

    # Subscription state — one row per organization (billing is tenant-scoped).
    sub = (
        db.query(models.BillingSubscription)
          .filter(models.BillingSubscription.organization_id == org_id)
          .first()
    )
    plan = None
    if sub and sub.plan_id:
        plan = db.query(models.BillingPlan).filter(models.BillingPlan.id == sub.plan_id).first()

    # Recent invoices (last 5).
    invoices = (
        db.query(models.BillingInvoice)
          .filter(models.BillingInvoice.organization_id == org_id)
          .order_by(desc(models.BillingInvoice.created_at))
          .limit(5).all()
    )

    # Billable seats for THIS organization only. This previously counted every
    # active user on the platform with no organization filter, so one tenant's
    # summary reported all tenants' staff. Delegated to the billing service so
    # the definition (ACTIVE_BILLABLE + SUSPENDED_BILLABLE) stays in one place
    # rather than being re-derived from is_active here.
    billable = billing_service.count_billable_seats(db, org_id)

    # Active payment method, if any.
    pm = (
        db.query(models.BillingPaymentMethod)
          .filter(models.BillingPaymentMethod.organization_id == org_id)
          .filter(models.BillingPaymentMethod.is_default == True)  # noqa: E712
          .first()
    )

    return {
        "subscription": {
            "plan_code":          plan.code if plan else None,
            "plan_name":          plan.name if plan else None,
            "monthly_price":      plan.monthly_price_per_user if plan else None,
            "annual_price":       plan.annual_price_per_user if plan else None,
            "billing_cycle":      sub.billing_cycle if sub else None,
            "subscription_state": sub.subscription_status if sub else "trial",
            "trial_end_date":     sub.trial_end_date.isoformat() if sub and sub.trial_end_date else None,
            "renewal_date":       sub.renewal_date.isoformat() if sub and sub.renewal_date else None,
            "currency":           sub.currency if sub else "USD",
        },
        "usage": {
            "billable_users":  billable,
            "active_users":    sub.active_users if sub else billable,
            "pending_invites": sub.pending_invites if sub else 0,
            "seat_limit":      plan.user_limit if plan else None,
        },
        "payment_method": (
            None if not pm else {
                "brand":        pm.brand,
                "last_four":    pm.last_four,
                "expiry_month": pm.expiry_month,
                "expiry_year":  pm.expiry_year,
            }
        ),
        "recent_invoices": [
            {
                "id":           inv.id,
                "number":       inv.invoice_number,
                "total":        inv.total_amount,
                "currency":     inv.currency,
                "status":       inv.status,
                "issued_at":    inv.created_at.isoformat() if inv.created_at else None,
                "paid_at":      inv.paid_date.isoformat() if inv.paid_date else None,
            }
            for inv in invoices
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# §8 — Security
# ──────────────────────────────────────────────────────────────────────────
class IPRule(BaseModel):
    cidr: str
    label: Optional[str] = None
    action: str = "allow"   # allow | deny


@router.get("/security")
def security_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Workspace-wide security posture. All values from real tables."""
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    org_id   = current_user.organization_id

    # Active sessions scoped to this organization.
    # UserSession has no organization_id column; scope through the User join.
    active_q = (
        db.query(func.count(models.UserSession.id))
          .join(models.User, models.User.id == models.UserSession.user_id)
          .filter(models.UserSession.is_revoked == False)  # noqa: E712
    )
    if not is_super:
        active_q = active_q.filter(models.User.organization_id == org_id)
    active_sessions = active_q.scalar() or 0

    # Workspace-wide config keys live in app_settings, scoped to this org.
    cfg = {
        r.key: r.value
        for r in db.query(models.AppSettings)
                   .filter(
                       models.AppSettings.organization_id == current_user.organization_id,
                       models.AppSettings.key.in_([
                           "security.mfa_required",
                           "security.sso_enabled",
                           "security.ip_allowlist_enabled",
                           "security.session_timeout_minutes",
                       ]),
                   )
                   .all()
    }

    # Admin-only users scoped to this organization.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stale_q = (
        db.query(func.count(models.User.id))
          .filter(models.User.role.in_(("admin", "hr")))
          .filter(models.User.is_active == True)  # noqa: E712
    )
    if not is_super:
        stale_q = stale_q.filter(models.User.organization_id == org_id)
    stale_admins = stale_q.scalar() or 0

    return {
        "mfa": {
            "required":      cfg.get("security.mfa_required", "false") == "true",
            "method_supported": ["totp"],
            "endpoint_put":  "/api/workspace/security/mfa",
        },
        "sso": {
            "enabled":       cfg.get("security.sso_enabled", "false") == "true",
            "providers_supported": ["saml2", "oidc"],
            "endpoint_put":  "/api/workspace/security/sso",
        },
        "sessions": {
            "active":        int(active_sessions),
            "timeout_minutes": int(cfg.get("security.session_timeout_minutes", "0") or 0),
            "endpoint_list": "/api/workspace/security/sessions",
        },
        "ip_rules": {
            "enabled":       cfg.get("security.ip_allowlist_enabled", "false") == "true",
            "rules":         _load_ip_rules(db, current_user.organization_id),
            "endpoint_put":  "/api/workspace/security/ip-rules",
        },
        "stale_admins": int(stale_admins),
        "audit_ref":     "/api/workspace/audit",
    }


def _load_ip_rules(db: Session, org_id=None) -> List[Dict[str, str]]:
    row = (
        db.query(models.AppSettings)
          .filter(
              models.AppSettings.organization_id == org_id,
              models.AppSettings.key == "security.ip_rules_json",
          )
          .first()
    )
    if not row or not row.value:
        return []
    import json as _json
    try:
        data = _json.loads(row.value)
        if isinstance(data, list):
            return data
    except Exception:
        _log.exception("ip_rules_json parse failed")
    return []


@router.get("/security/sessions")
def list_sessions(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    q = (
        db.query(models.UserSession, models.User)
          .join(models.User, models.UserSession.user_id == models.User.id)
          .filter(models.UserSession.is_revoked == False)  # noqa: E712
    )
    if not is_super:
        q = q.filter(models.User.organization_id == current_user.organization_id)
    rows = q.order_by(desc(models.UserSession.last_active_at)).limit(limit).all()
    return {
        "items": [
            {
                "id":             s.id,
                "user_email":     u.email,
                "user_name":      u.name,
                "device_name":    s.device_name or s.device_type,
                "browser":        s.browser,
                "os":             s.os,
                "ip_address":     s.ip_address,
                "location":       s.location,
                "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
                "created_at":     s.created_at.isoformat() if s.created_at else None,
            }
            for s, u in rows
        ],
    }


@router.post("/security/sessions/{session_id}/revoke")
def revoke_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    s = db.query(models.UserSession).filter(models.UserSession.id == session_id).one_or_none()
    if s is None:
        raise HTTPException(404, "Session not found")
    if not is_super:
        owner = db.query(models.User.organization_id).filter(models.User.id == s.user_id).scalar()
        if owner != current_user.organization_id:
            raise HTTPException(403, "Session does not belong to your organization")
    s.is_revoked = True
    s.revoked_at = datetime.now(timezone.utc)
    db.add(models.AuditLog(
        actor_id=current_user.id, target_id=s.user_id,
        organization_id=current_user.organization_id,
        entity="UserSession", action="SessionRevoked",
        payload={"session_id": session_id, "device": s.device_name or s.device_type},
    ))
    db.commit()
    return {"session_id": session_id, "revoked": True}


class SecurityToggle(BaseModel):
    enabled: bool


@router.put("/security/mfa")
def update_mfa(
    body: SecurityToggle,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    _upsert_setting(db, "security.mfa_required", "true" if body.enabled else "false", current_user.id, org_id=current_user.organization_id)
    db.add(models.AuditLog(
        actor_id=current_user.id, entity="WorkspaceSecurity",
        organization_id=current_user.organization_id,
        action="MFARequirementChanged",
        payload={"enabled": body.enabled},
    ))
    db.commit()
    return {"mfa_required": body.enabled}


@router.put("/security/sso")
def update_sso(
    body: SecurityToggle,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    _upsert_setting(db, "security.sso_enabled", "true" if body.enabled else "false", current_user.id, org_id=current_user.organization_id)
    db.add(models.AuditLog(
        actor_id=current_user.id, entity="WorkspaceSecurity",
        organization_id=current_user.organization_id,
        action="SSOConfigChanged",
        payload={"enabled": body.enabled},
    ))
    db.commit()
    return {"sso_enabled": body.enabled}


class IPRulesUpdate(BaseModel):
    enabled: bool
    rules: List[IPRule] = []


@router.put("/security/ip-rules")
def update_ip_rules(
    body: IPRulesUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    import json as _json
    org_id = current_user.organization_id
    _upsert_setting(db, "security.ip_allowlist_enabled", "true" if body.enabled else "false", current_user.id, org_id=org_id)
    _upsert_setting(db, "security.ip_rules_json", _json.dumps([r.model_dump() for r in body.rules]), current_user.id, org_id=org_id)
    db.add(models.AuditLog(
        actor_id=current_user.id, entity="WorkspaceSecurity",
        organization_id=current_user.organization_id,
        action="IPRulesChanged",
        payload={"enabled": body.enabled, "rule_count": len(body.rules)},
    ))
    db.commit()
    return {"ip_allowlist_enabled": body.enabled, "rules": [r.model_dump() for r in body.rules]}


def _upsert_setting(db: Session, key: str, value: str, actor_id: Optional[int], org_id=None) -> None:
    row = db.query(models.AppSettings).filter(
        models.AppSettings.organization_id == org_id,
        models.AppSettings.key == key,
    ).one_or_none()
    if row is None:
        db.add(models.AppSettings(organization_id=org_id, key=key, value=value, updated_by=actor_id))
    else:
        row.value = value
        row.updated_by = actor_id


# ──────────────────────────────────────────────────────────────────────────
# §9 — Connected Systems
# ──────────────────────────────────────────────────────────────────────────
# The integration registry lives in app_settings as JSON. The endpoint
# returns one row per known integration slot (whether connected or not)
# so the tab can render a complete picture, not just connected items.
_INTEGRATION_CATALOG: List[Dict[str, Any]] = [
    {"key": "payroll",   "category": "Payroll",   "vendor_examples": "Gusto, Rippling, Deel",
     "description": "Sync payable hours into payroll runs."},
    {"key": "hris",      "category": "HRIS",      "vendor_examples": "BambooHR, Workday, HiBob",
     "description": "Import workers, departments, and employment records."},
    {"key": "accounting","category": "Accounting","vendor_examples": "QuickBooks, Xero, NetSuite",
     "description": "Push approved time records to client invoices."},
    {"key": "pm",        "category": "Project Management", "vendor_examples": "Jira, Asana, Linear",
     "description": "Map tasks and projects to time records."},
    {"key": "calendar",  "category": "Calendar",  "vendor_examples": "Google Calendar, Microsoft 365",
     "description": "Pull events for the schedule and meeting blocks."},
    {"key": "comms",     "category": "Communication", "vendor_examples": "Slack, Microsoft Teams",
     "description": "Deliver risk alerts and approvals inline."},
]


@router.get("/integrations",
            dependencies=[Depends(require_capability("integrations.api"))])
def list_integrations(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    # Observation is provided by the rollout-gated require_capability dependency
    # (Phase 4.3 self-observing gate); the manual integrations.api shadow hook
    # that used to sit here has been consolidated away to avoid double-logging.
    state = _integration_state(db, current_user.organization_id)
    out = []
    for slot in _INTEGRATION_CATALOG:
        rec = state.get(slot["key"], {})
        out.append({
            **slot,
            "connected":     bool(rec.get("connected")),
            "vendor":        rec.get("vendor"),
            "connected_at":  rec.get("connected_at"),
            "connected_by":  rec.get("connected_by"),
        })
    return {"integrations": out}


class IntegrationUpdate(BaseModel):
    connected: bool
    vendor: Optional[str] = None


@router.put("/integrations/{key}",
            dependencies=[Depends(require_writable_capability("integrations.api"))])
def update_integration(
    key: str,
    body: IntegrationUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _require_admin(current_user)
    if not any(slot["key"] == key for slot in _INTEGRATION_CATALOG):
        raise HTTPException(400, f"Unknown integration key: {key}")
    vendor_clean = (body.vendor or "").strip() or None
    if body.connected and not vendor_clean:
        raise HTTPException(400, "Vendor name is required to connect an integration")
    state = _integration_state(db, current_user.organization_id)
    state[key] = {
        "connected":   body.connected,
        "vendor":      vendor_clean,
        "connected_at": datetime.now(timezone.utc).isoformat() if body.connected else None,
        "connected_by": current_user.email if body.connected else None,
    }
    import json as _json
    _upsert_setting(db, "integrations.state_json", _json.dumps(state), current_user.id, org_id=current_user.organization_id)
    db.add(models.AuditLog(
        actor_id=current_user.id, entity="Integration",
        organization_id=current_user.organization_id,
        action="IntegrationConnected" if body.connected else "IntegrationDisconnected",
        payload={"key": key, "vendor": body.vendor},
    ))
    db.commit()
    return state[key] | {"key": key}


def _integration_state(db: Session, org_id=None) -> Dict[str, Dict[str, Any]]:
    row = (
        db.query(models.AppSettings)
          .filter(
              models.AppSettings.organization_id == org_id,
              models.AppSettings.key == "integrations.state_json",
          )
          .one_or_none()
    )
    if not row or not row.value:
        return {}
    import json as _json
    try:
        data = _json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────
# §10 — Audit & Compliance
# ──────────────────────────────────────────────────────────────────────────
@router.get("/audit")
def audit_feed(
    limit: int = Query(50, ge=1, le=500),
    entity: Optional[str] = None,
    action: Optional[str] = None,
    since_hours: int = Query(168, ge=1, le=8760),   # default = last 7 days
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Combined audit feed.

    Pulls from the legacy AuditLog table (every admin action emits a row)
    plus the multi-tenant CommercialEvent table (governed commercial events
    per Tier-0 spec). Returns a single merged, time-ordered stream so the
    Audit & Compliance tab has one source of truth.
    """
    _require_admin(current_user)
    is_super = current_user.role == "super_admin"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    # Legacy audit_logs
    q = db.query(models.AuditLog, models.User).outerjoin(
        models.User, models.User.id == models.AuditLog.actor_id,
    ).filter(models.AuditLog.created_at >= cutoff)
    if not is_super:
        q = q.filter(models.AuditLog.organization_id == current_user.organization_id)
    if entity:
        q = q.filter(models.AuditLog.entity == entity)
    if action:
        q = q.filter(models.AuditLog.action == action)
    legacy = q.order_by(desc(models.AuditLog.created_at)).limit(limit).all()
    legacy_rows = [
        {
            "source":     "audit_logs",
            "id":         f"audit_{r.id}",
            "occurred_at": r.created_at.isoformat() if r.created_at else None,
            "actor_email": (u.email if u else None),
            "actor_role":  (u.role if u else None),
            "entity":      r.entity,
            "action":      r.action,
            "payload":     r.payload,
            "ip_address":  r.ip_address,
        }
        for r, u in legacy
    ]

    # Commercial events — only visible to super_admin (uses a separate String org_id system
    # that does not map 1:1 to the integer organization_id on User rows).
    comm_rows = []
    if is_super:
        try:
            from app.commercial.models import CommercialEvent
            q2 = (
                db.query(CommercialEvent)
                  .filter(CommercialEvent.occurred_at >= cutoff)
            )
            if action:
                q2 = q2.filter(CommercialEvent.event_type == action)
            comm = q2.order_by(desc(CommercialEvent.occurred_at)).limit(limit).all()
            comm_rows = [
                {
                    "source":      "commercial_events",
                    "id":          f"comm_{r.id}",
                    "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
                    "actor_email": None,
                    "actor_role":  None,
                    "entity":      r.source_service,
                    "action":      r.event_type,
                    "payload":     r.payload,
                    "ip_address":  None,
                    "state_from":  r.state_from,
                    "state_to":    r.state_to,
                    "error_code":  r.error_code,
                    "correlation_id": r.correlation_id,
                }
                for r in comm
            ]
        except Exception:
            comm_rows = []

    merged = sorted(
        legacy_rows + comm_rows,
        key=lambda r: r["occurred_at"] or "",
        reverse=True,
    )[:limit]

    # Aggregate compliance counters (last 30 days).
    cutoff_30 = datetime.now(timezone.utc) - timedelta(days=30)
    audit_count_q = (
        db.query(func.count(models.AuditLog.id))
          .filter(models.AuditLog.created_at >= cutoff_30)
    )
    if not is_super:
        audit_count_q = audit_count_q.filter(
            models.AuditLog.organization_id == current_user.organization_id
        )
    counters = {
        "audit_events_30d": int(audit_count_q.scalar() or 0),
        "legal_holds": 0,
        "consents_recorded_30d": 0,
    }
    if is_super:
        try:
            from app.commercial.models import CommercialConsent
            counters["consents_recorded_30d"] = int(
                db.query(func.count(CommercialConsent.id))
                  .filter(CommercialConsent.occurred_at >= cutoff_30)
                  .scalar() or 0
            )
        except Exception:
            pass

    return {
        "since_hours": since_hours,
        "counters": counters,
        "events": merged,
    }
