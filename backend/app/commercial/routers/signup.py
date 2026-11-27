"""POST /api/v1/signup (§14.1) — end-to-end commercial activation entry point.

This single endpoint runs the full Journey A / Journey B pipeline:

  validation -> bot challenge -> fraud check -> commercial routing
  -> identity creation -> provisioning orchestrator (operational tenant +
  commercial tenant) -> entitlements -> consent log
  -> welcome + verification email -> session -> redirect URL

Every transition writes a CommercialSignup row update + commercial event.

TRANSACTION SHAPE
-----------------
Everything that makes up the tenant is written in ONE transaction and
committed once:

    user, Organization, users.organization_id, OrgSettings, AppSettings,
    LeavePolicy rows, CommercialOrganization (+ operational_org_id),
    CommercialWorkspace, CommercialSubscription, entitlements cache,
    onboarding checklist, consent bundle, verification token, signup row

If any step fails the whole transaction is rolled back, and the failure
telemetry is then written through a SEPARATE short-lived session. That
separation matters: the pipeline previously recorded failures by committing
the *current* session, which also persisted the half-built user created
earlier in the request. Because ``users.email`` is globally unique and the
duplicate-email check runs first, that orphan permanently blocked the address
from ever registering again. Rolling back first means a failed signup leaves
nothing behind and the visitor can simply retry.

The authenticated session is established AFTER the commit, deliberately. It
is not part of the tenant graph, and ``establish_session`` owns its own
commit/rollback; nesting it inside the tenant transaction would let a failed
device-row insert roll back a perfectly good tenant. If session issuance
fails the tenant still exists and the client falls back to the login page.
"""

# NOTE: deliberately NO `from __future__ import annotations` in this module.
# The endpoint below carries @limiter.limit, and slowapi's wrapper does not (and
# cannot) inherit this module's globals. With postponed annotations FastAPI would
# try to resolve the string "SignupRequest" against slowapi's namespace and fail
# at import time. app/auth/service.py carries @limiter.limit for the same reason
# and likewise avoids postponed annotations.
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database.database import get_db
from app.commercial import (
    constants, ids, state_machines, fraud, routing, identity,
    provisioning, entitlements, consent, challenge,
)
from app.commercial.audit import emit_event
from app.commercial.idempotency import idempotent
from app.commercial.models import CommercialSignup, ManualReviewCase
from app.commercial.notifications import send_welcome_and_verification
from app.commercial.schemas import SignupRequest, SignupSuccess
from app.commercial.routers._helpers import (
    client_meta, error_payload, app_base_url,
)
from app.billing import service as billing_service
from app.org.provisioning_service import TenantProvisioningError
from app.rate_limit import limiter

_log = logging.getLogger("zoiko.commercial.routers.signup")
router = APIRouter(prefix="/api/v1", tags=["Commercial v1"])

# Hand-off slot for the web refresh token, keyed by signup_id.
#
# The session is minted inside the idempotency-wrapped core (which returns a
# plain dict and has no access to the Response object), but a web refresh token
# must leave as an HttpOnly cookie rather than in the JSON body. The endpoint
# below pops the value immediately after the core returns, so an entry lives
# for the remainder of one request. Entries are popped unconditionally; the
# `.pop(..., None)` means a replayed idempotent response simply finds nothing.
_pending_refresh: dict = {}


