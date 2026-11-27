"""Activation status read-model (Phase 4 §1).

A pure read over state that already exists — the workspace ``provisioning_state``
(driven by the existing provisioning state machine), subscription/commercial
state, entitlement cache, and email-verification state. The frontend polls this
so it never has to guess provisioning progress.

Reused, not rebuilt: provisioning runs inline in the signup request today, so a
poll almost always sees a terminal state (``ready`` on success, ``failed_*`` on
error). This read-model is agnostic to whether provisioning is sync or async —
it just reports the current persisted state, so it keeps working unchanged if
provisioning later moves to a background worker.

No PII is exposed here (no email/name), so a caller proving knowledge of an
unguessable id (signup_id / workspace_id / organization_id) is the capability.
"""
from __future__ import annotations

from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.commercial import ids, identity
from app.commercial.models import (
    CommercialSignup, CommercialWorkspace, CommercialOrganization,
    CommercialSubscription, EntitlementsCache,
)
from app.commercial.routers._helpers import app_base_url

# Canonical success path: (raw provisioning_state, friendly label). Order is the
# progress order; mirrors constants.PROVISIONING_STATES minus the failure tail.
_STEPS = [
    ("queued", "Pending"),
    ("identity_bound", "Creating user"),
    ("organization_created", "Creating organization"),
    ("workspace_created", "Creating workspace"),
    ("owner_assigned", "Assigning owner"),
    ("defaults_applied", "Applying defaults"),
    ("audit_initialized", "Initializing audit"),
    ("entitlements_applied", "Computing entitlements"),
    ("ready", "Workspace ready"),
]
_STATE_INDEX = {state: i for i, (state, _) in enumerate(_STEPS)}
_LABELS = [label for _, label in _STEPS]
_TOTAL = len(_STEPS)

# Coarse per-remaining-step estimate. Provisioning is sub-second today; this is
# a UI hint, not an SLA. Marked coarse deliberately.
_SECONDS_PER_STEP = 3

_FAILURE_STATES = {"failed_retryable", "failed_terminal", "rolled_back"}


def _support_reference(correlation_id: Optional[str]) -> Optional[str]:
    """Human-quotable reference for the provisioning operation.

    Derived from the signup's ``correlation_id``, which is minted once per
    signup and persisted — so it is stable across refreshes, retries and
    sessions, and it joins straight back to every ``commercial_events`` row for
    that activation. Grouped as ZT-XXXX-XXXX because that is how people read a
    reference back over the phone.
    """
    if not correlation_id:
        return None
    suffix = correlation_id.split("_", 1)[-1][:8].upper()
    if len(suffix) == 8:
        return f"ZT-{suffix[:4]}-{suffix[4:]}"
    return f"ZT-{suffix}"


# ──────────────────────────────────────────────────────────────────────────
# UI milestones (activation flow, Step 3 — Workspace)
#
# Six lines the visitor reads, mapped onto the nine real provisioning states.
# Nothing here invents a state: each milestone names the state at which the
# work it describes is genuinely finished, and the thresholds ascend with the
# displayed order so a later line can never complete before an earlier one.
#
# One deliberate conservatism. The pipeline runs `defaults_applied` and
# `audit_initialized` BEFORE `entitlements_applied`, i.e. security/audit setup
# really finishes before entitlements do. Displaying it in that true order
# would put the design's two middle rows the other way round, so the audit row
# is instead held until `ready` — it is marked complete later than it actually
# was, never earlier. Erring towards "not yet" is the safe direction for a
# progress display.
# ──────────────────────────────────────────────────────────────────────────
_MILESTONES = [
    # (key, title, description, provisioning state at which it is complete)
    ("account_verified", "Account verified", "Your email is confirmed.", None),
    ("organisation_created", "Organisation created", "Workspace record set up.",
     "organization_created"),
    ("workspace_provisioning", "Workspace provisioning", "Building your secure workspace.",
     "owner_assigned"),
    ("trial_entitlements", "Trial entitlements applying", "Enabling your plan features.",
     "entitlements_applied"),
    ("security_audit", "Security and audit controls", "Applying policy and audit setup.",
     "ready"),
    ("ready", "Ready", "Your workspace is ready.", "ready"),
]


