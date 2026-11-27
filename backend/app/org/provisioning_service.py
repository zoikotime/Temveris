"""Shared operational tenant provisioning.

Single source of truth for "bring a new tenant into existence". Both entry
points call it:

  * ``app.super_admin.router.provision_organization``  (created_via="super_admin")
  * ``app.commercial.routers.signup``                  (created_via="self_service")

WHY THIS MODULE EXISTS
----------------------
Operational tenant creation previously lived *inside* the super-admin HTTP
handler, while the self-serve signup path had its own, incomplete
implementation that created a ``CommercialOrganization`` but no operational
``Organization`` — leaving ``users.organization_id`` NULL. Because every
org-scoped read is written as::

    Model.organization_id == current_user.organization_id

and SQLAlchemy compiles a Python ``None`` right-hand side to ``IS NULL``,
that filter *succeeds* and returns every org-less row. All self-serve
tenants therefore shared one bucket and could read each other's data.

The two paths had already diverged once before over exactly this kind of
disagreement — migration ``c4a7e2b915d3`` exists to repair ``app_settings``
rows that landed in the shared NULL-organization bucket because one path
stamped ``organization_id`` and the other did not. Having one function
removes the class of bug rather than fixing one instance of it.

TRANSACTION CONTRACT
--------------------
**This module flushes; it never commits and never rolls back.** The caller
owns the transaction boundary, so a caller can compose tenant provisioning
with its own writes in a single atomic unit. Callers MUST:

    try:
        result = provision_tenant(db, ...)
        ...            # optional additional writes in the same transaction
        db.commit()
    except Exception:
        db.rollback()
        raise

This mirrors ``app.commercial.provisioning.provision_tenant``, which is
also flush-only.

ERROR CONTRACT
--------------
Validation failures raise :class:`TenantProvisioningError`, which carries
both an HTTP status code and a stable machine-readable ``code``. It is
deliberately framework-agnostic so each caller can render it in its own
error format — the super-admin portal re-raises it as an ``HTTPException``
with an unchanged status and detail, while the commercial signup router
renders it through the §15 ``error_payload`` envelope.

OWNERSHIP
---------
The initial admin gets the existing ``admin`` role — no new role is
introduced. Ownership is recorded explicitly on the two existing ownership
columns, ``OrgSettings.owner_user_id`` and
``CommercialOrganization.owner_user_id``. No ``organizations.owner_id``
column is added; that would be a third source of truth for the same fact.

Writing ``OrgSettings.owner_user_id`` also removes a read-time fallback:
``app.org.router._resolve_owner`` previously had to guess the owner as
"earliest-created admin/hr in the org" whenever the column was NULL. For a
freshly provisioned tenant the explicit value is that same user, so the
``/org/settings`` response is unchanged — the guess is simply no longer
needed.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from database import models
from app._tz import is_valid_timezone

_log = logging.getLogger("zoiko.org.provisioning")

# Same rule the super-admin portal has always enforced on slugs.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Recognised provenance values, recorded in the audit payload so operations
# can tell a self-served tenant from a manually provisioned one.
CREATED_VIA_SUPER_ADMIN = "super_admin"
CREATED_VIA_SELF_SERVICE = "self_service"

INVITE_TOKEN_TTL_HOURS = 24
DEFAULT_SEED_TEAMS = ["Engineering", "HR", "Sales", "Operations"]

# Error codes. Stable strings — callers map these onto their own error
# envelopes, so treat them as part of the contract.
ERR_INVALID_ADMIN_EMAIL = "INVALID_ADMIN_EMAIL"
ERR_ORG_NAME_REQUIRED = "ORG_NAME_REQUIRED"
ERR_INVALID_SLUG = "INVALID_SLUG"
ERR_EMAIL_ALREADY_ACTIVE = "EMAIL_ALREADY_ACTIVE"
ERR_USER_ALREADY_IN_ORG = "USER_ALREADY_IN_ORG"


class TenantProvisioningError(Exception):
    """A provisioning request that must be rejected before it is attempted.

    Carries an HTTP status code (so the super-admin portal's existing
    responses are byte-for-byte unchanged) and a stable machine code (so the
    commercial layer can render its own error envelope).
    """

    def __init__(
        self,
        *,
        status_code: int,
        detail: str,
        code: str,
        field: Optional[str] = None,
    ):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.field = field


@dataclass
class TenantProvisioningResult:
    """Everything the callers need, without re-querying."""

    organization: models.Organization
    admin_user: models.User
    org_settings: models.OrgSettings
    commercial_org: Optional[Any] = None
    # Populated only in the invite-credential mode (super-admin path).
    invite_token: Optional[str] = None
    invite_token_expires_at: Optional[datetime] = None
    policies_seeded: int = 0
    teams_seeded: int = 0
    app_settings_seeded: int = 0
    team_names: List[str] = field(default_factory=list)
    # True when an existing, un-activated account was adopted rather than a
    # new User row created (the super-admin re-invite path).
    reused_existing_user: bool = False

    @property
    def organization_id(self) -> int:
        return self.organization.id

    @property
    def commercial_org_id(self) -> Optional[str]:
        return getattr(self.commercial_org, "id", None)


def _commercial_models():
    """Lazy-import the commercial models.

    Kept lazy (and failure-tolerant) for the same reason the super-admin
    portal does it: the legacy boot path must not hard-fail when the
    commercial tables have not been migrated yet.
    """
    try:
        from app.commercial import models as cm
        return cm
    except Exception as e:  # pragma: no cover - only on a broken install
        _log.warning("commercial models unavailable: %s", e)
        return None


def _derive_name_from_email(email: str) -> str:
    prefix = email.split("@")[0]
    return prefix.replace(".", " ").replace("_", " ").replace("-", " ").title()


def provision_tenant(
    db: Session,
    *,
    org_name: str,
    admin_email: str,
    created_via: str,
    admin_name: Optional[str] = None,
    # Identity / credential mode ------------------------------------------------
    # Pass an already-created User row to adopt it as the tenant's initial
    # admin (the self-serve path, where signup creates the user with a chosen
    # password before provisioning). Leave it None to have this function
    # create the admin with a 24h invite token instead (the super-admin path).
    existing_admin_user: Optional[models.User] = None,
    actor_user_id: Optional[int] = None,
    # Organization profile -----------------------------------------------------
    country: Optional[str] = None,
    timezone_name: Optional[str] = None,
    currency: Optional[str] = None,
    company_name: Optional[str] = None,
    slug: Optional[str] = None,
    # Seeding ------------------------------------------------------------------
    seed_leave_policies: bool = True,
    seed_default_teams: bool = False,
    default_teams: Optional[List[str]] = None,
    shared_pool_enabled: bool = True,
    leave_waiting_period_months: int = 6,
    # Commercial layer ---------------------------------------------------------
    create_commercial_org: bool = True,
    commercial_state: str = "active",
    commercial_org_id: Optional[str] = None,
    expected_users: Optional[int] = None,
    domain: Optional[str] = None,
    legal_name: Optional[str] = None,
    # Audit --------------------------------------------------------------------
    audit_ip: Optional[str] = None,
    extra_audit_payload: Optional[dict] = None,
) -> TenantProvisioningResult:
    """Provision a complete operational tenant. Flush-only — see module docs.

    Establishes, in order:
      1. ``Organization``                     (integer PK — the tenant key)
      2. ``OrgSettings``                      (+ explicit ``owner_user_id``)
      3. ``AppSettings``                      (org-stamped policy keys)
      4. ``LeavePolicy`` rows                 (optional)
      5. ``Team`` rows                        (optional)
      6. Initial admin ``User``               (created, or adopted)
      7. ``CommercialOrganization``           (linked via ``operational_org_id``)
      8. ``AuditLog``                         (``OrgProvisioned``)

    Raises:
        TenantProvisioningError: on any validation failure, before writes.
    """
    # ── Pre-flight validation (before any DB write) ───────────────────────
    admin_email = (admin_email or "").strip().lower()
    if not admin_email or "@" not in admin_email:
        raise TenantProvisioningError(
            status_code=400,
            detail="admin_email must be a valid email address",
            code=ERR_INVALID_ADMIN_EMAIL,
            field="admin_email",
        )

    org_name = (org_name or "").strip()
    if not org_name:
        raise TenantProvisioningError(
            status_code=400,
            detail="Organization name is required",
            code=ERR_ORG_NAME_REQUIRED,
            field="name",
        )

    slug = (slug or "").strip().lower() or None
    if slug and not SLUG_RE.match(slug):
        raise TenantProvisioningError(
            status_code=400,
            detail="slug must be lowercase alphanumeric with hyphens (e.g. acme-corp)",
            code=ERR_INVALID_SLUG,
            field="slug",
        )

    if created_via not in (CREATED_VIA_SUPER_ADMIN, CREATED_VIA_SELF_SERVICE):
        # Programmer error, not user input — surface loudly.
        raise ValueError(f"unknown created_via: {created_via!r}")

    waiting_months = max(0, min(36, int(leave_waiting_period_months)))

    # Resolve the initial admin account up front so validation happens before
    # the Organization row is written.
    reused_existing_user = False
    if existing_admin_user is not None:
        # Adoption mode: the caller already created (and flushed) the user.
        if existing_admin_user.organization_id is not None:
            raise TenantProvisioningError(
                status_code=409,
                detail="This account already belongs to an organization.",
                code=ERR_USER_ALREADY_IN_ORG,
                field="admin_email",
            )
        candidate_user = existing_admin_user
    else:
        # Invite mode. Block if the address belongs to a fully-activated
        # account in ANY org — same rule as the employee invite flow.
        candidate_user = (
            db.query(models.User).filter(models.User.email == admin_email).first()
        )
        if candidate_user and candidate_user.is_password_set:
            raise TenantProvisioningError(
                status_code=409,
                detail=(
                    "This email address already belongs to an active account. "
                    "Use a different address or impersonate the account and "
                    "reassign the org."
                ),
                code=ERR_EMAIL_ALREADY_ACTIVE,
                field="admin_email",
            )
        reused_existing_user = candidate_user is not None

    # ── Step 1 — Organization (the integer-PK tenant anchor) ──────────────
    org = models.Organization(name=org_name)
    db.add(org)
    db.flush()  # populate org.id without committing

    country_code = (country or "").upper()[:2] or None
    currency_code = (currency or "").upper()[:3] or None

    # Validate the timezone at this choke point, because both provisioning paths
    # pass through here and only one of them validates upstream.
    #
    # The super-admin handler rejects a missing or invalid zone with a 400 before
    # calling us. Self-service does not: `ctx.timezone` comes from the signup
    # payload's `client_context.timezone`, i.e. straight from the browser, so it
    # is client-supplied and may be absent, stale or crafted.
    #
    # An unloadable zone written here is worse than none: `app._tz._resolve`
    # silently falls back at read time, so the tenant would run its attendance
    # day, absence cutoffs and payroll periods on the fallback clock while
    # OrgSettings claimed otherwise. `is_valid_timezone` is documented as THE
    # validator for every write path including provisioning, so it is applied
    # here rather than duplicated per caller.
    #
    # Invalid becomes NULL rather than an error: the super-admin path has already
    # made a missing zone a hard 400 where a human can fix it, and for
    # self-service NULL is the correct "prompt the admin" state. Rejecting the
    # signup outright would fail a legitimate customer over a browser quirk.
    tz_candidate = (timezone_name or "").strip()
    default_timezone = tz_candidate if is_valid_timezone(tz_candidate) else None
    if tz_candidate and default_timezone is None:
        _log.warning(
            "Discarding invalid timezone %r while provisioning %r (created_via=%s) "
            "— leaving default_timezone NULL so the admin is prompted",
            tz_candidate, org_name, created_via,
        )

    # ── Step 2 — OrgSettings ──────────────────────────────────────────────
    org_settings = models.OrgSettings(
        organization_id=org.id,
        company_name=(company_name or org_name).strip(),
        slug=slug,
        primary_country=country_code,
        default_timezone=default_timezone,
        default_currency=currency_code,
    )
    db.add(org_settings)
    db.flush()

    # ── Step 3 — AppSettings: org-prefixed policy keys ────────────────────
    # organization_id is stamped as well as the key suffix. Without the column
    # these rows land in the shared NULL-organization bucket, where any
    # org-less read can pick them up (repaired by migration c4a7e2b915d3).
    # The leave readers match on the suffixed key, so the column is additive
    # and backward compatible.
    db.add(models.AppSettings(
        organization_id=org.id,
        key=f"shared_pool_enabled:{org.id}",
        value="true" if shared_pool_enabled else "false",
        description=f"Shared leave pool toggle for org {org.id}",
    ))
    db.add(models.AppSettings(
        organization_id=org.id,
        key=f"leave_waiting_period_months:{org.id}",
        value=str(waiting_months),
        description=f"Leave eligibility waiting period (months) for org {org.id}",
    ))
    db.flush()
    app_settings_seeded = 2

    # ── Step 4 — LeavePolicy rows ─────────────────────────────────────────
    policies_seeded = 0
    if seed_leave_policies:
        from app.leave.router import LEAVE_POLICIES  # lazy: avoids import cycle

        for leave_type, pol in LEAVE_POLICIES.items():
            db.add(models.LeavePolicy(
                organization_id=org.id,
                leave_type=leave_type,
                annual_limit=pol.get("limit", 0),
                monthly_accrual=pol.get("accrual", 0.0),
                max_carry_forward=0,
                requires_document=bool(pol.get("doc_required", False)),
                min_notice_days=pol.get("notice_days", 0),
                approval_chain=["manager", "hr"],
                is_active=True,
            ))
            policies_seeded += 1
        db.flush()

    # ── Step 5 — Teams ────────────────────────────────────────────────────
    teams_seeded = 0
    team_names: List[str] = []
    if seed_default_teams:
        for team_name in (default_teams or DEFAULT_SEED_TEAMS):
            tn = (team_name or "").strip()
            if tn:
                db.add(models.Team(organization_id=org.id, name=tn))
                team_names.append(tn)
                teams_seeded += 1
        db.flush()

    # ── Step 6 — Initial admin user ───────────────────────────────────────
    invite_token: Optional[str] = None
    token_expires: Optional[datetime] = None

    resolved_admin_name = (admin_name or "").strip() or _derive_name_from_email(admin_email)

    if existing_admin_user is not None:
        # Adoption: attach the caller's user to this tenant. The role is set
        # here — never taken from client input.
        admin_user = existing_admin_user
        admin_user.organization_id = org.id
        admin_user.role = "admin"
        if not (admin_user.name or "").strip():
            admin_user.name = resolved_admin_name
    else:
        invite_token = str(uuid.uuid4())
        token_expires = datetime.now(timezone.utc) + timedelta(hours=INVITE_TOKEN_TTL_HOURS)

        if reused_existing_user:
            # Re-invite path: same address, account not yet activated.
            admin_user = candidate_user
            admin_user.organization_id = org.id
            admin_user.role = "admin"
            admin_user.invite_token = invite_token
            admin_user.invite_token_expires_at = token_expires
            admin_user.is_invited = True
            admin_user.invited_by_id = actor_user_id
        else:
            admin_user = models.User(
                name=resolved_admin_name,
                email=admin_email,
                password=None,
                role="admin",
                organization_id=org.id,
                is_active=True,
                is_invited=True,
                is_password_set=False,
                invite_token=invite_token,
                invite_token_expires_at=token_expires,
                invited_by_id=actor_user_id,
            )
            db.add(admin_user)

    db.flush()  # populate admin_user.id

    # Ownership, recorded explicitly on the existing column rather than left
    # to the read-time "earliest admin" fallback in org.router._resolve_owner.
    org_settings.owner_user_id = admin_user.id
    org_settings.owner_since = datetime.now(timezone.utc)
    db.flush()

    # ── Step 7 — CommercialOrganization, linked to the operational org ────
    comm_org = None
    if create_commercial_org:
        cm = _commercial_models()
        if cm:
            comm_org = cm.CommercialOrganization(
                id=commercial_org_id or f"org_{uuid.uuid4().hex[:20]}",
                name=org_name,
                legal_name=legal_name,
                country=country_code,
                owner_user_id=admin_user.id,
                operational_org_id=org.id,
                expected_users=expected_users,
                domain=domain,
                commercial_state=commercial_state,
            )
            db.add(comm_org)
            db.flush()

    # ── Step 8 — Audit ────────────────────────────────────────────────────
    audit_payload = {
        "org_id": org.id,
        "org_name": org_name,
        "admin_email": admin_email,
        "slug": slug,
        "country": country_code,
        "created_via": created_via,
        "seed_leave_policies": seed_leave_policies,
        "seed_default_teams": seed_default_teams,
        "policies_seeded": policies_seeded,
        "teams_seeded": teams_seeded,
        "shared_pool_enabled": shared_pool_enabled,
        "leave_waiting_period_months": waiting_months,
    }
    if extra_audit_payload:
        audit_payload.update(extra_audit_payload)

    db.add(models.AuditLog(
        actor_id=actor_user_id if actor_user_id is not None else admin_user.id,
        target_id=admin_user.id,
        organization_id=org.id,
        entity="Organization",
        action="OrgProvisioned",
        payload=audit_payload,
        ip_address=audit_ip,
    ))
    db.flush()

    return TenantProvisioningResult(
        organization=org,
        admin_user=admin_user,
        org_settings=org_settings,
        commercial_org=comm_org,
        invite_token=invite_token,
        invite_token_expires_at=token_expires,
        policies_seeded=policies_seeded,
        teams_seeded=teams_seeded,
        app_settings_seeded=app_settings_seeded,
        team_names=team_names,
        reused_existing_user=reused_existing_user,
    )
