"""Bot challenge verification for public signup.

Public organization creation is an abuse-sensitive operation: it mints a
tenant, an admin account and a trial subscription from an unauthenticated
request. The fraud engine (``app.commercial.fraud``) scores velocity and
reputation signals, but scoring alone cannot distinguish a script from a
person — that needs a proof-of-humanity challenge, which is what this module
verifies.

DESIGN
------
The provider is Cloudflare Turnstile, verified server-side against the
siteverify endpoint. The provider's response is authoritative; a client-side
widget render proves nothing.

CONFIGURATION
-------------
``SIGNUP_CHALLENGE_SECRET``
    Provider secret key. When unset the module reports itself as
    *not configured* and ``verify()`` returns ``skipped``.
``SIGNUP_CHALLENGE_REQUIRED``
    ``"true"``/``"1"``/``"yes"`` to hard-require a valid challenge token.
    Defaults to whether a secret is configured.

FAIL-OPEN vs FAIL-CLOSED
------------------------
Two different failure modes, treated differently on purpose:

* *Not configured* — the deployment has not set a secret. The challenge is
  skipped and a warning is logged. This keeps local development, CI and the
  existing super-admin-provisioned deployments working, and matches how the
  email transports in ``app.email_service`` degrade. Production must set the
  secret; ``is_enforced()`` is exposed so a readiness check can assert it.

* *Configured but the token is missing, malformed, reused or rejected* —
  fail CLOSED. The request is refused. Note this includes the case where the
  provider itself is unreachable: when a challenge is required we do not let
  a network problem at the provider become a bypass.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger("zoiko.commercial.challenge")

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_VERIFY_TIMEOUT_SECONDS = 6.0

# Outcomes
OUTCOME_PASSED = "passed"
OUTCOME_SKIPPED = "skipped"       # not configured — see module docs
OUTCOME_MISSING = "missing"       # required but the client sent no token
OUTCOME_FAILED = "failed"         # provider rejected the token
OUTCOME_UNAVAILABLE = "unavailable"  # provider unreachable while required

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass
class ChallengeResult:
    outcome: str
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when the request may proceed."""
        return self.outcome in (OUTCOME_PASSED, OUTCOME_SKIPPED)


def _secret() -> str:
    return (os.getenv("SIGNUP_CHALLENGE_SECRET") or "").strip()


def is_configured() -> bool:
    return bool(_secret())


def is_enforced() -> bool:
    """Whether a valid challenge token is mandatory for public signup."""
    raw = (os.getenv("SIGNUP_CHALLENGE_REQUIRED") or "").strip().lower()
    if raw:
        return raw in _TRUTHY
    return is_configured()


def verify(token: Optional[str], *, remote_ip: Optional[str] = None) -> ChallengeResult:
    """Verify a challenge token server-side.

    Returns a :class:`ChallengeResult`; callers check ``.ok``. Never raises.
    """
    secret = _secret()
    enforced = is_enforced()

    if not secret:
        if enforced:
            # Explicitly required but impossible to verify — refuse rather
            # than silently allow.
            _log.error(
                "SIGNUP_CHALLENGE_REQUIRED is set but SIGNUP_CHALLENGE_SECRET is not — "
                "refusing signups instead of failing open."
            )
            return ChallengeResult(OUTCOME_UNAVAILABLE, "challenge not configured")
        _log.warning(
            "Signup bot challenge is NOT configured (SIGNUP_CHALLENGE_SECRET unset) — "
            "public signup is unprotected against automated abuse."
        )
        return ChallengeResult(OUTCOME_SKIPPED, "not configured")

    token = (token or "").strip()
    if not token:
        if not enforced:
            return ChallengeResult(OUTCOME_SKIPPED, "not enforced")
        return ChallengeResult(OUTCOME_MISSING, "challenge token missing")

    try:
        import httpx

        data = {"secret": secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        with httpx.Client(timeout=_VERIFY_TIMEOUT_SECONDS) as client:
            resp = client.post(TURNSTILE_VERIFY_URL, data=data)
        body = resp.json() if resp.content else {}
    except Exception as e:
        _log.error("Challenge provider verification failed: %s", e)
        if enforced:
            return ChallengeResult(OUTCOME_UNAVAILABLE, "challenge provider unreachable")
        return ChallengeResult(OUTCOME_SKIPPED, "provider unreachable, not enforced")

    if body.get("success") is True:
        return ChallengeResult(OUTCOME_PASSED)

    codes = body.get("error-codes") or []
    _log.info("Challenge token rejected: %s", codes)
    return ChallengeResult(OUTCOME_FAILED, ",".join(str(c) for c in codes) or "rejected")