class ActivationNotFound(Exception):
    pass


def _build_milestones(*, state_index: int, email_verified: bool,
                      failed: bool, ready: bool) -> list:
    """Resolve each milestone against the real provisioning state.

    `account_verified` is the one milestone that is not a provisioning state —
    it is the email-verification fact Step 2 established, read from the same
    `identity.is_email_verified` every other caller uses.

    Only ONE milestone is ever marked `active`, and only while provisioning is
    genuinely still running. That guard matters for the case where the
    workspace is `ready` but the email is not verified: without it the first
    row would spin forever underneath five completed ones, implying work that
    nothing is doing. Such a row is `pending` — not done, not in progress.
    """
    out = []
    first_pending_marked = False
    running = not failed and not ready

    for key, title, description, threshold in _MILESTONES:
        if threshold is None:
            done = email_verified
        else:
            done = state_index >= _STATE_INDEX[threshold]

        status = "complete" if done else "pending"
        if not done and not first_pending_marked:
            if failed:
                status = "failed"
                first_pending_marked = True
            elif running:
                status = "active"
                first_pending_marked = True

        out.append({"key": key, "title": title,
                    "description": description, "status": status})
    return out


def _trial_facts(sub) -> dict:
    """The customer-visible trial window.

    Length comes from ``app.billing.trial`` — the module whose docstring names
    itself the single source of truth for trial length, whose ladder sends the
    expiry mail, and whose 14 days Step 1's plan summary already quoted. The
    start is the commercial subscription's recorded ``trial_started_at``, i.e.
    the real signup moment.

    NOTE: ``commercial_subscriptions.trial_ends_at`` is stored as start + 30
    (app.commercial.constants.TRIAL_LENGTH_DAYS) and therefore disagrees with
    what is returned here. That is a known, pre-existing discrepancy and is
    deliberately NOT repaired by this read — repairing it changes live tenants'
    trial windows and needs its own migration and rollout. This function
    introduces no new clock: it reports the one the customer already
    experiences.
    """
    from datetime import timedelta
    from app.billing import trial as billing_trial

    length_days = billing_trial.TRIAL_LENGTH_DAYS
    started = getattr(sub, "trial_started_at", None)
    ends_at = None
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        ends_at = (started + timedelta(days=length_days)).isoformat()
    return {
        "length_days": length_days,
        "started_at": started.isoformat() if started is not None else None,
        "ends_at": ends_at,
        "plan_code": getattr(sub, "plan_code", None),
        "state": getattr(sub, "state", None),
    }


