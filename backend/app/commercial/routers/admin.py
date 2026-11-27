"""Admin Operations console (§22).

All actions produce an immutable audit record. Authorization is the
existing `admin`/`owner` role gate from app.auth.service.get_current_user.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user
from app.commercial import (
    ids, state_machines, entitlements, constants, consent, provisioning,
    reconciliation, projection, identity,
)
from app.commercial.audit import emit_event
from app.commercial.models import (
    CommercialOrganization, CommercialWorkspace, CommercialSubscription,
    CommercialSignup, CommercialEvent, FraudSignal, ManualReviewCase,
    CommercialConsent,
)
from app.commercial.schemas import ManualReviewResolveRequest


router = APIRouter(prefix="/api/v1/admin/commercial", tags=["Commercial Admin"])


def _require_admin(user: User):
    """Platform-operations guard. Only super_admin may call commercial-admin
    endpoints — tenant roles (admin, owner) are org-scoped and must not
    enumerate or mutate other tenants' commercial state."""
    if user.role != "super_admin":
        raise HTTPException(403, "Super admin access required")


# ──────────────────────────────────────────────────────────────────────────
# Search
# ──────────────────────────────────────────────────────────────────────────
@router.get("/tenants")
def list_tenants(
    q: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    query = db.query(CommercialOrganization)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(or_(
            func.lower(CommercialOrganization.name).like(like),
            func.lower(CommercialOrganization.domain).like(like),
            func.lower(CommercialOrganization.id).like(like),
        ))
    if state:
        query = query.filter(CommercialOrganization.commercial_state == state)
    total = query.count()
    rows = (query.order_by(CommercialOrganization.created_at.desc())
                .offset(offset).limit(limit).all())
    return {
        "total": total,
        "items": [
            {
                "organization_id": r.id, "name": r.name, "country": r.country,
                "commercial_state": r.commercial_state,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/tenants/{organization_id}")
def tenant_detail(
    organization_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()
    if not org:
        raise HTTPException(404, "Not found")
    sub = db.query(CommercialSubscription).filter_by(organization_id=organization_id).one_or_none()
    workspaces = db.query(CommercialWorkspace).filter_by(organization_id=organization_id).all()
    return {
        "organization": {
            "id": org.id, "name": org.name, "country": org.country,
            "domain": org.domain, "commercial_state": org.commercial_state,
            "expected_users": org.expected_users,
            "owner_user_id": org.owner_user_id,
            "created_at": org.created_at.isoformat() if org.created_at else None,
        },
        "subscription": _serialize_sub(sub) if sub else None,
        "workspaces": [
            {
                "id": w.id, "slug": w.slug, "name": w.name,
                "provisioning_state": w.provisioning_state,
                "first_login_at": w.first_login_at.isoformat() if w.first_login_at else None,
                "first_email_verified_at": w.first_email_verified_at.isoformat() if w.first_email_verified_at else None,
            }
            for w in workspaces
        ],
        "entitlements": entitlements.get(db, organization_id, email_verified=True),
    }


def _serialize_sub(sub: CommercialSubscription) -> dict:
    return {
        "id": sub.id, "plan_code": sub.plan_code, "state": sub.state,
        "billing_cycle": sub.billing_cycle, "seat_count": sub.seat_count,
        "trial_started_at": sub.trial_started_at.isoformat() if sub.trial_started_at else None,
        "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
        "active_since": sub.active_since.isoformat() if sub.active_since else None,
        "provider": sub.provider,
    }


# ──────────────────────────────────────────────────────────────────────────
# Lifecycle controls (suspend / reactivate / close)
# ──────────────────────────────────────────────────────────────────────────
def _transition_subscription(db: Session, org_id: str, target: str, current_user: User,
                             reason: Optional[str] = None) -> dict:
    sub = db.query(CommercialSubscription).filter_by(organization_id=org_id).one_or_none()
    org = db.query(CommercialOrganization).filter_by(id=org_id).one_or_none()
    if not sub or not org:
        raise HTTPException(404, "Tenant not found")
    try:
        new_state = state_machines.subscription.transition(sub.state, target)
    except state_machines.IllegalTransitionError as e:
        raise HTTPException(409, str(e))
    old = sub.state
    sub.state = new_state
    org.commercial_state = new_state
    now = datetime.now(timezone.utc)
    if target == "suspended":
        sub.suspended_at = now
    elif target == "active":
        sub.active_since = sub.active_since or now
        sub.suspended_at = None
    elif target == "cancelled":
        sub.cancelled_at = now
    elif target == "closed":
        sub.closed_at = now
    entitlements.invalidate(db, org_id)
    entitlements.compute(db, org_id, email_verified=True)
    emit_event(db, event_type="subscription.activated" if target == "active" else "subscription.recovered",
               source_service="admin-operations",
               payload={"actor": current_user.email, "reason": reason or "admin action"},
               organization_id=org_id, user_id=current_user.id,
               state_from=old, state_to=new_state)
    db.commit()
    return {"organization_id": org_id, "state": new_state}


@router.post("/tenants/{organization_id}/suspend")
def suspend(organization_id: str, body: dict = None,
            db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return _transition_subscription(db, organization_id, "suspended", current_user,
                                    (body or {}).get("reason"))


@router.post("/tenants/{organization_id}/reactivate")
def reactivate(organization_id: str, db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return _transition_subscription(db, organization_id, "active", current_user)


@router.post("/tenants/{organization_id}/close")
def close(organization_id: str, db: Session = Depends(get_db),
          current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return _transition_subscription(db, organization_id, "closed", current_user)


# ──────────────────────────────────────────────────────────────────────────
# Retry provisioning, recompute entitlements, reconcile
# ──────────────────────────────────────────────────────────────────────────
@router.post("/tenants/{organization_id}/retry-provisioning")
def retry_provisioning(organization_id: str, body: dict = None,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    ws = db.query(CommercialWorkspace).filter_by(organization_id=organization_id).first()
    if not ws:
        raise HTTPException(404, "No workspace to retry")
    result = provisioning.retry_provisioning(db, workspace_id=ws.id)
    db.commit()
    return result


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


#: Persisted commercial-event types the inspector's audit panel surfaces. As of
#: Phase 3.12 the snapshot.created/changed events are durable, so they join the
#: panel; the panel still shows only durable rows, never fabricated history.
_INSPECTOR_EVENT_TYPES = (
    "entitlements.computed", "entitlements.recomputed",
    "entitlements.snapshot.created", "entitlements.changed",
    "subscription.created", "subscription.activated", "subscription.past_due",
    "subscription.reactivated", "subscription.recovered",
    "subscription.seats_changed", "subscription.cancellation_scheduled",
    "subscription.cancelled",
)


def _inspector_subscription(sub) -> Optional[dict]:
    """Read-only subscription facts, including effective dates (§19)."""
    if sub is None:
        return None
    return {
        "plan_code": sub.plan_code,
        "state": sub.state,
        "billing_cycle": sub.billing_cycle,
        "seat_count": sub.seat_count,
        "currency": sub.currency,
        "provider": sub.provider,
        "active_since": _iso(sub.active_since),
        "trial_started_at": _iso(sub.trial_started_at),
        "trial_ends_at": _iso(sub.trial_ends_at),
    }


def _inspector_pending_lifecycle(sub) -> dict:
    """Separate the CURRENT state from any PENDING/scheduled future transition.

    Only surfaces what the commercial layer actually stores. A scheduled
    plan-downgrade with an effective date is not tracked here (billing owns it),
    so it is reported as ``not_tracked`` rather than invented.
    """
    if sub is None:
        return {"current_state": None, "has_pending": False,
                "scheduled_downgrade": "not_tracked_in_commercial_layer"}
    pending_cancellation = bool(sub.cancel_requested_at) and not sub.cancelled_at
    in_recovery = sub.state in {"past_due", "suspended", "cancelled_pending_end"}
    return {
        "current_state": sub.state,
        "has_pending": pending_cancellation or in_recovery,
        "trial_expiry": _iso(sub.trial_ends_at),
        "trial_expired_at": _iso(sub.trial_expired_at),
        "pending_cancellation": pending_cancellation,
        "cancellation_requested_at": _iso(sub.cancel_requested_at),
        "cancelled_at": _iso(sub.cancelled_at),
        "suspended_at": _iso(sub.suspended_at),
        "past_due_since": _iso(sub.past_due_since),
        "closed_at": _iso(sub.closed_at),
        "recovery_available": in_recovery,
        # Billing owns the downgrade effective date; the commercial subscription
        # stores no pending plan change.
        "scheduled_downgrade": "not_tracked_in_commercial_layer",
    }


def _inspector_enterprise_overlay(sub) -> dict:
    """Diagnostic visibility into an active enterprise overlay — never mutation.

    Exposes only which keys the overlay affects, not their values, to avoid
    leaking bespoke contractual limits.
    """
    override = getattr(sub, "enterprise_override", None) if sub is not None else None
    if not isinstance(override, dict) or not override:
        return {"active": False, "keys": [], "source": None}
    return {
        "active": True,
        "keys": sorted(override.keys()),
        "source": "subscription.enterprise_override",
    }


def _inspector_recent_events(db, organization_id: str, limit: int = 10) -> list:
    """Most recent persisted entitlement/subscription events for the tenant."""
    rows = (db.query(CommercialEvent)
              .filter(CommercialEvent.organization_id == organization_id,
                      CommercialEvent.event_type.in_(_INSPECTOR_EVENT_TYPES))
              .order_by(CommercialEvent.occurred_at.desc())
              .limit(limit).all())
    return [
        {
            "event_type": e.event_type,
            "occurred_at": _iso(e.occurred_at),
            "state_from": e.state_from,
            "state_to": e.state_to,
            "source_service": e.source_service,
        }
        for e in rows
    ]


@router.get("/tenants/{organization_id}/entitlement-inspector")
def entitlement_inspector(organization_id: str, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Read-only entitlement diagnostic for one tenant (spec §19).

    Explains what the resolver decided and why, for support/engineering. It is
    an inspector, not a grant panel: there is no override, no mutation and no
    commit here. It reuses the existing resolver and presentation projection, so
    it can never disagree with what a tenant actually resolves.

    The decision trace reflects the resolver's real pipeline and is honest about
    what is not evaluated — workspace configuration (layer 6) is unbuilt, and
    actor RBAC is a separate concern — rather than fabricating those layers.
    """
    _require_admin(current_user)
    org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()
    if not org:
        raise HTTPException(404, "Not found")
    sub = (db.query(CommercialSubscription)
             .filter_by(organization_id=organization_id).one_or_none())

    # Cache status BEFORE resolving, so it reflects what a read would encounter
    # rather than the row this inspection may refresh. Read-only.
    cache = entitlements.cache_status(db, organization_id)

    owner_verified = (
        identity.is_email_verified(db, org.owner_user_id)
        if org.owner_user_id else False
    )
    profile = entitlements.get(db, organization_id, email_verified=owner_verified)

    # The inspector shows the workspace's own decisions, so it projects without a
    # worker lens (an operator inspecting a tenant sees the full admin view).
    projected = projection.build_actor_projection(profile, role="admin")

    return {
        "organization": {
            "id": org.id, "name": org.name, "country": org.country,
            "commercial_state": org.commercial_state,
            "owner_user_id": org.owner_user_id,
        },
        "commercial_state": profile.get("commercial_state"),
        "plan": profile.get("plan"),
        # Kept for backward compatibility with the Phase 3.6 shape.
        "subscription_state": sub.state if sub else None,
        "billing_cycle": sub.billing_cycle if sub else None,
        "subscription": _inspector_subscription(sub),
        "snapshot": {
            "manifest_version": profile.get("manifest_version"),
            "snapshot_hash": profile.get("snapshot_hash"),
            "computed_at": profile.get("computed_at"),
            "expires_at": profile.get("expires_at"),
        },
        "cache": cache,
        "pending_lifecycle": _inspector_pending_lifecycle(sub),
        "enterprise_overlay": _inspector_enterprise_overlay(sub),
        "restrictions": profile.get("restrictions"),
        "capabilities": projected["capabilities"],
        "decision_trace": projection.decision_trace(),
        "recent_events": _inspector_recent_events(db, organization_id),
        "notes": {
            "read_only": True,
            "presentation_is_not_authorization": True,
            "layer6_workspace_configuration": "not_built",
            "last_invalidation": "not_tracked",
            "scheduled_downgrade": "not_tracked_in_commercial_layer",
            "snapshot_events": "persisted_and_logged",
        },
    }


@router.post("/tenants/{organization_id}/recompute-entitlements")
def recompute_entitlements(organization_id: str, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    entitlements.invalidate(db, organization_id)
    profile = entitlements.compute(db, organization_id, email_verified=True)
    emit_event(db, event_type="entitlements.recomputed",
               source_service="admin-operations",
               payload={"actor": current_user.email},
               organization_id=organization_id, user_id=current_user.id)
    db.commit()
    return profile


@router.post("/reconcile/run-now")
def run_reconciliation(current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return reconciliation.run_all_once()


# ──────────────────────────────────────────────────────────────────────────
# Manual review queue
# ──────────────────────────────────────────────────────────────────────────
@router.get("/manual-review")
def manual_review_queue(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    q = db.query(ManualReviewCase)
    if status:
        q = q.filter(ManualReviewCase.status == status)
    total = q.count()
    rows = q.order_by(ManualReviewCase.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "case_id": r.case_id, "signup_id": r.signup_id,
                "organization_id": r.organization_id, "reason": r.reason,
                "severity": r.severity, "fraud_score": r.fraud_score,
                "status": r.status, "outcome": r.outcome,
                "details": r.details,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/manual-review/{case_id}/resolve")
def resolve_manual_review(case_id: str, body: ManualReviewResolveRequest,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    if body.outcome not in constants.MANUAL_REVIEW_OUTCOMES:
        raise HTTPException(400, f"Unknown outcome: {body.outcome}")
    case = db.query(ManualReviewCase).filter_by(case_id=case_id).one_or_none()
    if not case:
        raise HTTPException(404, "Case not found")
    case.outcome = body.outcome
    case.review_notes = body.notes
    case.reviewer_user_id = current_user.id
    case.reviewed_at = datetime.now(timezone.utc)
    case.status = "resolved"
    emit_event(db, event_type="manual_review.resolved",
               source_service="admin-operations",
               payload={"outcome": body.outcome, "actor": current_user.email,
                        "case_id": case_id},
               organization_id=case.organization_id, user_id=current_user.id)
    db.commit()
    return {"case_id": case_id, "outcome": body.outcome, "status": case.status}


# ──────────────────────────────────────────────────────────────────────────
# Fraud queue
# ──────────────────────────────────────────────────────────────────────────
@router.get("/fraud-signals")
def list_fraud_signals(
    severity: Optional[str] = None,
    since_hours: int = 24,
    limit: int = 200,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    q = db.query(FraudSignal)
    if severity:
        q = q.filter(FraudSignal.severity == severity)
    if since_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        q = q.filter(FraudSignal.created_at >= cutoff)
    rows = q.order_by(FraudSignal.created_at.desc()).limit(limit).all()
    return {
        "items": [
            {
                "signal_type": r.signal_type, "severity": r.severity,
                "signup_id": r.signup_id, "organization_id": r.organization_id,
                "score_delta": r.score_delta, "details": r.details,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# Demo / Sandbox tenant creation (I-7: only via admin tooling)
# ──────────────────────────────────────────────────────────────────────────
@router.post("/tenants/demo")
def create_demo_tenant(body: dict, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return _create_admin_tenant(db, current_user, body, target_state="demo")


@router.post("/tenants/sandbox")
def create_sandbox_tenant(body: dict, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return _create_admin_tenant(db, current_user, body, target_state="sandbox")


@router.post("/tenants/enterprise")
def create_enterprise_tenant(body: dict, db: Session = Depends(get_db),
                             current_user: User = Depends(get_current_user)):
    """Production enterprise tenant — invariant I-7."""
    _require_admin(current_user)
    return _create_admin_tenant(db, current_user, body, target_state="active",
                                plan_code="enterprise")


def _create_admin_tenant(db: Session, actor: User, body: dict, *,
                         target_state: str, plan_code: str = "verified"):
    name = (body.get("company_name") or "").strip()
    if not name:
        raise HTTPException(400, "company_name required")
    owner_id = body.get("owner_user_id") or actor.id

    org_id = ids.org_id()
    ws_id = ids.workspace_id()
    sub_id = ids.subscription_id()
    now = datetime.now(timezone.utc)

    db.add(CommercialOrganization(
        id=org_id, name=name,
        country=body.get("country"),
        owner_user_id=owner_id,
        expected_users=body.get("expected_users"),
        commercial_state=target_state,
    ))
    slug = (name.lower().replace(" ", "-") + "-" + ws_id.split("_", 1)[-1][:6])[:120]
    db.add(CommercialWorkspace(
        id=ws_id, organization_id=org_id, slug=slug, name=name,
        timezone=body.get("timezone"), locale=body.get("locale", "en-US"),
        provisioning_state="ready",
    ))
    db.add(CommercialSubscription(
        id=sub_id, organization_id=org_id,
        plan_code=plan_code, billing_cycle="monthly",
        seat_count=body.get("expected_users") or 1,
        state=target_state, provider="manual",
        enterprise_override=body.get("enterprise_override") or {},
        active_since=now if target_state == "active" else None,
    ))
    entitlements.invalidate(db, org_id)
    profile = entitlements.compute(db, org_id, email_verified=True)
    emit_event(db, event_type="tenant.provisioned",
               source_service="admin-operations",
               payload={"target_state": target_state, "plan": plan_code,
                        "actor": actor.email},
               organization_id=org_id, workspace_id=ws_id, user_id=actor.id)
    db.commit()
    return {"organization_id": org_id, "workspace_id": ws_id,
            "subscription_id": sub_id, "state": target_state,
            "entitlements": profile}


# ──────────────────────────────────────────────────────────────────────────
# Consent records, events viewer
# ──────────────────────────────────────────────────────────────────────────
@router.get("/users/{user_id}/consents")
def user_consents(user_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    return consent.list_for_user(db, user_id)


@router.get("/search")
def commercial_search(
    q: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ops-center global search: resolve an id / correlation id / support ref /
    email to its entity and correlation timeline (audit §9)."""
    _require_admin(current_user)
    from app.commercial import search as _search
    return _search.resolve(db, q)


@router.get("/events")
def list_events(
    organization_id: Optional[str] = None,
    event_type: Optional[str] = None,
    error_code: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    q = db.query(CommercialEvent)
    if organization_id:
        q = q.filter(CommercialEvent.organization_id == organization_id)
    if event_type:
        q = q.filter(CommercialEvent.event_type == event_type)
    if error_code:
        q = q.filter(CommercialEvent.error_code == error_code)
    total = q.count()
    rows = q.order_by(CommercialEvent.occurred_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "event_id": r.event_id, "event_type": r.event_type,
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
                "source_service": r.source_service,
                "organization_id": r.organization_id,
                "workspace_id": r.workspace_id, "user_id": r.user_id,
                "state_from": r.state_from, "state_to": r.state_to,
                "error_code": r.error_code,
                "payload": r.payload,
                "correlation_id": r.correlation_id,
            }
            for r in rows
        ],
    }


# ──────────────────────────────────────────────────────────────────────────
# Funnel / metrics summary (§21)
# ──────────────────────────────────────────────────────────────────────────
@router.get("/metrics/funnel")
def funnel_metrics(
    since_hours: int = 24,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    # Ordered funnel stages shown in the dashboard.
    _STAGE_EVENTS = [
        "signup.started", "signup.submitted", "user.created",
        "workspace.created", "tenant.provisioned",
        "email_verification.sent", "email_verification.completed",
        "workspace.first_login", "workspace.first_policy_configured",
        "workspace.first_invite_sent", "workspace.first_meaningful_action",
        "trial.converted",
    ]
    _FAILURE_EVENTS = {
        "signup.validation_failed", "signup.rate_limited", "signup.abuse_flagged",
        "tenant.provisioning_failed", "payment.declined", "email_delivery.failed",
    }

    # Single GROUP BY query replaces 13 individual COUNT queries.
    # Every event type we care about is fetched in one round-trip; missing
    # types simply won't appear in the result set (defaulting to 0 below).
    rows = (
        db.query(
            CommercialEvent.event_type,
            func.count(CommercialEvent.event_type).label("n"),
        )
        .filter(CommercialEvent.occurred_at >= cutoff)
        .group_by(CommercialEvent.event_type)
        .all()
    )
    counts_map = {et: int(n) for et, n in rows}

    stages = [(et, counts_map.get(et, 0)) for et in _STAGE_EVENTS]
    failures = sum(counts_map.get(et, 0) for et in _FAILURE_EVENTS)
    return {
        "since_hours": since_hours,
        "stages": [{"name": n, "count": c} for n, c in stages],
        "failures": failures,
        "alert_thresholds": {
            "provisioning_failure_pct": constants.ALERT_PROVISIONING_FAILURE_PCT,
            "api_error_pct": constants.ALERT_API_ERROR_PCT,
            "email_failure_pct": constants.ALERT_EMAIL_DELIVERY_FAILURE_PCT,
            "payment_failure_pct": constants.ALERT_PAYMENT_FAILURE_SPIKE_PCT,
            "signup_drop_pct": constants.ALERT_SIGNUP_DROP_PCT,
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# §6.2 Critical-failure recovery: payment-succeeded checkout sessions that
# couldn't complete post-payment provisioning. Admin can retry or refund.
# ──────────────────────────────────────────────────────────────────────────
@router.get("/payment-recovery")
def list_payment_recovery_queue(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    from app.commercial.models import CheckoutSession
    q = db.query(CheckoutSession).filter(CheckoutSession.recovery_required == True)  # noqa: E712
    total = q.count()
    rows = q.order_by(CheckoutSession.updated_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "checkout_session_id": r.id,
                "organization_id": r.organization_id,
                "workspace_id": r.workspace_id,
                "plan": r.plan_code, "cycle": r.billing_cycle, "seats": r.seat_count,
                "currency": r.currency, "amount_state": r.state,
                "reason": r.recovery_reason, "attempts": r.recovery_attempts,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


@router.post("/payment-recovery/{checkout_session_id}/retry")
def retry_payment_recovery(
    checkout_session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    from app.commercial.billing import checkout as billing_checkout
    try:
        result = billing_checkout.retry_post_payment_provisioning(
            db, checkout_session_id=checkout_session_id,
        )
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"Retry failed: {e}")


@router.post("/payment-recovery/{checkout_session_id}/refund")
def refund_payment_recovery(
    checkout_session_id: str,
    body: dict = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    reason = (body or {}).get("reason") or "post_payment_provisioning_unrecoverable"
    from app.commercial.billing import checkout as billing_checkout
    try:
        result = billing_checkout.queue_refund_for_failed_provisioning(
            db, checkout_session_id=checkout_session_id, reason=reason,
        )
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"Refund failed: {e}")


# ──────────────────────────────────────────────────────────────────────────
# §22 — Resend verification email + override-verify a user
# ──────────────────────────────────────────────────────────────────────────
@router.post("/users/{user_id}/resend-verification")
def admin_resend_verification(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    target = db.query(User).filter(User.id == user_id).one_or_none()
    if not target:
        raise HTTPException(404, "User not found")
    # Defense-in-depth: even within the super_admin gate, verify the target
    # user belongs to the same organization as the caller when both have an
    # org. This prevents a compromised super_admin account in one org from
    # affecting users in unrelated orgs. A super_admin with no organization_id
    # (pure platform operator) may act on any user.
    if (
        current_user.organization_id is not None
        and target.organization_id != current_user.organization_id
    ):
        raise HTTPException(403, "Target user does not belong to your organization")
    from app.commercial import notifications as _notif, identity as _identity
    # Find the org for this user via CommercialWorkspace, if any.
    ws = (
        db.query(CommercialWorkspace)
          .filter(CommercialWorkspace.owner_user_id == user_id)
          .first()
    )
    org_id = ws.organization_id if ws else None
    first_name = (getattr(target, "name", "") or target.email).split(" ")[0]
    raw_token, _row = _identity.issue_verification_token(
        db, user_id=user_id, organization_id=org_id,
    )
    _notif.send_verification_resend(
        db, to_email=target.email, first_name=first_name,
        raw_verification_token=raw_token,
        user_id=user_id, organization_id=org_id,
    )
    emit_event(
        db, event_type="email_verification.sent",
        source_service="admin-operations",
        payload={"actor": current_user.email, "target_user_id": user_id,
                 "via": "admin_resend"},
        user_id=current_user.id, organization_id=org_id,
    )
    db.commit()
    return {"user_id": user_id, "resent": True}


@router.post("/users/{user_id}/mark-verified")
def admin_mark_verified(
    user_id: int,
    body: dict = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Override email verification (e.g. when a user lost access to the
    email account or the provider repeatedly failed delivery). Recorded
    as an audit event so it shows up in the legal-acceptance trail."""
    _require_admin(current_user)
    target = db.query(User).filter(User.id == user_id).one_or_none()
    if not target:
        raise HTTPException(404, "User not found")
    if (
        current_user.organization_id is not None
        and target.organization_id != current_user.organization_id
    ):
        raise HTTPException(403, "Target user does not belong to your organization")
    setattr(target, "email_verified", True)
    setattr(target, "email_verified_at", datetime.now(timezone.utc))
    reason = (body or {}).get("reason") or "admin_override"
    emit_event(
        db, event_type="email_verification.completed",
        source_service="admin-operations",
        payload={"actor": current_user.email, "target_user_id": user_id,
                 "method": "admin_override", "reason": reason},
        user_id=current_user.id,
    )
    # Recompute entitlements so the email_unverified restriction lifts.
    if hasattr(target, "organization_id"):
        try:
            entitlements.invalidate(db, target.organization_id)
            entitlements.compute(db, target.organization_id, email_verified=True)
        except Exception:
            pass
    db.commit()
    return {"user_id": user_id, "email_verified": True}


# ──────────────────────────────────────────────────────────────────────────
# §23 — Dead-letter webhook queue: monitor + replay
# ──────────────────────────────────────────────────────────────────────────
@router.get("/webhooks/dead-letter")
def list_dead_letter_webhooks(
    provider: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    from app.commercial.models import ProviderWebhookReceipt
    q = db.query(ProviderWebhookReceipt).filter(ProviderWebhookReceipt.status == "dead_letter")
    if provider:
        q = q.filter(ProviderWebhookReceipt.provider == provider)
    rows = q.order_by(ProviderWebhookReceipt.received_at.desc()).limit(limit).all()
    return {
        "total": len(rows),
        "items": [
            {
                "id": r.id, "provider": r.provider,
                "provider_event_id": r.provider_event_id, "event_type": r.event_type,
                "attempts": r.attempts, "last_error": r.last_error,
                "received_at": r.received_at.isoformat() if r.received_at else None,
            }
            for r in rows
        ],
    }


@router.post("/webhooks/dead-letter/{receipt_id}/replay")
def replay_dead_letter_webhook(
    receipt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    from app.commercial.models import ProviderWebhookReceipt
    receipt = db.query(ProviderWebhookReceipt).filter_by(id=receipt_id).one_or_none()
    if not receipt:
        raise HTTPException(404, "Webhook receipt not found")
    # Reset to received so the normal handler picks it up. Idempotency on
    # (provider, provider_event_id) ensures duplicate work is skipped.
    receipt.status = "received"
    receipt.last_error = None
    emit_event(
        db, event_type="webhook.processing_failed",
        source_service="admin-operations",
        payload={"actor": current_user.email, "receipt_id": receipt_id,
                 "action": "replay_queued"},
        user_id=current_user.id,
    )
    db.commit()
    return {"receipt_id": receipt_id, "status": "queued_for_replay"}


# ══════════════════════════════════════════════════════════════════════════
# §27 Phase 5 — A/B feature flags, HRIS import preview, lifecycle scoring
# ══════════════════════════════════════════════════════════════════════════
@router.get("/feature-flags")
def list_feature_flags(current_user: User = Depends(get_current_user)):
    _require_admin(current_user)
    from app.commercial import phase5
    return {"flags": phase5.list_flags()}


@router.post("/feature-flags/{flag}/override")
def set_feature_flag_override(
    flag: str, body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    org_id = (body or {}).get("organization_id")
    value  = (body or {}).get("value")
    if not org_id:
        raise HTTPException(400, "organization_id required")
    from app.commercial import phase5
    phase5.set_override(flag, organization_id=org_id, value=value)
    emit_event(db, event_type="entitlements.recomputed",
               source_service="admin-operations",
               payload={"actor": current_user.email, "flag": flag,
                        "override": value, "via": "feature_flag_override"},
               organization_id=org_id, user_id=current_user.id)
    db.commit()
    return {"flag": flag, "organization_id": org_id, "value": value}


@router.post("/hris-import/preview")
def hris_import_preview_endpoint(
    body: dict,
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    rows = (body or {}).get("rows") or []
    if not isinstance(rows, list):
        raise HTTPException(400, "`rows` must be a list of {name,email,...}")
    from app.commercial import phase5
    return phase5.hris_import_preview(rows)


@router.post("/hris-import/commit")
def hris_import_commit_endpoint(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    org_id  = (body or {}).get("organization_id")
    preview = (body or {}).get("preview") or {}
    if not org_id:
        raise HTTPException(400, "organization_id required")
    from app.commercial import phase5
    result = phase5.hris_import_commit(
        db, organization_id=org_id, preview=preview,
        actor_user_id=current_user.id,
    )
    db.commit()
    return result


@router.get("/health-score/{organization_id}")
def health_score_for_tenant(
    organization_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    from app.commercial import phase5
    return phase5.compute_health_score(db, organization_id)


@router.get("/health-score")
def health_scores_all(
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    from app.commercial import phase5
    return {"items": phase5.compute_health_for_all(db, limit=limit)}
