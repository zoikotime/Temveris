"""GET /api/v1/entitlements/current (§14.5).

Resolves the entitlement object for the authenticated user's tenant.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user
from app.commercial import entitlements, identity, projection
from app.commercial.models import CommercialOrganization
from app.commercial.tenant_resolver import resolve_commercial_org


router = APIRouter(prefix="/api/v1", tags=["Commercial v1"])


@router.get("/entitlements/current")
def get_current_entitlements(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Resolved by operational-tenant membership, not ownership, so every member
    # of the organization gets its real entitlement profile. Ownership-only
    # resolution meant an invited employee fell through to the synthetic
    # "default" profile below, which would make any seat or feature enforcement
    # apply to exactly one user per tenant.
    org = resolve_commercial_org(db, current_user)
    if org is None:
        # Legacy single-tenant user — return a synthetic profile so frontend
        # code can run without breaking. App actions still gate on the
        # restrictions block.
        return {
            "tenant_id": "default",
            "plan": "verified",
            "commercial_state": "trial_active",
            "trial_overlay": False,
            "features": {
                "time_tracking": True, "dashboards": True, "audit_logs": True,
                "advanced_compliance": False, "legal_hold": False, "sso": False,
                "scim": False, "api_access": False, "data_export": False,
            },
            "limits": {
                "seat_limit": None, "retention_days": 365,
                "api_requests_per_day": 0, "workspace_count": 1,
            },
            "restrictions": {"email_unverified": False, "billing_required": False,
                             "manual_review_required": False},
            "computed_at": None,
            "expires_at": None,
        }

    # Email verification is an ORGANIZATION-level expansion gate (spec I-6): it
    # decides whether this tenant may invite and export, not what one member may
    # do. So it keys on the organization's owner, matching
    # /entitlements/by-org/{id} below.
    #
    # This also avoids a false restriction now that non-owners resolve to a real
    # organization: an invited employee activates through the invite link, never
    # through a verification token, so is_email_verified() is always False for
    # them. Keying on the caller would have permanently marked every invited
    # employee email_unverified.
    owner_verified = (
        identity.is_email_verified(db, org.owner_user_id)
        if org.owner_user_id else False
    )
    profile = entitlements.get(db, org.id, email_verified=owner_verified)
    return profile


@router.get("/entitlements/projection")
def get_actor_projection(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Actor-aware presentation projection for the caller's workspace (§7, §17).

    A read-only sibling of ``/entitlements/current``. ``/current`` returns the
    workspace-level truth; this returns the same decisions plus a per-capability
    SHOW/LOCK/HIDE/READ_ONLY/CONFIGURATION_REQUIRED presentation hint for the
    current actor's role. It is a distinct operation (§17 lists workspace
    resolution and actor projection separately), so ``/current`` is left
    unchanged for its existing consumers.

    Presentation is derived per request from the actor's role and never cached —
    the entitlement cache is keyed on organization alone, so caching a role-aware
    view there would let one actor see another's projection. Nothing here
    enforces or writes; a denied capability can never project as SHOW.
    """
    org = resolve_commercial_org(db, current_user)
    if org is None:
        # Fail closed, consistent with the entitlement engine: an unresolvable
        # tenant is entitled to nothing, so everything hides. We deliberately do
        # NOT reuse the permissive synthetic default that /current returns for
        # legacy users — a presentation must never over-show.
        empty = entitlements._empty_profile(
            "default", plan="verified", state="manual_review")
        return projection.build_actor_projection(empty, role=current_user.role)

    owner_verified = (
        identity.is_email_verified(db, org.owner_user_id)
        if org.owner_user_id else False
    )
    profile = entitlements.get(db, org.id, email_verified=owner_verified)
    return projection.build_actor_projection(profile, role=current_user.role)


@router.get("/entitlements/by-org/{organization_id}")
def get_org_entitlements(
    organization_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Platform-level entitlement lookup by commercial org ID.
    Restricted to super_admin — tenant admins must use /entitlements/current
    which is automatically scoped to their own organization."""
    if current_user.role != "super_admin":
        raise HTTPException(403, "Super admin access required")
    org = db.query(CommercialOrganization).filter_by(id=organization_id).first()
    if org is None:
        raise HTTPException(404, "Organization not found")
    owner_verified = (
        identity.is_email_verified(db, org.owner_user_id) if org.owner_user_id else False
    )
    return entitlements.get(db, organization_id, email_verified=owner_verified)