def compute(
    db: Session,
    *,
    signup_id: Optional[str] = None,
    organization_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Build the activation-status DTO from persisted state. Raises
    ActivationNotFound if no matching signup/workspace/org exists."""
    signup: Optional[CommercialSignup] = None
    if signup_id:
        signup = db.query(CommercialSignup).filter_by(id=signup_id).one_or_none()
        if signup is None:
            raise ActivationNotFound()
        organization_id = organization_id or signup.organization_id
        workspace_id = workspace_id or signup.workspace_id

    # Looked up the other way round when the caller identified the tenant
    # rather than the signup. The signup row is the provisioning operation, and
    # its correlation_id is what the support reference is built from — without
    # this the session-scoped caller (Step 3, which has no signup id) would
    # render no reference at all.
    if signup is None and (organization_id or workspace_id):
        q = db.query(CommercialSignup)
        if organization_id:
            q = q.filter(CommercialSignup.organization_id == organization_id)
        else:
            q = q.filter(CommercialSignup.workspace_id == workspace_id)
        signup = q.order_by(CommercialSignup.created_at.desc()).first()

    workspace: Optional[CommercialWorkspace] = None
    if workspace_id:
        workspace = db.query(CommercialWorkspace).filter_by(id=workspace_id).one_or_none()
    if workspace is None and organization_id:
        workspace = (db.query(CommercialWorkspace)
                     .filter_by(organization_id=organization_id).first())

    if signup is None and workspace is None and organization_id is None:
        raise ActivationNotFound()

    org = None
    if organization_id:
        org = db.query(CommercialOrganization).filter_by(id=organization_id).one_or_none()

    # ── provisioning progress ────────────────────────────────────────────────
    prov_state = workspace.provisioning_state if workspace else None
    failure_code = getattr(signup, "failure_code", None)

    failed = (prov_state in _FAILURE_STATES) or (
        signup is not None and signup.state == "signup_failed"
    )
    # recovery: a payment succeeded but provisioning failed leaves recovery flags;
    # a retryable provisioning state is also recoverable.
    retry_available = prov_state == "failed_retryable" or (
        failed and prov_state != "failed_terminal"
    )

    if prov_state == "ready":
        idx = _STATE_INDEX["ready"]
    elif prov_state in _STATE_INDEX:
        idx = _STATE_INDEX[prov_state]
    else:
        idx = 0  # not_started / unknown → Pending

    ready = prov_state == "ready"
    if failed:
        status = "failed"
        current_step = None
        failed_step = _LABELS[idx] if not ready else None
    elif ready:
        status = "ready"
        current_step = "Workspace ready"
        failed_step = None
    elif idx > 0:
        status = "in_progress"
        current_step = _LABELS[idx]
        failed_step = None
    else:
        status = "pending"
        current_step = _LABELS[0]
        failed_step = None

    completed_steps = _LABELS[:idx] if not ready else list(_LABELS)
    percent = 100 if ready else int(round(idx / (_TOTAL - 1) * 100))
    steps_remaining = 0 if (ready or failed) else (_TOTAL - 1 - idx)
    eta = None if (ready or failed) else steps_remaining * _SECONDS_PER_STEP

    # ── related resource state ───────────────────────────────────────────────
    sub = None
    if organization_id:
        sub = db.query(CommercialSubscription).filter_by(organization_id=organization_id).one_or_none()
    ent = None
    if organization_id:
        ent = db.query(EntitlementsCache).filter_by(organization_id=organization_id).one_or_none()

    # Whose inbox to check. The signup row carries it on the id-keyed lookup;
    # the session-scoped caller passes it directly, because a tenant found by
    # organization alone has no signup row to read it from — and without this
    # the Ready screen told a verified admin their address was unconfirmed.
    verify_user_id = user_id
    if verify_user_id is None and signup is not None:
        verify_user_id = signup.user_id
    email_verified = (
        identity.is_email_verified(db, verify_user_id) if verify_user_id else False
    )

    correlation_id = getattr(signup, "correlation_id", None)
    workspace_url = f"{app_base_url()}/dashboard" if ready else None

    return {
        "signup_id": getattr(signup, "id", None),
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "status": status,
        "workspace_ready": ready,
        "provisioning": {
            "state": prov_state,
            "current_step": current_step,
            "completed_steps": completed_steps,
            "total_steps": _TOTAL,
            "percent_complete": percent,
            "failed_step": failed_step,
            "failure_code": failure_code if failed else None,
            "retry_available": bool(retry_available),
            "estimated_seconds_remaining": eta,
        },
        # The six lines the Verify→Workspace screen renders, resolved against
        # the real state above rather than against a client-side timer.
        "milestones": _build_milestones(
            state_index=idx, email_verified=email_verified,
            failed=failed, ready=ready,
        ),
        "organization_status": getattr(org, "commercial_state", None),
        "subscription_status": getattr(sub, "state", None),
        "entitlements_ready": ent is not None,
        "email_verified": email_verified,
        "trial": _trial_facts(sub),
        "workspace_url": workspace_url,
        "workspace_name": getattr(workspace, "name", None),
        "correlation_id": correlation_id,
        "trace_id": ids.trace_id(),
        "support_reference": _support_reference(correlation_id),
    }
