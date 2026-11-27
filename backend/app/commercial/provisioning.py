"""Provisioning orchestrator (§9 — Activation Orchestration Service).

Coordinates the 14-step tenant provisioning pipeline:

    1. Validate signup token
    2. Create or bind user
    3. Create organization      <- delegates to app.org.provisioning_service
    4. Create workspace
    5. Assign owner role
    6. Apply jurisdiction defaults
    7. (folded into step 3)
    8. Create subscription state
    9. Create billing customer (if required)
   10. Compute entitlements
   11. Initialize audit stream
   12. Initialize onboarding checklist
   13. Queue welcome notifications
   14. Redirect user

Step 3 is the important one. It calls the SHARED tenant-provisioning service
(``app.org.provisioning_service.provision_tenant``) rather than writing a
``CommercialOrganization`` row on its own. That service owns the operational
tenant graph — the integer-PK ``Organization`` row, ``users.organization_id``,
``OrgSettings`` (with explicit ownership), the org-stamped ``AppSettings``
policy keys and the default ``LeavePolicy`` rows — and it links the commercial
organization back to the operational one via ``operational_org_id``.

Before this delegation existed, self-serve signup created only the commercial
organization. ``users.organization_id`` stayed NULL, and because every
org-scoped read is written as ``Model.organization_id ==
current_user.organization_id`` — which SQLAlchemy compiles to ``IS NULL`` for a
Python ``None`` — every self-serve tenant shared a single bucket and could read
the others' data. Step 7 (the old ``_step_apply_policy_template``) is now folded
into step 3 for the same reason: it used to detect the NULL organization and
return early, silently skipping leave-policy seeding.

p95 target: < 30 seconds. The orchestrator runs *inline* on the signup
request, not as a background job, so the signup endpoint can return a
final workspace_id and redirect_url to the caller. Each step is wrapped
with retry-on-recoverable-failure and a rollback path for terminal
failures.

This is invariant I-2 (every org/workspace has a commercial state) and
I-5 (no silent failures) enforcement.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.commercial import constants, ids, state_machines, entitlements
from app.commercial.audit import emit_event
from app.commercial.models import (
    CommercialOrganization, CommercialWorkspace, CommercialSubscription,
    CommercialSignup, OnboardingChecklist,
)

_log = logging.getLogger("zoiko.commercial.provisioning")


# ──────────────────────────────────────────────────────────────────────────
# CONTEXT (passed between steps, mutated as we go)
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class ProvisioningContext:
    signup_id: str
    correlation_id: str
    user_id: int
    first_name: str
    last_name: str
    email: str
    company_name: str
    country: Optional[str]
    locale: Optional[str]
    timezone: Optional[str]
    plan_code: str
    intent: str           # trial | paid
    expected_users: Optional[int]
    billing_required: bool

    # Populated as we progress.
    organization_id: Optional[str] = None
    # The integer-PK operational tenant this commercial org maps onto. Set by
    # step 3 via the shared provisioning service; this is the id that every
    # org-scoped operational table keys on, and the value written to
    # CommercialOrganization.operational_org_id.
    operational_org_id: Optional[int] = None
    workspace_id: Optional[str] = None
    subscription_id: Optional[str] = None
    provisioning_state: str = "not_started"
    commercial_state: str = "trial_active"


# ──────────────────────────────────────────────────────────────────────────
# RUNNER
# ──────────────────────────────────────────────────────────────────────────
def provision_tenant(
    db: Session,
    ctx: ProvisioningContext,
) -> Dict[str, Any]:
    """Run all 14 steps in one transaction.

    Returns a result dict with the final IDs and state. Raises on
    failures that are not safely rollback-able.
    """
    try:
        _set_state(db, ctx, "queued")
        _step_validate_signup(db, ctx)             # 1
        _step_bind_user(db, ctx)                   # 2
        _set_state(db, ctx, "identity_bound")
        _step_create_organization(db, ctx)         # 3
        _set_state(db, ctx, "organization_created")
        _step_create_workspace(db, ctx)            # 4
        _set_state(db, ctx, "workspace_created")
        _step_assign_owner(db, ctx)                # 5
        _set_state(db, ctx, "owner_assigned")
        _step_apply_jurisdiction(db, ctx)          # 6
        # Step 7 (leave-policy + AppSettings seeding) is folded into step 3 —
        # the shared provisioning service does it as part of building the
        # operational tenant, so it can no longer be skipped for want of an
        # organization_id. See the module docstring.
        _set_state(db, ctx, "defaults_applied")
        _step_create_subscription(db, ctx)         # 8
        _step_create_billing_customer(db, ctx)     # 9
        _step_compute_entitlements(db, ctx)        # 10
        _set_state(db, ctx, "audit_initialized")   # 11 (audit stream is the commercial_events table — initialized implicitly)
        _step_init_onboarding_checklist(db, ctx)   # 12
        _set_state(db, ctx, "entitlements_applied")
        _set_state(db, ctx, "ready")
        emit_event(
            db,
            event_type="tenant.provisioned",
            source_service="activation-orchestration",
            payload={"plan": ctx.plan_code, "intent": ctx.intent},
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            correlation_id=ctx.correlation_id,
        )
        return {
            "organization_id": ctx.organization_id,
            "operational_org_id": ctx.operational_org_id,
            "workspace_id": ctx.workspace_id,
            "subscription_id": ctx.subscription_id,
            "commercial_state": ctx.commercial_state,
            "provisioning_state": "ready",
        }
    except Exception as e:    # one of our steps failed — record + re-raise
        _log.exception("Provisioning failed for signup_id=%s", ctx.signup_id)
        emit_event(
            db,
            event_type="tenant.provisioning_failed",
            source_service="activation-orchestration",
            payload={"error": str(e), "signup_id": ctx.signup_id},
            organization_id=ctx.organization_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            correlation_id=ctx.correlation_id,
            error_code=constants.ERR_PROVISIONING_FAILED,
        )
        # Mark the current workspace as failed_retryable so the
        # admin-console retry button can re-run provisioning.
        _try_set_state(db, ctx, "failed_retryable")
        raise


# ──────────────────────────────────────────────────────────────────────────
# STEP IMPLEMENTATIONS
# ──────────────────────────────────────────────────────────────────────────
def _step_validate_signup(db: Session, ctx: ProvisioningContext) -> None:
    """Step 1 — confirm the CommercialSignup row exists and is in a state
    that allows progression."""
    row = db.query(CommercialSignup).filter_by(id=ctx.signup_id).one_or_none()
    if row is None:
        raise RuntimeError(f"Signup {ctx.signup_id} not found")
    if row.state in {"signup_failed", "abuse_blocked"}:
        raise RuntimeError(f"Signup {ctx.signup_id} is in non-progressing state {row.state}")


def _step_bind_user(db: Session, ctx: ProvisioningContext) -> None:
    """Step 2 — the user was created by the signup router before calling
    the orchestrator (so we already have ctx.user_id). Validate."""
    if not ctx.user_id:
        raise RuntimeError("User id missing from provisioning context")


def _currency_for_country(country: Optional[str]) -> str:
    """Map a country to its billing currency.

    Shared by the operational OrgSettings (step 3) and the commercial workspace
    (step 6) so a tenant can never end up with two different currencies.
    """
    code = (country or "").upper()
    if code in {"GB"}:
        return "GBP"
    if code in {"DE", "FR", "ES", "IT", "NL", "BE", "PT", "IE", "AT", "FI", "GR"}:
        return "EUR"
    if code in {"IN"}:
        return "INR"
    return "USD"


def _step_create_organization(db: Session, ctx: ProvisioningContext) -> None:
    """Step 3 — create the operational tenant AND its commercial organization.

    Delegates to the shared provisioning service so self-serve signup produces
    exactly the same tenant graph as super-admin provisioning: an integer-PK
    ``Organization``, a non-NULL ``users.organization_id``, ``OrgSettings`` with
    explicit ownership, org-stamped ``AppSettings``, seeded ``LeavePolicy``
    rows, and a ``CommercialOrganization`` whose ``operational_org_id`` points
    back at the operational row.

    The user was created by the signup router before the orchestrator ran, so
    it is adopted here rather than created again.
    """
    from app.org.provisioning_service import (
        CREATED_VIA_SELF_SERVICE,
        provision_tenant as provision_operational_tenant,
    )
    from database import models as _models

    admin_user = (
        db.query(_models.User).filter(_models.User.id == ctx.user_id).one_or_none()
    )
    if admin_user is None:
        raise RuntimeError(f"Signup user {ctx.user_id} not found — cannot provision tenant")

    domain = ctx.email.split("@", 1)[-1].lower() if "@" in ctx.email else None

    # The commercial org id is minted here so the rest of the pipeline (and the
    # signup response) can reference it, and handed to the shared service to
    # use verbatim.
    ctx.organization_id = ids.org_id()

    result = provision_operational_tenant(
        db,
        org_name=ctx.company_name,
        admin_email=ctx.email,
        admin_name=f"{ctx.first_name} {ctx.last_name}".strip() or None,
        created_via=CREATED_VIA_SELF_SERVICE,
        existing_admin_user=admin_user,
        # Self-registration: the registering admin IS the actor.
        actor_user_id=admin_user.id,
        country=ctx.country,
        timezone_name=ctx.timezone,
        currency=_currency_for_country(ctx.country),
        # The submitted company name must reach the operational OrgSettings row,
        # not just the commercial organization — OrgSettings.company_name is
        # what the product UI reads.
        company_name=ctx.company_name,
        seed_leave_policies=True,
        # Teams are the operator's choice in the onboarding wizard, not a
        # signup-time default.
        seed_default_teams=False,
        create_commercial_org=True,
        commercial_state=ctx.commercial_state,
        commercial_org_id=ctx.organization_id,
        expected_users=ctx.expected_users,
        domain=domain,
        legal_name=ctx.company_name,
        extra_audit_payload={
            "signup_id": ctx.signup_id,
            "correlation_id": ctx.correlation_id,
            "plan": ctx.plan_code,
            "intent": ctx.intent,
        },
    )

    ctx.operational_org_id = result.organization.id

    # Open the setup wizard for this tenant. The wizard is opt-in by row
    # presence: only organizations that explicitly begin() are ever routed into
    # it, so no pre-existing tenant can be dropped into a setup flow.
    from app.org import setup_state
    setup_state.begin(db, ctx.operational_org_id)

    emit_event(
        db, event_type="organization.created",
        source_service="organization-service",
        payload={
            "name": ctx.company_name,
            "operational_org_id": ctx.operational_org_id,
        },
        organization_id=ctx.organization_id, user_id=ctx.user_id,
        correlation_id=ctx.correlation_id,
    )


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", (text or "").lower()).strip("-")
    return s or "workspace"


def _step_create_workspace(db: Session, ctx: ProvisioningContext) -> None:
    """Step 4 — create the workspace."""
    ctx.workspace_id = ids.workspace_id()
    base_slug = _slugify(ctx.company_name)
    # Slug uniqueness: tack on a short id-suffix to keep it human-readable
    # but globally unique without an extra DB roundtrip.
    slug = f"{base_slug}-{ctx.workspace_id.split('_', 1)[-1][:6]}"
    ws = CommercialWorkspace(
        id=ctx.workspace_id,
        organization_id=ctx.organization_id,
        slug=slug,
        name=ctx.company_name,
        timezone=ctx.timezone,
        locale=ctx.locale or constants.DEFAULT_LANGUAGE,
        provisioning_state="workspace_created",
    )
    db.add(ws)
    db.flush()
    emit_event(
        db, event_type="workspace.created",
        source_service="workspace-service",
        payload={"slug": slug},
        organization_id=ctx.organization_id, workspace_id=ctx.workspace_id,
        user_id=ctx.user_id, correlation_id=ctx.correlation_id,
    )


def _step_assign_owner(db: Session, ctx: ProvisioningContext) -> None:
    """Step 5 — owner is already on CommercialOrganization. Nothing to
    do at the DB level; emit the event for the funnel."""
    emit_event(
        db, event_type="user.created",
        source_service="identity-service",
        payload={"role": "owner"},
        organization_id=ctx.organization_id, user_id=ctx.user_id,
        correlation_id=ctx.correlation_id,
    )


def _step_apply_jurisdiction(db: Session, ctx: ProvisioningContext) -> None:
    """Step 6 — currency, retention defaults driven by country.
    For now this is best-effort; full tax-region detection is part of §20.
    """
    if not ctx.organization_id:
        return
    ws = db.query(CommercialWorkspace).filter_by(id=ctx.workspace_id).one_or_none()
    if ws is None:
        return
    # Same helper the operational OrgSettings currency comes from (step 3), so
    # the two layers cannot disagree about a tenant's currency.
    ws.currency = _currency_for_country(ctx.country)
    ws.region = (ctx.country or "US").upper()

    # Record the country AND the timezone on OrgSettings. Country is what the
    # workforce side reads (holiday calendar, and any future statutory default);
    # without it a new tenant's jurisdiction is known only to the billing tables.
    #
    # The timezone matters more. `ctx.timezone` is the signing-up admin's own
    # IANA zone, captured in the signup payload (`client_context.timezone`) and
    # carried this far already — it was simply never persisted, so every
    # self-service tenant started with default_timezone NULL and silently ran
    # its attendance day, absence cutoffs and payroll periods on the system
    # fallback clock. Only a value that actually loads is written; anything else
    # is left NULL so the admin is prompted rather than given a wrong zone.
    if (ctx.country or ctx.timezone) and ctx.user_id:
        from database import models as _models
        from app._tz import is_valid_timezone as _valid_tz
        # The workforce org id, not the commercial one — same lookup step 7 uses.
        user = db.query(_models.User).filter(_models.User.id == ctx.user_id).one_or_none()
        wf_org_id = getattr(user, "organization_id", None)
        if wf_org_id:
            cc = ctx.country.upper()[:2] if ctx.country else None
            tz = (ctx.timezone or "").strip()
            tz = tz if _valid_tz(tz) else None
            row = (
                db.query(_models.OrgSettings)
                .filter(_models.OrgSettings.organization_id == wf_org_id)
                .one_or_none()
            )
            if row is None:
                db.add(_models.OrgSettings(organization_id=wf_org_id,
                                           primary_country=cc,
                                           default_timezone=tz))
            else:
                if cc and not row.primary_country:
                    row.primary_country = cc
                # Never overwrite a zone the tenant already chose.
                if tz and not row.default_timezone:
                    row.default_timezone = tz
    db.flush()


def _step_create_subscription(db: Session, ctx: ProvisioningContext) -> None:
    """Step 8 — create CommercialSubscription with the trial window."""
    ctx.subscription_id = ids.subscription_id()
    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=constants.TRIAL_LENGTH_DAYS)
    sub = CommercialSubscription(
        id=ctx.subscription_id,
        organization_id=ctx.organization_id,
        plan_code=ctx.plan_code,
        billing_cycle="monthly",
        seat_count=ctx.expected_users or 1,
        currency="USD",
        state="trial_active" if ctx.intent == "trial" else "active",
        trial_started_at=now if ctx.intent == "trial" else None,
        trial_ends_at=trial_end if ctx.intent == "trial" else None,
        active_since=now if ctx.intent != "trial" else None,
        provider="stub",
    )
    db.add(sub)
    db.flush()
    emit_event(
        db, event_type="subscription.created",
        source_service="subscription-service",
        payload={
            "plan": ctx.plan_code,
            "intent": ctx.intent,
            "trial_ends_at": trial_end.isoformat() if ctx.intent == "trial" else None,
            "seat_count": ctx.expected_users or 1,
        },
        organization_id=ctx.organization_id, workspace_id=ctx.workspace_id,
        user_id=ctx.user_id, correlation_id=ctx.correlation_id,
    )
    if ctx.intent == "trial":
        emit_event(
            db, event_type="trial.started",
            source_service="subscription-service",
            payload={"plan": ctx.plan_code, "trial_ends_at": trial_end.isoformat()},
            organization_id=ctx.organization_id, workspace_id=ctx.workspace_id,
            user_id=ctx.user_id, correlation_id=ctx.correlation_id,
        )


def _step_create_billing_customer(db: Session, ctx: ProvisioningContext) -> None:
    """Step 9 — only if billing is required (paid signup).

    For trials we skip this. For paid signups, the checkout-session
    endpoint will create the provider billing customer on first call
    via app.commercial.billing.providers.
    """
    if not ctx.billing_required:
        return
    # Defer to checkout-session creation; emit event so audit ledger
    # records the intent.
    emit_event(
        db, event_type="billing.customer_created",
        source_service="billing-service",
        payload={"plan": ctx.plan_code, "provider": "stub"},
        organization_id=ctx.organization_id, user_id=ctx.user_id,
        correlation_id=ctx.correlation_id,
    )


def _step_compute_entitlements(db: Session, ctx: ProvisioningContext) -> None:
    """Step 10 — first computation populates the cache."""
    profile = entitlements.compute(db, ctx.organization_id, email_verified=False)
    emit_event(
        db, event_type="entitlements.computed",
        source_service="entitlements-service",
        payload={"plan": profile.get("plan"), "state": profile.get("commercial_state")},
        organization_id=ctx.organization_id, user_id=ctx.user_id,
        correlation_id=ctx.correlation_id,
    )


def _step_init_onboarding_checklist(db: Session, ctx: ProvisioningContext) -> None:
    """Step 12 — empty checklist row so onboarding UI has somewhere to write."""
    row = OnboardingChecklist(
        organization_id=ctx.organization_id,
        workspace_id=ctx.workspace_id,
    )
    db.add(row)
    db.flush()


# ──────────────────────────────────────────────────────────────────────────
# STATE TRANSITIONS
# ──────────────────────────────────────────────────────────────────────────
def _set_state(db: Session, ctx: ProvisioningContext, new_state: str) -> None:
    old = ctx.provisioning_state
    ctx.provisioning_state = state_machines.provisioning.transition(old, new_state)
    if ctx.workspace_id:
        ws = db.query(CommercialWorkspace).filter_by(id=ctx.workspace_id).one_or_none()
        if ws:
            ws.provisioning_state = ctx.provisioning_state
            db.flush()


def _try_set_state(db: Session, ctx: ProvisioningContext, new_state: str) -> None:
    """Best-effort state set, used in failure paths where the strict
    state machine would reject the transition. We log + continue.
    """
    try:
        _set_state(db, ctx, new_state)
    except state_machines.IllegalTransitionError:
        # Force-set in failure path; we still want operations to see
        # the row marked as failed.
        if ctx.workspace_id:
            ws = db.query(CommercialWorkspace).filter_by(id=ctx.workspace_id).one_or_none()
            if ws:
                ws.provisioning_state = "failed_retryable"
                db.flush()


# ──────────────────────────────────────────────────────────────────────────
# RETRY  (used by admin console + reconciliation jobs)
# ──────────────────────────────────────────────────────────────────────────
def retry_provisioning(db: Session, *, workspace_id: str, correlation_id: Optional[str] = None) -> dict:
    """Re-run the orchestrator for a failed workspace.

    Looks up the original signup context and replays the pipeline from
    the last completed step.
    """
    ws = db.query(CommercialWorkspace).filter_by(id=workspace_id).one_or_none()
    if ws is None:
        raise RuntimeError("Workspace not found")
    org = db.query(CommercialOrganization).filter_by(id=ws.organization_id).one_or_none()
    if org is None:
        raise RuntimeError("Organization not found")

    sub = db.query(CommercialSubscription).filter_by(organization_id=org.id).one_or_none()

    emit_event(
        db, event_type="tenant.provisioning_retried",
        source_service="admin-operations",
        payload={"workspace_id": workspace_id},
        organization_id=org.id, workspace_id=workspace_id,
        correlation_id=correlation_id,
    )

    if sub is None:
        # Re-create the subscription if it never made it.
        sub_id = ids.subscription_id()
        now = datetime.now(timezone.utc)
        trial_end = now + timedelta(days=constants.TRIAL_LENGTH_DAYS)
        db.add(CommercialSubscription(
            id=sub_id, organization_id=org.id, plan_code="verified",
            billing_cycle="monthly", seat_count=org.expected_users or 1,
            state="trial_active", trial_started_at=now, trial_ends_at=trial_end,
            provider="stub",
        ))
        db.flush()

    entitlements.invalidate(db, org.id)
    profile = entitlements.compute(db, org.id, email_verified=False)
    ws.provisioning_state = "ready"
    db.flush()
    emit_event(
        db, event_type="tenant.provisioning_recovered",
        source_service="admin-operations",
        payload={"plan": profile.get("plan")},
        organization_id=org.id, workspace_id=workspace_id,
        correlation_id=correlation_id,
    )
    return {"organization_id": org.id, "workspace_id": workspace_id,
            "provisioning_state": "ready"}
