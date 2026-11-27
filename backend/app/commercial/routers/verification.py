"""Email verification endpoints (§14 + §19 — unlocks expansion actions)."""

# No `from __future__ import annotations` here: the resend endpoint carries
# @limiter.limit, and slowapi's wrapper cannot resolve this module's postponed
# annotations. Same constraint as routers/signup.py and app/auth/service.py.
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User
from app.auth.service import get_current_user
from app.commercial import identity, entitlements, constants
from app.commercial.audit import emit_event
from app.commercial.models import OnboardingChecklist, CommercialWorkspace
from app.commercial.notifications import send_verification_resend
from app.commercial.routers._helpers import error_payload, client_meta
from app.rate_limit import limiter
from datetime import datetime, timezone


_log = logging.getLogger("zoiko.commercial.routers.verification")
router = APIRouter(prefix="/api/v1", tags=["Commercial v1"])


def _apply_verification(db: Session, user, *, via: str) -> dict:
    """Everything that happens once an email is proven, whichever credential
    proved it.

    Extracted so the emailed link and the typed code cannot drift: both stamp
    the same milestone, tick the same checklist item, recompute the same
    entitlements and emit the same event. ``via`` is recorded on the event so
    the funnel can tell the two apart, and is the only difference between them.
    """
    # Mark first_email_verified_at on the workspace (any workspace for this owner).
    ws = (
        db.query(CommercialWorkspace)
          .join(CommercialWorkspace.organization)
          .filter(CommercialWorkspace.organization.has(owner_user_id=user.id))
          .first()
    )
    if ws and ws.first_email_verified_at is None:
        ws.first_email_verified_at = datetime.now(timezone.utc)
    # Tick the onboarding checklist.
    if ws:
        cl = db.query(OnboardingChecklist).filter_by(organization_id=ws.organization_id).one_or_none()
        if cl and not cl.email_confirmed:
            cl.email_confirmed = True
            cl.email_confirmed_at = datetime.now(timezone.utc)

    org_id = ws.organization_id if ws else None
    entitlements.invalidate(db, org_id) if org_id else None
    if org_id:
        entitlements.compute(db, org_id, email_verified=True)

    emit_event(db, event_type="email_verification.completed",
               source_service="identity-service",
               payload={"via": via}, organization_id=org_id, user_id=user.id)
    db.commit()
    return {"verified": True, "user_id": user.id, "organization_id": org_id}


@router.post("/email-verification/verify")
def verify_email(body: dict, db: Session = Depends(get_db)):
    """Consume the token from the welcome email.

    Unauthenticated, and deliberately unchanged: the credential is 43
    characters of CSPRNG output, the person clicking may not be signed in, and
    links already sitting in inboxes have to keep working. The typed-code path
    is a separate, session-gated endpoint below — six digits cannot be accepted
    on an open endpoint.
    """
    raw_token = body.get("token")
    if not raw_token:
        raise HTTPException(400, "token required")

    user, err = identity.consume_verification_token(db, raw_token=raw_token)
    if err:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_payload(
                code=err if err == "ALREADY_CONSUMED" else "EMAIL_VERIFICATION_FAILED",
                message=("This link has expired." if err == "TOKEN_EXPIRED"
                         else "This link is no longer valid."),
                recoverable=True,
                recovery_action="request_new_verification_email",
            ),
        )
    return _apply_verification(db, user, via="token")


# Identical wording for a wrong digit, a wrong length and a missing row —
# nothing here tells a guesser how close they were.
_CODE_MESSAGES = {
    constants.ERR_VERIFICATION_CODE_INVALID:
        "That code isn't right. Check the email and try again.",
    constants.ERR_VERIFICATION_CODE_EXPIRED:
        "That code has expired. Send yourself a new one.",
    constants.ERR_VERIFICATION_CODE_LOCKED:
        "Too many incorrect codes. Send yourself a new one to try again.",
}
_CODE_RECOVERY = {
    constants.ERR_VERIFICATION_CODE_INVALID: "retry",
    constants.ERR_VERIFICATION_CODE_EXPIRED: "request_new_verification_email",
    constants.ERR_VERIFICATION_CODE_LOCKED: "request_new_verification_email",
}