def _persist_signup_failure(
    *,
    signup_id: str,
    email: str,
    correlation_id: str,
    failure_code: str,
    failure_message: str,
    intent: Optional[str] = None,
    plan: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Record a failed signup attempt in its own session.

    Called only after the request's main transaction has been rolled back, so
    the funnel keeps its telemetry without any half-built tenant surviving.
    Best-effort by design: losing a telemetry row must never turn a clean
    rollback into a 500.
    """
    from database.database import SessionLocal

    audit_db = SessionLocal()
    try:
        audit_db.add(CommercialSignup(
            id=signup_id,
            email=(email or "").strip().lower(),
            state="signup_failed",
            intent=intent or "trial",
            plan=plan,
            ip_address=ip_address,
            failure_code=failure_code,
            failure_message=(failure_message or "")[:500],
            correlation_id=correlation_id,
        ))
        audit_db.commit()
    except Exception:
        audit_db.rollback()
        _log.exception("Could not persist signup failure telemetry for %s", signup_id)
    finally:
        audit_db.close()


def _route_to_redirect(route: str, message_extra: str = "") -> str:
    base = app_base_url()
    if route in {constants.ROUTE_SELF_SERVE_TRIAL, constants.ROUTE_SELF_SERVE_PAID}:
        return f"{base}/onboarding"
    if route == constants.ROUTE_ASSISTED:
        return f"{base}/contact-sales?route=assisted"
    if route == constants.ROUTE_ENTERPRISE:
        return f"{base}/contact-sales?route=enterprise"
    if route == constants.ROUTE_MANUAL_REVIEW:
        return f"{base}/signup/review"
    return f"{base}/signup/blocked"


@idempotent("signup")
def _signup_core(payload, request, db):
    # Core signup pipeline. Wrapped by @idempotent so a repeated Idempotency-Key
    # replays the original response. Kept separate from the FastAPI endpoint
    # below so FastAPI resolves the endpoint's own annotations (this module's
    # globals), not the decorator wrapper's.
    correlation_id = ids.correlation_id()
    ip_address, ua_hash = client_meta(request)
    device_fp = (payload.client_context.device_fingerprint
                 if payload.client_context else None)

    # 1. Persist signup_started row + emit.
    signup_row = CommercialSignup(
        id=ids.signup_id(),
        email=payload.email.strip().lower(),
        state="signup_started",
        intent=payload.intent or "trial",
        plan=payload.plan,
        expected_users=payload.expected_users,
        country=(payload.country or "").upper()[:2] or None,
        locale=(payload.client_context.locale if payload.client_context else None),
        timezone=(payload.client_context.timezone if payload.client_context else None),
        utm_source=(payload.source.utm_source if payload.source else None),
        utm_medium=(payload.source.utm_medium if payload.source else None),
        utm_campaign=(payload.source.utm_campaign if payload.source else None),
        referrer=(payload.source.referrer if payload.source else None),
        ip_address=ip_address,
        user_agent_hash=ua_hash,
        device_fingerprint=device_fp,
        correlation_id=correlation_id,
    )
    db.add(signup_row)
    db.flush()
    emit_event(db, event_type="signup.started", source_service="activation-orchestration",
               payload={"plan": payload.plan, "intent": payload.intent},
               correlation_id=correlation_id)

    try:
        signup_row.state = state_machines.signup.transition("signup_started", "signup_submitted")
        emit_event(db, event_type="signup.submitted", source_service="activation-orchestration",
                   payload={}, correlation_id=correlation_id)

        # 2. Duplicate email check.
        if identity.email_exists(db, payload.email):
            signup_row.state = state_machines.signup.transition("signup_submitted", "validation_failed")
            signup_row.failure_code = constants.ERR_EMAIL_ALREADY_EXISTS
            signup_row.failure_message = "Email already registered"
            db.commit()
            emit_event(db, event_type="signup.validation_failed",
                       source_service="activation-orchestration",
                       payload={"field": "email"}, correlation_id=correlation_id,
                       error_code=constants.ERR_EMAIL_ALREADY_EXISTS, commit=True)
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=error_payload(
                    code=constants.ERR_EMAIL_ALREADY_EXISTS,
                    message="An account already exists for this email address.",
                    field="email",
                    recoverable=True,
                    recovery_action="login_or_reset_password",
                ),
            )

        # 2b. Bot challenge — verified server-side BEFORE any tenant write.
        signup_row.state = state_machines.signup.transition("signup_submitted", "abuse_check_pending")
        chal = challenge.verify(payload.challenge_token, remote_ip=ip_address)
        if not chal.ok:
            code = (constants.ERR_CHALLENGE_REQUIRED
                    if chal.outcome == challenge.OUTCOME_MISSING
                    else constants.ERR_CHALLENGE_FAILED)
            signup_row.state = state_machines.signup.transition("abuse_check_pending", "abuse_blocked")
            signup_row.failure_code = code
            signup_row.failure_message = chal.detail or chal.outcome
            db.commit()
            emit_event(db, event_type="signup.abuse_flagged",
                       source_service="fraud-service",
                       payload={"reason": "challenge", "outcome": chal.outcome},
                       correlation_id=correlation_id,
                       error_code=code, commit=True)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=error_payload(
                    code=code,
                    message=("Please complete the verification challenge and try again."
                             if chal.outcome == challenge.OUTCOME_MISSING
                             else "We couldn't verify that you're human. Please try again."),
                    field="challenge_token",
                    recoverable=True,
                    recovery_action="retry_challenge",
                ),
            )

        # 3. Fraud check + rate limits.
        decision = fraud.evaluate(
            db, email=payload.email, country=payload.country,
            ip_address=ip_address, device_fingerprint=device_fp,
        )
        fraud.record_attempt(db, email=payload.email, ip_address=ip_address,
                             device_fingerprint=device_fp)
        fraud.persist_signals(db, signup_id=signup_row.id, organization_id=None,
                              decision=decision)

        if decision.action == fraud.ACTION_BLOCK:
            signup_row.state = state_machines.signup.transition("abuse_check_pending", "abuse_blocked")
            signup_row.failure_code = (constants.ERR_DISPOSABLE_EMAIL_BLOCKED
                                       if any(s.type == "disposable_email" for s in decision.signals)
                                       else constants.ERR_COUNTRY_RESTRICTED)
            signup_row.failure_message = decision.reason or "Blocked"
            db.commit()
            emit_event(db, event_type="signup.abuse_flagged",
                       source_service="fraud-service",
                       payload={"reason": decision.reason, "score": decision.score},
                       correlation_id=correlation_id,
                       error_code=signup_row.failure_code, commit=True)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=error_payload(
                    code=signup_row.failure_code,
                    message=decision.reason or "Signup blocked by abuse rules.",
                    recoverable=False,
                ),
            )

        # ACTION_CHALLENGE (score >= 40) must NOT fall through to a successful
        # signup. Velocity signals are exactly the pattern automated abuse
        # produces, so a challenged request either proves humanity or is
        # escalated to manual review — it is never silently allowed.
        #
        # If a challenge was actually verified for this request the signal has
        # already been answered, so we downgrade to allow and record it.
        # Otherwise the request joins the manual-review queue below.
        challenge_satisfied = chal.outcome == challenge.OUTCOME_PASSED
        if decision.action == fraud.ACTION_CHALLENGE:
            if challenge_satisfied:
                emit_event(db, event_type="signup.challenge_satisfied",
                           source_service="fraud-service",
                           payload={"score": decision.score, "reason": decision.reason},
                           correlation_id=correlation_id)
            else:
                decision.action = fraud.ACTION_REVIEW
                decision.reason = (
                    (decision.reason or "Velocity signals")
                    + " — challenge not satisfied"
                )

        if decision.action == fraud.ACTION_REVIEW:
            signup_row.state = state_machines.signup.transition("abuse_check_pending", "manual_review_required")
            case = ManualReviewCase(
                case_id=ids.case_id(),
                signup_id=signup_row.id,
                reason="fraud_evaluation",
                severity="high",
                fraud_score=decision.score,
                details={"signals": [{"type": s.type, "severity": s.severity} for s in decision.signals]},
                status="pending",
            )
            db.add(case)
            db.commit()
            emit_event(db, event_type="signup.abuse_flagged",
                       source_service="fraud-service",
                       payload={"reason": decision.reason, "score": decision.score,
                                "case_id": case.case_id},
                       correlation_id=correlation_id,
                       error_code=constants.ERR_MANUAL_REVIEW_REQUIRED, commit=True)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "signup_id": signup_row.id,
                    "manual_review_required": True,
                    "case_id": case.case_id,
                    "message": "Your signup is under review. We'll email you within one business day.",
                },
            )

        # 4. Commercial routing.
        route_decision = routing.route(
            intent=payload.intent, plan=payload.plan,
            expected_users=payload.expected_users,
            email=payload.email, country=payload.country,
            company_name=payload.company_name,
            sso_or_scim_requested=payload.sso_or_scim_requested,
            custom_data_residency=payload.custom_data_residency,
            fraud=decision,
            # Read from the plan catalog rather than assumed: a plan that is not
            # offered as a no-card trial must reach the assisted lane instead of
            # self-provisioning. This is the only caller that provisions, so it
            # is the only one that has to supply it.
            trial_enabled=billing_service.trial_enabled_for(db, payload.plan),
        )
        signup_row.route = route_decision.route

        # Enterprise / assisted / blocked routes don't continue down self-serve.
        if route_decision.route in {constants.ROUTE_ENTERPRISE, constants.ROUTE_ASSISTED}:
            signup_row.failure_code = None
            db.commit()
            return {
                "signup_id": signup_row.id,
                "route": route_decision.route,
                "handoff": route_decision.recommended_handoff,
                "redirect_url": _route_to_redirect(route_decision.route),
                "message": "Routed to our team — we'll be in touch.",
            }
        if route_decision.route == constants.ROUTE_BLOCKED:
            signup_row.state = state_machines.signup.transition("abuse_check_pending", "abuse_blocked")
            signup_row.failure_code = constants.ERR_COUNTRY_RESTRICTED
            db.commit()
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content=error_payload(
                    code=constants.ERR_COUNTRY_RESTRICTED,
                    message=(route_decision.reasons[0] if route_decision.reasons
                             else "Signup not available in your region."),
                    recoverable=False,
                ),
            )

        # 5. Self-serve: create user + run orchestrator.
        signup_row.state = state_machines.signup.transition("abuse_check_pending", "identity_created")
        user = identity.create_provisional_user(
            db, first_name=payload.first_name, last_name=payload.last_name,
            email=payload.email, password=payload.password,
            locale=(payload.client_context.locale if payload.client_context else None),
            timezone_name=(payload.client_context.timezone if payload.client_context else None),
        )
        signup_row.user_id = user.id

        ctx = provisioning.ProvisioningContext(
            signup_id=signup_row.id,
            correlation_id=correlation_id,
            user_id=user.id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            company_name=payload.company_name,
            country=payload.country,
            locale=(payload.client_context.locale if payload.client_context else None),
            timezone=(payload.client_context.timezone if payload.client_context else None),
            plan_code=payload.plan or constants.PLAN_VERIFIED,
            intent=payload.intent or "trial",
            expected_users=payload.expected_users,
            billing_required=(payload.intent or "trial") != "trial",
        )

        try:
            signup_row.state = state_machines.signup.transition("identity_created", "organization_created")
            result = provisioning.provision_tenant(db, ctx)
            signup_row.organization_id = result["organization_id"]
            signup_row.workspace_id = result["workspace_id"]
            signup_row.state = state_machines.signup.transition("organization_created", "workspace_pending")
            signup_row.state = state_machines.signup.transition("workspace_pending", "workspace_ready")

            if ctx.intent == "trial":
                signup_row.state = state_machines.signup.transition("workspace_ready", "trial_created")
                signup_row.state = state_machines.signup.transition("trial_created", "signup_complete")
            else:
                # Paid signup: workspace_ready -> paid_checkout_pending.
                # The frontend will hit /api/v1/billing/checkout-session after
                # this response. We mark workspace_ready here; the checkout
                # endpoint advances the state machine onward.
                signup_row.state = state_machines.signup.transition("workspace_ready", "paid_checkout_pending")

        except TenantProvisioningError as exc:
            # Validation rejected inside the shared provisioning service (e.g.
            # the address already belongs to an organization). Roll the whole
            # transaction back — including the user flushed a moment ago — then
            # record the failure separately.
            _log.warning(
                "Tenant provisioning rejected for %s: %s", payload.email, exc.code
            )
            db.rollback()
            _persist_signup_failure(
                signup_id=signup_row.id, email=payload.email,
                correlation_id=correlation_id,
                failure_code=exc.code, failure_message=exc.detail,
                intent=payload.intent, plan=payload.plan, ip_address=ip_address,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=error_payload(
                    code=exc.code,
                    message=exc.detail,
                    field=exc.field,
                    recoverable=True,
                    recovery_action="login_or_reset_password",
                ),
            )
        except Exception:
            # Provisioning blew up mid-pipeline. Roll back so no orphan user
            # survives to permanently block this email address from retrying,
            # then persist the failure telemetry in its own session.
            _log.exception("Provisioning failed for signup_id=%s", signup_row.id)
            db.rollback()
            _persist_signup_failure(
                signup_id=signup_row.id, email=payload.email,
                correlation_id=correlation_id,
                failure_code=constants.ERR_PROVISIONING_FAILED,
                failure_message="Provisioning pipeline failed",
                intent=payload.intent, plan=payload.plan, ip_address=ip_address,
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=error_payload(
                    code=constants.ERR_PROVISIONING_FAILED,
                    message="We couldn't finish creating your workspace. Our team has been alerted. Please try again.",
                    recoverable=True,
                    recovery_action="retry",
                ),
            )

        # 6. Consent log.
        consent.record_signup_bundle(
            db,
            user_id=user.id,
            signup_id=signup_row.id,
            organization_id=ctx.organization_id,
            terms_version=payload.terms_version,
            privacy_version=payload.privacy_version,
            marketing_consent=payload.marketing_consent,
            ip_address=ip_address,
            user_agent=request.headers.get("user-agent", "") if request else None,
            country=payload.country,
            locale=(payload.client_context.locale if payload.client_context else None),
            correlation_id=correlation_id,
        )

        # 7. Verification token. The email itself is queued after the commit —
        # nothing should reach the customer's inbox describing a tenant whose
        # transaction might still roll back.
        # Both credentials on one row: the 48-hour link for the email, and the
        # short code the Verify step asks the visitor to type. Whichever they
        # use first consumes the same row.
        raw_token, raw_code, _ = identity.issue_verification_credentials(
            db, user_id=user.id, organization_id=ctx.organization_id,
        )

        emit_event(db, event_type="signup.completed",
                   source_service="activation-orchestration",
                   payload={"intent": ctx.intent, "plan": ctx.plan_code,
                            "route": route_decision.route,
                            "operational_org_id": ctx.operational_org_id},
                   organization_id=ctx.organization_id,
                   workspace_id=ctx.workspace_id,
                   user_id=user.id, correlation_id=correlation_id)

        # ── The single commit for the whole tenant graph ──────────────────
        db.commit()

        # Invariant check: a successful self-serve signup must never leave the
        # admin org-less. If this ever trips, the tenant is unusable and the
        # NULL-organization bucket is back — fail loudly rather than hand the
        # caller a success response.
        db.refresh(user)
        if user.organization_id is None:
            _log.error(
                "INVARIANT VIOLATED: signup %s committed with organization_id=NULL "
                "for user %s", signup_row.id, user.id,
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content=error_payload(
                    code=constants.ERR_PROVISIONING_FAILED,
                    message="We couldn't finish setting up your organization. Our team has been alerted.",
                    recoverable=True,
                    recovery_action="contact_support",
                ),
            )

        # ── Post-commit, non-fatal side effects ──────────────────────────
        # Welcome + verification email. A delivery failure must not undo a
        # committed tenant; the user can request a resend.
        try:
            send_welcome_and_verification(
                db, to_email=user.email, first_name=payload.first_name,
                company_name=payload.company_name,
                raw_verification_token=raw_token,
                raw_verification_code=raw_code,
                organization_id=ctx.organization_id,
                workspace_id=ctx.workspace_id,
                user_id=user.id,
                correlation_id=correlation_id,
            )
            db.commit()
        except Exception:
            db.rollback()
            _log.exception(
                "Welcome/verification email failed for signup_id=%s", signup_row.id
            )

        # Authenticated session, so the visitor lands in the setup wizard
        # rather than on the login page. Own transaction (establish_session
        # commits internally); failure is non-fatal — the tenant exists and the
        # client falls back to signing in.
        session_fields: dict = {}
        try:
            from app.auth.service import establish_session
            from app.auth import tokens as rtokens

            issued = establish_session(db, user, request, remember=True)
            session_fields = {
                "access_token": issued["access_token"],
                "token_type": "bearer",
                "expires_in": issued["expires_in"],
                "role": user.role,
            }
            # Desktop clients receive the refresh token in the body; web
            # clients get it as an HttpOnly cookie set by the endpoint wrapper.
            if request is not None and rtokens.is_desktop_client(request):
                session_fields["refresh_token"] = issued["refresh_token"]
            else:
                # Stashed for the endpoint to convert into a cookie.
                _pending_refresh[signup_row.id] = (issued["refresh_token"], True)
        except Exception:
            db.rollback()
            _log.exception(
                "Session establishment failed after signup_id=%s — tenant is "
                "committed, client must sign in manually", signup_row.id,
            )

        return SignupSuccess(
            signup_id=signup_row.id,
            user_id=user.id,
            organization_id=ctx.organization_id,
            operational_org_id=ctx.operational_org_id,
            workspace_id=ctx.workspace_id,
            commercial_state=ctx.commercial_state,
            verification_required=True,
            redirect_url=_route_to_redirect(route_decision.route),
            message="Workspace created successfully.",
            setup_required=True,
            **session_fields,
        ).model_dump()

    except HTTPException:
        raise
    except Exception as e:
        _log.exception("Signup failed for email=%s", payload.email)
        # Roll back FIRST so a half-built user cannot survive and block the
        # address from being registered again, then record the failure through
        # a separate session.
        try:
            db.rollback()
        except Exception:
            pass
        _persist_signup_failure(
            signup_id=signup_row.id, email=payload.email,
            correlation_id=correlation_id,
            failure_code=constants.ERR_INTERNAL, failure_message=str(e),
            intent=payload.intent, plan=payload.plan, ip_address=ip_address,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_payload(
                code=constants.ERR_INTERNAL,
                message="Something went wrong. Please try again or contact support.",
                recoverable=True,
                recovery_action="retry",
            ),
        )


@router.post("/signup")
@limiter.limit(constants.SIGNUP_RATE_LIMIT)
def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """POST /api/v1/signup — commercial activation entry point.

    Supports the optional ``Idempotency-Key`` header (Build Contracts §7.1):
    a retry / double-submit with the same key replays the original response
    instead of creating a second user / org / workspace. Absent the header,
    behavior is unchanged.

    Rate limited per IP (``constants.SIGNUP_RATE_LIMIT``) on top of the
    DB-backed velocity buckets in ``app.commercial.fraud``. Creating an
    organization is a rare action, so the ceiling is much tighter than the
    login routes'.

    On success a session is issued. For web clients the refresh token is
    delivered as an HttpOnly cookie (never also in the body — dual delivery
    would trip the single-use rotation's reuse detection); desktop clients,
    identified by ``X-Client-Type``, receive it in the JSON body instead.
    """
    result = _signup_core(payload=payload, request=request, db=db)

    # Attach the web refresh cookie, if the core stashed one for this signup.
    if isinstance(result, dict):
        signup_id = result.get("signup_id")
        pending = _pending_refresh.pop(signup_id, None) if signup_id else None
        if pending:
            raw_refresh, remember = pending
            try:
                from app.auth import tokens as rtokens
                rtokens.set_refresh_cookie(response, raw_refresh, remember)
            except Exception:
                _log.exception("Could not set refresh cookie for signup %s", signup_id)
    return result
