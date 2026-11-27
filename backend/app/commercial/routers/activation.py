"""GET /api/v1/activation/status (Phase 4 §1).

Read-only provisioning/activation status for the Workspace Preparing → Ready
frontend. Look up by signup_id (post-signup, pre-verification), workspace_id, or
organization_id — all unguessable ids, and the DTO carries no PII.

There is also a session-scoped variant, ``/status/me``. The activation flow
navigates to Step 3 with no query string — the visitor is signed in by then, so
the tenant is derived from the session rather than from an id in the URL. That
variant is the only one allowed to return anything identifying (the owner's
first name for the Ready screen's greeting), precisely because it is the only
one that knows who is asking.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User, OrgSettings
from app.auth.service import get_current_user
from app.commercial import activation_status, provisioning
from app.commercial.models import CommercialWorkspace
from app.commercial.tenant_resolver import resolve_commercial_org
from app.commercial.routers._helpers import error_payload

_log = logging.getLogger("zoiko.commercial.routers.activation")
router = APIRouter(prefix="/api/v1/activation", tags=["Commercial v1"])


@router.get("/status")
def get_status(
    signup_id: str | None = Query(default=None),
    workspace_id: str | None = Query(default=None),
    organization_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if not (signup_id or workspace_id or organization_id):
        return JSONResponse(
            status_code=400,
            content=error_payload(
                code="VALIDATION_FAILED",
                message="Provide signup_id, workspace_id, or organization_id.",
                recoverable=True,
            ),
        )
    try:
        return activation_status.compute(
            db, signup_id=signup_id, workspace_id=workspace_id,
            organization_id=organization_id,
        )
    except activation_status.ActivationNotFound:
        return JSONResponse(
            status_code=404,
            content=error_payload(
                code="VALIDATION_FAILED",
                message="No activation found for the provided identifier.",
                recoverable=False,
            ),
        )


def _own_workspace(db: Session, user: User):
    """The caller's commercial organization and workspace, or (None, None).

    Resolved through ``resolve_commercial_org`` — membership of the operational
    tenant on the session — so this can only ever answer for the caller's own
    organization. No id is accepted from the request.
    """
    org = resolve_commercial_org(db, user)
    if org is None:
        return None, None
    ws = db.query(CommercialWorkspace).filter_by(organization_id=org.id).first()
    return org, ws


@router.get("/status/me")
def get_my_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Activation status for the signed-in user's own tenant.

    This is what Step 3 and Step 4 poll. Tenant isolation is structural: the
    organization comes from the session, so there is no id a caller could
    substitute to read someone else's provisioning state.
    """
    org, ws = _own_workspace(db, current_user)
    if org is None:
        raise HTTPException(404, "No workspace is associated with this account")

    payload = activation_status.compute(
        db, organization_id=org.id, workspace_id=ws.id if ws else None,
        user_id=int(current_user.id),
    )

    # Identifying extras, safe here and only here: the caller is authenticated
    # and the data is their own.
    settings = (
        db.query(OrgSettings)
          .filter(OrgSettings.organization_id == org.operational_org_id)
          .first()
        if org.operational_org_id else None
    )
    payload["owner_first_name"] = (current_user.name or current_user.email).split()[0]
    payload["organization_name"] = org.name
    # The name the admin chose on Step 2. `CommercialWorkspace.slug` is the
    # machine-generated one from provisioning and is not what they picked.
    payload["workspace_slug"] = getattr(settings, "slug", None)
    return payload


@router.post("/retry")
def retry_own_provisioning(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run provisioning for the caller's own workspace, if it needs it.

    Exposes the retry the admin console already has
    (``provisioning.retry_provisioning``) to the person actually waiting on it,
    scoped to their own tenant. No second provisioning path is introduced —
    this calls the same function, which recreates only what is genuinely
    missing and recomputes entitlements.

    Guarded so it cannot become a way to re-run provisioning at will:

    * a workspace that is already ``ready`` returns immediately, writing
      nothing and emitting nothing;
    * a ``failed_terminal`` workspace is refused — that state exists to stop
      automatic recovery attempts;
    * anything else is retried, and the operation is idempotent, so a
      double-click, two tabs or a network retry converge on one outcome.
    """
    org, ws = _own_workspace(db, current_user)
    if org is None or ws is None:
        raise HTTPException(404, "No workspace is associated with this account")

    if ws.provisioning_state == "ready":
        return {"retried": False, "reason": "already_ready",
                "provisioning_state": ws.provisioning_state}

    if ws.provisioning_state == "failed_terminal":
        raise HTTPException(
            409,
            "This workspace needs manual recovery. Our operations team has been alerted.",
        )

    try:
        result = provisioning.retry_provisioning(db, workspace_id=ws.id)
        db.commit()
    except Exception:
        db.rollback()
        _log.exception("Self-service provisioning retry failed for workspace %s", ws.id)
        raise HTTPException(
            500,
            "We couldn't finish preparing your workspace. Our team has been alerted.",
        )

    return {"retried": True, "provisioning_state": result.get("provisioning_state")}
