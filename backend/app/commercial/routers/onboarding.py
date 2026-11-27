"""Onboarding checklist + first-action milestones (§19)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database import models
from database.models import User
from app.auth.service import get_current_user
from app.commercial import constants, entitlements
from app.commercial.audit import emit_event
from app.commercial.enforcement import seat_limit_status
from app.commercial.models import (
    OnboardingChecklist, CommercialWorkspace, CommercialOrganization,
)
from app.commercial.tenant_resolver import resolve_commercial_org
from app.org import setup_state


router = APIRouter(prefix="/api/v1/onboarding", tags=["Commercial v1"])


def _own_org(db: Session, user: User) -> Optional[CommercialOrganization]:
    """The caller's commercial organization.

    Resolved by operational-tenant membership rather than by ownership, so an
    invited employee sees their organization's onboarding state instead of
    ``None``. See app.commercial.tenant_resolver.
    """
    return resolve_commercial_org(db, user)


@router.get("/checklist")
def get_checklist(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    org = _own_org(db, current_user)
    if org is None:
        return {"onboarding": None}
    cl = db.query(OnboardingChecklist).filter_by(organization_id=org.id).one_or_none()
    if cl is None:
        return {"onboarding": None}
    ws = db.query(CommercialWorkspace).filter_by(organization_id=org.id).first()
    return {
        "organization_id": org.id,
        "workspace_id": ws.id if ws else None,
        "checklist": {
            "email_confirmed": cl.email_confirmed,
            "first_policy_configured": cl.first_policy_configured,
            "first_invite_sent": cl.first_invite_sent,
            "dashboard_reviewed": cl.dashboard_reviewed,
            "tools_connected": cl.tools_connected,
            "billing_reviewed": cl.billing_reviewed,
        },
        "time_to_value": {
            "first_login_at": ws.first_login_at.isoformat() if ws and ws.first_login_at else None,
            "first_email_verified_at": ws.first_email_verified_at.isoformat() if ws and ws.first_email_verified_at else None,
            "first_policy_configured_at": ws.first_policy_configured_at.isoformat() if ws and ws.first_policy_configured_at else None,
            "first_invite_sent_at": ws.first_invite_sent_at.isoformat() if ws and ws.first_invite_sent_at else None,
            "first_teammate_accepted_at": ws.first_teammate_accepted_at.isoformat() if ws and ws.first_teammate_accepted_at else None,
            "first_work_session_at": ws.first_work_session_at.isoformat() if ws and ws.first_work_session_at else None,
            "first_report_viewed_at": ws.first_report_viewed_at.isoformat() if ws and ws.first_report_viewed_at else None,
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# Setup wizard state  (distinct from the activation checklist above)
#
# The checklist is commercial activation telemetry — six independent,
# self-attested booleans. The wizard is an ordered, resumable, terminal setup
# flow. Different facts, so different state; see app.org.setup_state.
#
# Steps 1 and 2 of the wizard do NOT get endpoints here: they reuse the existing
# PUT /org/settings and POST /org/teams APIs. Only the progress marker is new.
# ──────────────────────────────────────────────────────────────────────────

class SetupStepRequest(BaseModel):
    step: str


def _setup_payload(db: Session, user: User) -> dict:
    """Wizard state plus everything the UI needs to render its steps.

    Every value is derived from the authenticated user's organization. Nothing
    here accepts an organization id from the caller.
    """
    org_id = user.organization_id
    state = setup_state.get_state(db, org_id)

    settings_row = None
    team_count = 0
    if org_id is not None:
        settings_row = (
            db.query(models.OrgSettings)
              .filter(models.OrgSettings.organization_id == org_id)
              .first()
        )
        team_count = (
            db.query(models.Team)
              .filter(models.Team.organization_id == org_id)
              .count()
        )

    seat_limit, seats_used = seat_limit_status(db, user)
    team_limit = None
    comm = resolve_commercial_org(db, user)
    if comm is not None:
        try:
            profile = entitlements.get(db, comm.id)
            # Only report a cap the server will actually enforce — never a
            # decorative one. None means unlimited.
            team_limit = (profile.get("limits") or {}).get("team_limit")
        except Exception:
            team_limit = None

    return {
        **state,
        "organization": {
            "id": org_id,
            "company_name": settings_row.company_name if settings_row else None,
            "default_timezone": settings_row.default_timezone if settings_row else None,
            "primary_country": settings_row.primary_country if settings_row else None,
            "logo_url": settings_row.logo_url if settings_row else None,
        },
        "admin": {"id": user.id, "name": user.name, "email": user.email, "role": user.role},
        "teams": {"count": team_count, "limit": team_limit},
        "seats": {"used": seats_used, "limit": seat_limit},
        # Offered to the Workspace-details form. Served rather than hardcoded
        # client-side so the label shown and the number stored agree.
        "worker_count_bands": [dict(b) for b in constants.WORKER_COUNT_BANDS],
    }


@router.get("/setup")
def get_setup(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Current wizard state for the caller's organization."""
    return _setup_payload(db, current_user)


