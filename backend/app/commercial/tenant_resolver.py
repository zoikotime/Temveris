"""Resolve the commercial organization for an authenticated user.

WHY THIS EXISTS
---------------
The commercial layer used to answer "which organization is this user's?" by
matching ownership::

    CommercialOrganization.owner_user_id == current_user.id

That works for the person who signed up and for nobody else. An employee
invited into the same tenant owns nothing, so the lookup returned ``None`` and
they fell through to a synthetic ``tenant_id: "default"`` entitlement profile
and a null onboarding checklist. Any future seat or feature enforcement built
on that resolver would therefore have applied to one user per organization —
which is precisely the case where a seat limit is meaningless.

Now that both provisioning paths populate
``CommercialOrganization.operational_org_id``, membership is expressible
directly::

    User.organization_id -> Organization.id -> CommercialOrganization.operational_org_id

That is a *membership* question rather than an *ownership* question, so it
resolves identically for the founding admin and for everyone they invite.

TENANT SAFETY
-------------
The organization id is always derived from server-side user state. Nothing here
accepts an organization id from a caller.

Ownership matching is kept as an explicit, narrow fallback: organizations
created by the self-serve path *before* ``operational_org_id`` was populated
still have it NULL, and their owner must keep working until the repair
migration runs. The fallback only ever matches the owner's own row, so it
cannot widen access to anyone else.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from database.models import User
from app.commercial.models import CommercialOrganization

_log = logging.getLogger("zoiko.commercial.tenant_resolver")


def resolve_commercial_org(
    db: Session,
    user: User,
    *,
    allow_owner_fallback: bool = True,
) -> Optional[CommercialOrganization]:
    """Return the caller's CommercialOrganization, or None.

    Resolution order:
      1. By operational tenant membership (``operational_org_id``) — works for
         every member of the organization, not just its owner.
      2. By ownership, for legacy self-serve organizations provisioned before
         the operational link existed.
    """
    org_id = getattr(user, "organization_id", None)

    if org_id is not None:
        org = (
            db.query(CommercialOrganization)
              .filter(CommercialOrganization.operational_org_id == org_id)
              .first()
        )
        if org is not None:
            return org

    if not allow_owner_fallback:
        return None

    # Legacy path. Deliberately last, and deliberately narrow.
    org = (
        db.query(CommercialOrganization)
          .filter(CommercialOrganization.owner_user_id == user.id)
          .first()
    )
    if org is not None and org.operational_org_id is None:
        _log.info(
            "Resolved commercial org %s for user %s via the ownership fallback — "
            "operational_org_id is NULL and should be backfilled.",
            org.id, user.id,
        )
    return org


def resolve_commercial_org_id(db: Session, user: User) -> Optional[str]:
    """Convenience wrapper returning just the id."""
    org = resolve_commercial_org(db, user)
    return org.id if org is not None else None
