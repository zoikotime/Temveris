"""RBAC + ABAC authorization for the offboarding domain (§4).

Two layers, both enforced server-side:

  RBAC — does the actor hold a role granting the *permission* an action needs?
         13 roles, each with an explicit permission set (PERMISSIONS matrix).
  ABAC — even with the permission, do the *attributes* line up? tenant must
         match (cross-tenant is always denied), a manager only acts within
         their department, destructive steps are blocked while a legal hold is
         active, etc.

A decision is allowed only when BOTH pass. Sensitive fields (termination reason,
legal-hold notes, payroll/billing notes) are additionally redacted at read time
by ``redact_case`` based on field-level permissions.

The product ships today with four legacy ``User.role`` strings
(employee/hr/admin/super_admin); ``roles_for_user`` maps those onto this richer
taxonomy so the matrix is the single authority while real per-role assignment
can be layered on later (a ``user.offboarding_roles`` attribute is honored if
present).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Set


# ════════════════════════════════════════════════════════════════════════════
# ROLES (§4)
# ════════════════════════════════════════════════════════════════════════════
class Role(str, enum.Enum):
    SUPER_ADMIN       = "super_admin"
    HR_ADMIN          = "hr_admin"
    HR_LEAD           = "hr_lead"
    MANAGER           = "manager"
    IT_SECURITY_ADMIN = "it_security_admin"
    PAYROLL_ADMIN     = "payroll_admin"
    PAYROLL_LEAD      = "payroll_lead"
    FINANCE_ADMIN     = "finance_admin"
    FINANCE_LEAD      = "finance_lead"
    LEGAL_ADMIN       = "legal_admin"
    COMPLIANCE_ADMIN  = "compliance_admin"
    SUPPORT_ADMIN     = "support_admin"
    FORMER_WORKER     = "former_worker"


# ════════════════════════════════════════════════════════════════════════════
# PERMISSIONS  — fine-grained actions a role may be granted
# ════════════════════════════════════════════════════════════════════════════
class Permission(str, enum.Enum):
    CASE_CREATE   = "case.create"
    CASE_VIEW     = "case.view"
    CASE_UPDATE   = "case.update"
    CASE_SUBMIT   = "case.submit"
    CASE_APPROVE  = "case.approve"
    CASE_CANCEL   = "case.cancel"
    CASE_REVERSE  = "case.reverse"

    ACCESS_REVOKE   = "access.revoke"
    ACCESS_OVERRIDE = "access.override"      # force-resolve a failed revocation
    TRACKING_STOP   = "tracking.stop_verify"

    PAYROLL_CLOSE = "payroll.close"
    BILLING_CLOSE = "billing.close"
    RETENTION_ASSIGN = "retention.assign"

    LEGAL_HOLD_APPLY   = "legal_hold.apply"
    LEGAL_HOLD_RELEASE = "legal_hold.release"

    FORMER_ACCESS_GRANT  = "former_access.grant"
    FORMER_ACCESS_REVOKE = "former_access.revoke"

    CERTIFICATE_GENERATE = "certificate.generate"

    TASK_COMPLETE  = "task.complete"
    BLOCKER_RAISE  = "blocker.raise"
    BLOCKER_RESOLVE = "blocker.resolve"
    WAIVER_GRANT   = "waiver.grant"
    WAIVER_APPROVE = "waiver.approve"

    AUDIT_VIEW = "audit.view"

    # Field-level (read) permissions for sensitive columns.
    FIELD_TERMINATION_REASON = "field.termination_reason"
    FIELD_LEGAL_NOTES        = "field.legal_notes"
    FIELD_PAYROLL_NOTES      = "field.payroll_notes"
    FIELD_BILLING_NOTES      = "field.billing_notes"


_ALL_PERMISSIONS: FrozenSet[Permission] = frozenset(Permission)


# Role → granted permissions. Separation of duties is intentional: approval,
# payroll, billing, legal and security are distinct grants so no single
# non-super-admin role can drive a case end-to-end alone.
PERMISSIONS: dict[Role, FrozenSet[Permission]] = {
    Role.SUPER_ADMIN: _ALL_PERMISSIONS,

    Role.HR_ADMIN: frozenset({
        Permission.CASE_CREATE, Permission.CASE_VIEW, Permission.CASE_UPDATE,
        Permission.CASE_SUBMIT, Permission.CASE_APPROVE, Permission.CASE_CANCEL,
        Permission.TASK_COMPLETE, Permission.BLOCKER_RAISE, Permission.BLOCKER_RESOLVE,
        Permission.WAIVER_GRANT, Permission.AUDIT_VIEW,
        Permission.FIELD_TERMINATION_REASON,
    }),
    Role.HR_LEAD: frozenset({
        Permission.CASE_CREATE, Permission.CASE_VIEW, Permission.CASE_UPDATE,
        Permission.CASE_SUBMIT, Permission.CASE_APPROVE, Permission.CASE_CANCEL,
        Permission.CASE_REVERSE, Permission.TASK_COMPLETE,
        Permission.BLOCKER_RAISE, Permission.BLOCKER_RESOLVE,
        Permission.WAIVER_GRANT, Permission.WAIVER_APPROVE,
        Permission.CERTIFICATE_GENERATE, Permission.AUDIT_VIEW,
        Permission.FIELD_TERMINATION_REASON,
    }),
    Role.MANAGER: frozenset({
        Permission.CASE_CREATE, Permission.CASE_VIEW, Permission.TASK_COMPLETE,
        Permission.BLOCKER_RAISE,
    }),
    Role.IT_SECURITY_ADMIN: frozenset({
        Permission.CASE_VIEW, Permission.ACCESS_REVOKE, Permission.ACCESS_OVERRIDE,
        Permission.TRACKING_STOP, Permission.TASK_COMPLETE,
        Permission.BLOCKER_RAISE, Permission.BLOCKER_RESOLVE, Permission.AUDIT_VIEW,
    }),
    Role.PAYROLL_ADMIN: frozenset({
        Permission.CASE_VIEW, Permission.PAYROLL_CLOSE, Permission.TASK_COMPLETE,
        Permission.BLOCKER_RAISE, Permission.FIELD_PAYROLL_NOTES,
    }),
    Role.PAYROLL_LEAD: frozenset({
        Permission.CASE_VIEW, Permission.PAYROLL_CLOSE, Permission.TASK_COMPLETE,
        Permission.BLOCKER_RAISE, Permission.BLOCKER_RESOLVE,
        Permission.WAIVER_GRANT, Permission.FIELD_PAYROLL_NOTES,
    }),
    Role.FINANCE_ADMIN: frozenset({
        Permission.CASE_VIEW, Permission.BILLING_CLOSE, Permission.TASK_COMPLETE,
        Permission.BLOCKER_RAISE, Permission.FIELD_BILLING_NOTES,
    }),
    Role.FINANCE_LEAD: frozenset({
        Permission.CASE_VIEW, Permission.BILLING_CLOSE, Permission.TASK_COMPLETE,
        Permission.BLOCKER_RAISE, Permission.BLOCKER_RESOLVE,
        Permission.WAIVER_GRANT, Permission.FIELD_BILLING_NOTES,
    }),
    Role.LEGAL_ADMIN: frozenset({
        Permission.CASE_VIEW, Permission.LEGAL_HOLD_APPLY, Permission.LEGAL_HOLD_RELEASE,
        Permission.TASK_COMPLETE, Permission.BLOCKER_RAISE, Permission.BLOCKER_RESOLVE,
        Permission.AUDIT_VIEW, Permission.FIELD_LEGAL_NOTES,
    }),
    Role.COMPLIANCE_ADMIN: frozenset({
        Permission.CASE_VIEW, Permission.RETENTION_ASSIGN, Permission.AUDIT_VIEW,
        Permission.FIELD_LEGAL_NOTES,
    }),
    Role.SUPPORT_ADMIN: frozenset({
        Permission.CASE_VIEW,
    }),
    Role.FORMER_WORKER: frozenset(),  # portal-only; gated by FormerWorker checks, not here
}


# Field name → permission required to read it (§4 field-level protection).
SENSITIVE_FIELDS: dict[str, Permission] = {
    "termination_reason": Permission.FIELD_TERMINATION_REASON,
    "legal_hold_notes":   Permission.FIELD_LEGAL_NOTES,
    "payroll_notes":      Permission.FIELD_PAYROLL_NOTES,
    "billing_notes":      Permission.FIELD_BILLING_NOTES,
}

REDACTED = "***redacted***"


# ════════════════════════════════════════════════════════════════════════════
# LEGACY ROLE MAPPING
# ════════════════════════════════════════════════════════════════════════════
# Map the four legacy User.role strings onto offboarding roles. `admin` is a
# broad operator (everything except super-admin's reverse/override-all and
# cross-cutting field views it isn't granted); `hr` is HR operations.
_LEGACY_MAP: dict[str, Set[Role]] = {
    "super_admin": {Role.SUPER_ADMIN},
    "admin": {
        Role.HR_ADMIN, Role.IT_SECURITY_ADMIN, Role.PAYROLL_ADMIN,
        Role.FINANCE_ADMIN, Role.LEGAL_ADMIN, Role.COMPLIANCE_ADMIN,
        Role.SUPPORT_ADMIN,
    },
    "hr": {Role.HR_ADMIN},
    "employee": set(),
}


def roles_for_user(user) -> FrozenSet[Role]:
    """Resolve a User to offboarding roles. Honors an explicit
    ``user.offboarding_roles`` (list of role strings) if present, else falls
    back to the legacy ``user.role`` mapping."""
    explicit = getattr(user, "offboarding_roles", None)
    if explicit:
        out: Set[Role] = set()
        for r in explicit:
            try:
                out.add(Role(r))
            except ValueError:
                continue
        if out:
            return frozenset(out)
    return frozenset(_LEGACY_MAP.get(getattr(user, "role", "") or "", set()))


def permissions_for(roles: FrozenSet[Role]) -> FrozenSet[Permission]:
    granted: Set[Permission] = set()
    for r in roles:
        granted |= PERMISSIONS.get(r, frozenset())
    return frozenset(granted)


# ════════════════════════════════════════════════════════════════════════════
# CONTEXT + RESOURCE ATTRIBUTES (ABAC)
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AuthzContext:
    user_id: Optional[int]
    tenant_id: str
    roles: FrozenSet[Role]
    department: Optional[str] = None
    jurisdiction: Optional[str] = None

    @property
    def permissions(self) -> FrozenSet[Permission]:
        return permissions_for(self.roles)

    def has(self, perm: Permission) -> bool:
        return perm in self.permissions


@dataclass(frozen=True)
class ResourceAttrs:
    tenant_id: str
    department: Optional[str] = None
    jurisdiction: Optional[str] = None
    worker_type: Optional[str] = None
    exit_type: Optional[str] = None
    risk_level: Optional[str] = None
    legal_hold_status: str = "none"
    case_status: Optional[str] = None
    data_classification: Optional[str] = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


# Action → permission it requires. The router/services authorize by action
# name so call sites read declaratively.
ACTION_PERMISSION: dict[str, Permission] = {
    "case.create":   Permission.CASE_CREATE,
    "case.view":     Permission.CASE_VIEW,
    "case.update":   Permission.CASE_UPDATE,
    "case.submit":   Permission.CASE_SUBMIT,
    "case.approve":  Permission.CASE_APPROVE,
    "case.cancel":   Permission.CASE_CANCEL,
    "case.reverse":  Permission.CASE_REVERSE,
    "access.revoke": Permission.ACCESS_REVOKE,
    "access.override": Permission.ACCESS_OVERRIDE,
    "tracking.stop_verify": Permission.TRACKING_STOP,
    "payroll.close": Permission.PAYROLL_CLOSE,
    "billing.close": Permission.BILLING_CLOSE,
    "retention.assign": Permission.RETENTION_ASSIGN,
    "legal_hold.apply":   Permission.LEGAL_HOLD_APPLY,
    "legal_hold.release": Permission.LEGAL_HOLD_RELEASE,
    "former_access.grant":  Permission.FORMER_ACCESS_GRANT,
    "former_access.revoke": Permission.FORMER_ACCESS_REVOKE,
    "certificate.generate": Permission.CERTIFICATE_GENERATE,
    "task.complete":  Permission.TASK_COMPLETE,
    "blocker.raise":  Permission.BLOCKER_RAISE,
    "blocker.resolve": Permission.BLOCKER_RESOLVE,
    "waiver.grant":   Permission.WAIVER_GRANT,
    "waiver.approve": Permission.WAIVER_APPROVE,
    "audit.view":     Permission.AUDIT_VIEW,
}

# Actions that mutate/destroy worker data and are therefore blocked while a
# legal hold is ACTIVE (except for legal roles managing the hold itself).
_LEGAL_HOLD_BLOCKED = frozenset({
    "case.cancel", "certificate.generate", "retention.assign",
})


class AuthorizationError(PermissionError):
    """Raised by ``require`` when a decision is denied. Carries the reason so
    the router maps it to a clean 403."""
    def __init__(self, decision: "Decision"):
        self.decision = decision
        super().__init__(decision.reason or "forbidden")


def evaluate(ctx: AuthzContext, action: str, resource: Optional[ResourceAttrs]) -> Decision:
    """Combined RBAC + ABAC decision for ``action`` on ``resource``."""
    # ── RBAC ─────────────────────────────────────────────────────────────────
    perm = ACTION_PERMISSION.get(action)
    if perm is None:
        return Decision(False, f"unknown action '{action}'")
    if not ctx.has(perm):
        return Decision(False, f"missing permission {perm.value}")

    if resource is None:
        return Decision(True)

    # ── ABAC ─────────────────────────────────────────────────────────────────
    # Tenant isolation: cross-tenant access is never allowed, regardless of role.
    if resource.tenant_id != ctx.tenant_id:
        return Decision(False, "cross_tenant_denied")

    # Manager scope: a manager may only act within their own department.
    if Role.MANAGER in ctx.roles and Role.SUPER_ADMIN not in ctx.roles \
            and not (ctx.roles - {Role.MANAGER}):
        if resource.department and ctx.department and resource.department != ctx.department:
            return Decision(False, "department_scope_denied")

    # Legal hold: block destructive lifecycle steps while a hold is active,
    # unless the actor is exercising a legal-hold permission (release/apply).
    if resource.legal_hold_status == "active" and action in _LEGAL_HOLD_BLOCKED:
        if not (ctx.has(Permission.LEGAL_HOLD_RELEASE) or Role.SUPER_ADMIN in ctx.roles):
            return Decision(False, "legal_hold_active")

    return Decision(True)


def require(ctx: AuthzContext, action: str, resource: Optional[ResourceAttrs] = None) -> None:
    """Raise AuthorizationError unless the action is permitted."""
    decision = evaluate(ctx, action, resource)
    if not decision.allowed:
        raise AuthorizationError(decision)


def can(ctx: AuthzContext, action: str, resource: Optional[ResourceAttrs] = None) -> bool:
    return evaluate(ctx, action, resource).allowed


# ════════════════════════════════════════════════════════════════════════════
# FIELD-LEVEL PROTECTION
# ════════════════════════════════════════════════════════════════════════════
def can_view_field(ctx: AuthzContext, field_name: str) -> bool:
    perm = SENSITIVE_FIELDS.get(field_name)
    if perm is None:
        return True  # not a protected field
    return ctx.has(perm)


def redact_case(data: dict, ctx: AuthzContext) -> dict:
    """Return a copy of a case dict with sensitive fields the actor can't read
    replaced by a redaction marker. Non-destructive (does not mutate input)."""
    out = dict(data)
    for field_name in SENSITIVE_FIELDS:
        if field_name in out and out[field_name] is not None and not can_view_field(ctx, field_name):
            out[field_name] = REDACTED
    return out