@router.post("/setup/step")
def advance_setup(
    body: SetupStepRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record wizard progress.

    Monotonic: a refresh, a browser back-navigation or a duplicate submit cannot
    rewind a tenant that has already moved on, and a completed tenant is never
    re-opened.
    """
    if current_user.organization_id is None:
        raise HTTPException(409, "No organization is associated with this account")
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(403, "Only an organization admin can run setup")
    try:
        setup_state.set_step(db, current_user.organization_id, body.step)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return _setup_payload(db, current_user)


@router.post("/setup/complete")
def complete_setup(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Finish the wizard. Idempotent and terminal."""
    if current_user.organization_id is None:
        raise HTTPException(409, "No organization is associated with this account")
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(403, "Only an organization admin can run setup")

    already = setup_state.get_state(db, current_user.organization_id)["completed"]
    setup_state.mark_complete(db, current_user.organization_id)

    if not already:
        org = resolve_commercial_org(db, current_user)
        db.add(models.AuditLog(
            actor_id=current_user.id,
            target_id=current_user.id,
            organization_id=current_user.organization_id,
            entity="Organization",
            action="OrgSetupCompleted",
            payload={"organization_id": current_user.organization_id},
        ))
        emit_event(
            db, event_type="onboarding.setup_completed",
            source_service="onboarding",
            payload={"operational_org_id": current_user.organization_id},
            organization_id=org.id if org else None,
            user_id=current_user.id,
        )
    db.commit()
    return _setup_payload(db, current_user)


# ──────────────────────────────────────────────────────────────────────────
# Activation flow, Step 2 — organisation details
#
# One call, because the form spans four tables: OrgSettings (name, country,
# zone, slug), User.designation, CommercialOrganization (name, country,
# expected_users) and CommercialWorkspace.timezone. Three separate client calls
# would not be atomic — a failure between them leaves an organisation renamed
# but unclassified.
#
# It UPDATES the organisation signup already created. Step 1 seeds a
# provisional "{First}'s workspace" because the Account step collects no
# company information; this is where that becomes the real name. Nothing here
# creates an organisation, a workspace, a subscription or an entitlement.
# ──────────────────────────────────────────────────────────────────────────

class OrganizationDetailsRequest(BaseModel):
    organization_name: str = Field(..., min_length=1, max_length=255)
    # Job title, not an RBAC role. Persisted to `User.designation`; `User.role`
    # is what every authorization gate in the product reads and is never
    # touched here.
    role: Optional[str] = Field(None, max_length=100)
    worker_band: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=2)
    timezone: Optional[str] = Field(None, max_length=64)
    workspace_slug: Optional[str] = Field(None, max_length=100)