@router.post("/email-verification/verify-code")
@limiter.limit(constants.VERIFICATION_VERIFY_RATE_LIMIT)
def verify_email_code(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verify the 6-digit code shown on the activation flow's Verify step.

    **Authenticated on purpose.** The identity being verified is the session's,
    never an address in the request body. Six digits on an open endpoint would
    let anyone verify an address they do not control; behind a session, an
    attacker must already hold the account, at which point the code proves
    nothing they have not already got.

    Four layers against brute force: the session requirement, a per-row attempt
    counter that locks at ``VERIFICATION_CODE_MAX_ATTEMPTS``, a short code TTL,
    and a per-IP ceiling on this route.

    Idempotent. Verifying twice returns ``verified: true`` rather than an
    error — the second half of a double-click, or a refresh after success, is
    not a failure.
    """
    code = (body or {}).get("code")

    if identity.is_email_verified(db, int(current_user.id)):
        # Already settled. Report it; recompute nothing.
        return {"verified": True, "user_id": current_user.id, "already_verified": True}

    user, err = identity.consume_verification_code(
        db, user_id=int(current_user.id), code=code or "",
    )

    if err == "ALREADY_CONSUMED":
        return {"verified": True, "user_id": current_user.id, "already_verified": True}

    if err:
        db.commit()      # persist the incremented attempt counter
        return JSONResponse(
            status_code=(status.HTTP_429_TOO_MANY_REQUESTS
                         if err == constants.ERR_VERIFICATION_CODE_LOCKED
                         else status.HTTP_400_BAD_REQUEST),
            content=error_payload(
                code=err,
                message=_CODE_MESSAGES.get(err, "We couldn't verify that code."),
                field="code",
                recoverable=True,
                recovery_action=_CODE_RECOVERY.get(err, "retry"),
            ),
        )

    return _apply_verification(db, user, via="code")


@router.get("/email-verification/status")
def verification_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What the Verify step needs to render itself on load and after a refresh.

    Server-authoritative on both counts: the address comes from the session
    rather than the query string, and the countdown is derived from the stored
    ``last_sent_at`` rather than a constant the client guesses at — so a
    refresh resumes the real remaining cooldown instead of restarting it.
    """
    return {
        "email": current_user.email,
        "verified": identity.is_email_verified(db, int(current_user.id)),
        "code_length": constants.VERIFICATION_CODE_LENGTH,
        "resend_cooldown_seconds": constants.VERIFICATION_RESEND_COOLDOWN_SECONDS,
        "resend_available_in": identity.resend_cooldown_remaining(
            db, user_id=int(current_user.id),
        ),
    }


@router.post("/email-verification/resend")
@limiter.limit(constants.VERIFICATION_RESEND_RATE_LIMIT)
def resend_verification(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
):
    """Re-send the verification email for an account.

    Deliberately unauthenticated: a user who has not yet verified may also not
    yet have signed in, and a signed-out visitor with a stale link needs a way
    back. That makes the endpoint an abuse surface, so three properties are
    enforced here:

    1. **Identified by email only.** ``user_id`` is no longer accepted. It was a
       sequential integer, which let an unauthenticated caller walk the whole
       user table and mint a verification email for every account.
    2. **Uniform response.** The same ``202`` body is returned whether or not
       the address exists, and whether or not the per-user cooldown has
       elapsed. Previously a known address returned ``200`` while an unknown one
       returned ``400``, which is an account-existence oracle. Rate limiting is
       the only condition that changes the status code, and that decision is
       made per IP rather than per account, so it leaks nothing.
    3. **Rate limited per IP.** The per-user 60-second cooldown in
       ``app.commercial.identity`` is a fairness control, not an abuse control;
       it does nothing against a caller iterating addresses.
    """
    from database.models import User

    email = (body or {}).get("email")
    email = (email or "").strip()

    # Uniform response used for every non-rate-limited outcome.
    uniform = JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "resent": True,
            "message": (
                "If an unverified account exists for that address, "
                "we've sent a new verification email."
            ),
        },
    )

    if not email or "@" not in email:
        # Same shape as a successful call — a malformed address must not be
        # distinguishable from an unknown one.
        return uniform

    u = db.query(User).filter(User.email.ilike(email)).first()
    if u is None:
        return uniform

    raw, raw_code, err = identity.resend_verification_credentials(db, user_id=int(u.id))
    if err == constants.ERR_RATE_LIMITED:
        # Per-user cooldown. Reported through the uniform body so it cannot be
        # used to prove the address exists.
        return uniform

    if raw:
        first_name = (u.name or u.email).split()[0]
        try:
            send_verification_resend(
                db, to_email=u.email, first_name=first_name,
                raw_verification_token=raw, raw_verification_code=raw_code,
                user_id=u.id, organization_id=None,
            )
        except Exception:
            # Never surface transport state to an unauthenticated caller.
            _log.exception("Verification resend delivery failed for user %s", u.id)
    db.commit()
    return uniform
