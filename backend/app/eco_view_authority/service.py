"""
The one authoritative ECO view-authority resolver.
==================================================

    require_eco_view_authority(db, caller=..., target_user_id=...)
        -> ViewAuthorityDecision

Owner ruling B2, Option B. It answers exactly one question: **may this caller
read this Worker's ECO records?** It returns a typed decision and reads nothing
else.

TENANT ISOLATION IS A PRECONDITION, NOT A CHECK
-----------------------------------------------
``organization_id`` is DERIVED FROM THE AUTHENTICATED CALLER. There is no
``organization_id`` parameter — not one that is validated, not one that is
ignored. A tenant boundary that can be named by the request is a tenant
boundary that can be argued with, so the signature simply has nowhere to put
one. A caller whose ``organization_id`` is NULL — which includes
``super_admin``, whose tenant id is cleared on login — holds no authority at
all.

WHAT IT NEVER DOES
------------------
* **No role strings.** Not ``User.role``, not a tuple of them, not a cascade.
  The grant is the authority; a job title is not.
* **No inference.** Not ``Team.head_user_id``, not ``department``, not
  ``designation``, not email, not manager or HR relationships.
* **No recipient resolution.** This says who may READ. It never selects a
  notification recipient, never creates a reviewer, manager or HR route, and
  never implies that anybody is told anything. ``RECIPIENT-AUTHORITY`` is a
  separate capability, satisfied by ``app.recipient_authority`` under Stage 5
  and imported by nothing here; GV-07 now waits on ALERT-DELIVERY alone.
* **No writing.** It issues no INSERT, UPDATE or DELETE, and grants nothing on
  demand. A missing grant is a denial, not a prompt to create one.

FAIL CLOSED, EVERY TIME
-----------------------
Absent grant, ambiguous grant, malformed grant, unknown caller, unknown target,
missing tenant context and cross-tenant target are all DENIED. There is no
path through this function that returns authority it did not positively
establish.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from database import models as dbm

from .models import EcoViewGrant
from .outcomes import DenialReason, ViewAuthorityDecision, ViewAuthorityOutcome


def _deny(reason: DenialReason, detail: str, *, caller_id=None,
          target_user_id=None, organization_id=None) -> ViewAuthorityDecision:
    return ViewAuthorityDecision(
        outcome=ViewAuthorityOutcome.DENIED,
        caller_id=caller_id,
        target_user_id=target_user_id,
        organization_id=organization_id,
        reason=reason,
        detail=detail,
    )


def classify_grants(
    grants: Sequence[EcoViewGrant], *, organization_id: int, user_id: int,
) -> Optional[DenialReason]:
    """Is this set of matched grant rows usable? ``None`` means yes.

    Separated out and pure so the defensive branches are testable. The unique
    constraint makes "more than one" unreachable through the ORM, and the query
    filters on exactly the tenant and user re-verified here — so both branches
    below are unreachable in normal operation and are checked anyway. A future
    schema slip, a hand-written migration or a direct SQL insert would
    otherwise pass silently, and this resolver is the last thing standing
    between that and somebody reading another tenant's attendance evidence.
    """
    if not grants:
        return DenialReason.NO_GRANT
    if len(grants) > 1:
        return DenialReason.GRANT_AMBIGUOUS
    grant = grants[0]
    if int(grant.organization_id) != int(organization_id) or \
            int(grant.user_id) != int(user_id):
        return DenialReason.GRANT_MALFORMED
    return None


def require_eco_view_authority(
    db: Session,
    *,
    caller,
    target_user_id: int,
) -> ViewAuthorityDecision:
    """Resolve ECO view authority for one caller over one Worker.

    ``caller`` is the AUTHENTICATED user object — the same one the platform's
    authentication layer already produces. Its ``organization_id`` is the only
    tenant boundary this function recognises.

    Returns, never raises, for every ordinary outcome. Reads only; the caller
    owns the transaction and nothing is flushed or committed.
    """
    caller_id = getattr(caller, "id", None)
    if caller is None or caller_id is None:
        return _deny(DenialReason.CALLER_UNKNOWN,
                     "no authenticated caller identity was supplied")
    caller_id = int(caller_id)

    # ── Tenant context, from the caller and only the caller ─────────────────
    organization_id = getattr(caller, "organization_id", None)
    if organization_id is None:
        return _deny(
            DenialReason.NO_TENANT_CONTEXT,
            f"user {caller_id} belongs to no organization, so it holds no "
            f"tenant-scoped capability; platform-owner status is not "
            f"tenant-administrator status",
            caller_id=caller_id, target_user_id=target_user_id,
        )
    organization_id = int(organization_id)

    # ── Grant 1: the Worker reading their own records ───────────────────────
    # Checked before any query: a Worker's authority over their own evidence is
    # a property of identity, not of anything stored, so it needs no grant row
    # and cannot be revoked by one being absent.
    if caller_id == int(target_user_id):
        return ViewAuthorityDecision(
            outcome=ViewAuthorityOutcome.WORKER_SELF,
            caller_id=caller_id,
            target_user_id=int(target_user_id),
            organization_id=organization_id,
            detail="the caller is the Worker these records belong to",
        )

    # ── The target must exist and must be inside the caller's tenant ────────
    target = db.execute(
        select(dbm.User).where(dbm.User.id == int(target_user_id))
    ).scalars().first()
    if target is None:
        return _deny(
            DenialReason.TARGET_UNKNOWN,
            f"no user {target_user_id} exists",
            caller_id=caller_id, target_user_id=target_user_id,
            organization_id=organization_id,
        )
    if target.organization_id is None or \
            int(target.organization_id) != organization_id:
        return _deny(
            DenialReason.CROSS_TENANT,
            f"user {target_user_id} does not belong to organization "
            f"{organization_id}; knowing a record identifier is not authority",
            caller_id=caller_id, target_user_id=target_user_id,
            organization_id=organization_id,
        )

    # ── Grant 2: the tenant's explicitly persisted ECO view capability ──────
    grants = db.execute(
        select(EcoViewGrant).where(
            EcoViewGrant.organization_id == organization_id,
            EcoViewGrant.user_id == caller_id,
        )
    ).scalars().all()

    refusal = classify_grants(
        grants, organization_id=organization_id, user_id=caller_id)
    if refusal is not None:
        return _deny(
            refusal,
            f"user {caller_id} holds no usable ECO view grant in organization "
            f"{organization_id} ({refusal.value})",
            caller_id=caller_id, target_user_id=target_user_id,
            organization_id=organization_id,
        )

    return ViewAuthorityDecision(
        outcome=ViewAuthorityOutcome.TENANT_ADMINISTRATOR,
        caller_id=caller_id,
        target_user_id=int(target_user_id),
        organization_id=organization_id,
        detail=(f"user {caller_id} holds the persisted ECO view grant for "
                f"organization {organization_id}"),
    )


__all__ = ["require_eco_view_authority", "classify_grants"]