@router.post("/organization")
def save_organization_details(
    payload: OrganizationDetailsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save the Step 2 organisation form and advance the setup wizard.

    IDEMPOTENT BY CONSTRUCTION rather than by replay cache. Every write is an
    UPDATE of a fixed field to a fixed value on a row that already exists, and
    ``setup_state.set_step`` is monotonic — so a double-click, a retry after a
    timeout or a refresh-and-resubmit all converge on the same state. The
    ``Idempotency-Key`` machinery in app.commercial.idempotency exists to stop
    CREATE operations running twice; there is nothing here that a second run
    could duplicate.

    AUTHORIZATION mirrors the rest of the setup wizard: admin or HR, and the
    tenant is derived from the session. No organisation id is accepted from the
    caller, so this can only ever configure the caller's own organisation.
    """
    # Same guard as POST /setup/step and PUT /org/settings.
    if current_user.organization_id is None:
        raise HTTPException(409, "No organization is associated with this account")
    if (current_user.role or "").lower() not in ("admin", "hr"):
        raise HTTPException(403, "Only an organization admin can run setup")

    org_name = payload.organization_name.strip()
    if not org_name:
        raise HTTPException(400, "Organization name cannot be empty")

    expected_users = None
    if payload.worker_band is not None and payload.worker_band.strip():
        band = payload.worker_band.strip()
        if band not in constants.WORKER_COUNT_BAND_VALUES:
            raise HTTPException(400, "Unknown worker count band")
        expected_users = constants.WORKER_COUNT_BAND_VALUES[band]

    designation = (payload.role or "").strip() or None

    # ── Stage the writes this endpoint owns ───────────────────────────────
    # They are flushed here but committed below by update_org_settings, which
    # shares this session — so the whole form lands in ONE transaction and a
    # validation failure inside it rolls back everything staged here too.

    # 1. Job title on the user. Deliberately NOT current_user.role.
    #
    # Re-queried rather than mutated in place: `get_current_user` serves from a
    # 5-minute cache (app.auth.service._USER_CACHE), so `current_user` can be an
    # instance left over from an earlier request's session. Writing to it would
    # be a write to a detached object — and would also poison the cached copy
    # every other request in that window reads.
    if designation is not None:
        row = db.query(User).filter(User.id == current_user.id).first()
        if row is not None:
            row.designation = designation

    # 2. Operational tenant display name, kept in step with OrgSettings.
    op_org = (
        db.query(models.Organization)
          .filter(models.Organization.id == current_user.organization_id)
          .first()
    )
    if op_org is not None:
        op_org.name = org_name

    # 3. Commercial metadata.
    comm = resolve_commercial_org(db, current_user)
    if comm is not None:
        comm.name = org_name
        if payload.country:
            previous_country = comm.country
            comm.country = payload.country.strip().upper()[:2] or None
            if comm.country != previous_country:
                # Country feeds the jurisdiction layer of the entitlement
                # resolver, so a change here can alter the tenant's profile.
                # Reuses the one existing invalidator; the snapshot check in
                # entitlements.get() would also catch this, but dropping the row
                # here keeps the cache honest rather than relying on the
                # integrity check to paper over a stale entry.
                entitlements.invalidate(db, comm.id)
        if expected_users is not None:
            # Segmentation only. CommercialSubscription.seat_count is
            # deliberately untouched — billable seats come from real users.
            comm.expected_users = expected_users

        # 4. Workspace timezone mirror. OrgSettings.default_timezone stays the
        #    authority (see app/_tz.py); this copy is what provisioning reads.
        if payload.timezone:
            ws = db.query(CommercialWorkspace).filter_by(organization_id=comm.id).first()
            if ws is not None:
                ws.timezone = payload.timezone.strip()
    db.flush()

    # ── Delegate the OrgSettings write to the endpoint that owns it ───────
    # Called rather than reimplemented so there remains exactly one definition
    # of a valid slug, country code and IANA zone, one audit-log shape, and one
    # timezone-cache invalidation. It commits this session, which is what makes
    # the staged writes above part of the same transaction.
    from app.org.router import OrgSettingsUpdate, update_org_settings

    try:
        update_org_settings(
            OrgSettingsUpdate(
                company_name=org_name,
                primary_country=(payload.country or None),
                default_timezone=(payload.timezone or None),
                slug=(payload.workspace_slug or None),
            ),
            db=db,
            current_user=current_user,
        )
    except HTTPException:
        db.rollback()
        raise

    # ── Close the wizard ──────────────────────────────────────────────────
    # The same monotonic machine the /setup wizard uses, driven to its terminal
    # state rather than to STEP_TEAMS.
    #
    # This step used to advance to "teams", which left `setup_required` true
    # after the activation flow had already collected everything the wizard
    # asks for. `SetupGate` re-checks that flag on every navigation, so the
    # admin was bounced out of the new flow and into the old three-step wizard
    # at its Teams screen — a surface the four-step activation flow replaced and
    # does not include.
    #
    # There is no teams step to return to: teams are created from Organization
    # Settings, which is exactly what the wizard's own copy tells you. Marking
    # it complete is the honest record — this tenant has been set up — and it is
    # idempotent and terminal, so a retry or a second tab cannot reopen it.
    setup_state.mark_complete(db, current_user.organization_id)

    emit_event(
        db, event_type="onboarding.organization_configured",
        source_service="onboarding",
        payload={"operational_org_id": current_user.organization_id,
                 "worker_band": payload.worker_band},
        organization_id=comm.id if comm is not None else None,
        user_id=current_user.id,
    )
    db.commit()

    return _setup_payload(db, current_user)


_MILESTONE_FIELD_MAP = {
    "first_policy_configured": ("first_policy_configured", "first_policy_configured_at",
                                 "workspace.first_policy_configured"),
    "first_invite_sent":       ("first_invite_sent", "first_invite_sent_at",
                                 "workspace.first_invite_sent"),
    "dashboard_reviewed":      ("dashboard_reviewed", "first_report_viewed_at",
                                 "workspace.first_meaningful_action"),
    "tools_connected":         ("tools_connected", None, "workspace.first_meaningful_action"),
    "billing_reviewed":        ("billing_reviewed", None, "workspace.first_meaningful_action"),
    "first_login":             (None, "first_login_at", "workspace.first_login"),
    "first_work_session":      (None, "first_work_session_at", "workspace.first_meaningful_action"),
}


@router.post("/milestone/{milestone}")
def mark_milestone(milestone: str, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    if milestone not in _MILESTONE_FIELD_MAP:
        raise HTTPException(400, f"Unknown milestone: {milestone}")
    org = _own_org(db, current_user)
    if org is None:
        raise HTTPException(404, "No tenant for this user")

    cl_field, ws_field, event_type = _MILESTONE_FIELD_MAP[milestone]
    now = datetime.now(timezone.utc)

    cl = db.query(OnboardingChecklist).filter_by(organization_id=org.id).one_or_none()
    if cl is None:
        ws_id = (db.query(CommercialWorkspace).filter_by(organization_id=org.id).first() or type("X", (), {"id": None})).id
        cl = OnboardingChecklist(organization_id=org.id, workspace_id=ws_id)
        db.add(cl)
        db.flush()
    if cl_field and not getattr(cl, cl_field, False):
        setattr(cl, cl_field, True)
        ts_attr = cl_field + "_at"
        if hasattr(cl, ts_attr):
            setattr(cl, ts_attr, now)

    ws = db.query(CommercialWorkspace).filter_by(organization_id=org.id).first()
    if ws and ws_field and getattr(ws, ws_field) is None:
        setattr(ws, ws_field, now)

    emit_event(db, event_type=event_type,
               source_service="onboarding",
               payload={"milestone": milestone, "actor": current_user.email},
               organization_id=org.id, workspace_id=ws.id if ws else None,
               user_id=current_user.id)
    db.commit()
    return {"milestone": milestone, "marked_at": now.isoformat()}
