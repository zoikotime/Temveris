"""
The EMAIL channel adapter — the only place ECO's notifications meet a provider.
==============================================================================

Below the platform boundary. ECO never imports this module, and a guard asserts
that; ECO also never imports ``app.email_service``.

WHAT THIS ADAPTER OWNS
----------------------
Resolving the recipient identity to a destination, invoking the repository's
existing email transport, and NORMALISING what comes back into the platform's
closed vocabulary.

WHY IT CALLS ``_send`` WITHOUT A GOVERNANCE CONTEXT
---------------------------------------------------
``email_service._send`` contains its own preference/suppression gate, engaged
only when the caller supplies ``db`` and ``recipient_user_id``. This adapter
deliberately supplies NEITHER, and the reason is a correctness one rather than
a shortcut:

Under D-2 the PLATFORM evaluates preferences and suppression, and it does so
BEFORE transport is reached. By the time this adapter is called that decision
has already been made and was to proceed. Re-running the same gate here would
evaluate it twice — and ``_send`` reports a suppressed message by returning
``True`` (line: *"intentionally not sent; NOT a delivery failure"*), which is
indistinguishable from a successful send. A second gate could therefore turn a
"transmit" decision into a silent non-transmission that this adapter would
report as ``TRANSPORTED``. That is precisely the overstatement D-2 and §34
exist to prevent.

So governance runs exactly once, at the platform layer, and this adapter is
pure transport.

WHY DIAGNOSTICS ARE COARSE
--------------------------
``_send`` returns a bare ``bool``. Its provider-level detail — Resend's
``{ok, error, id}`` — is produced internally and discarded before the public
surface. This adapter therefore cannot report WHICH provider failed or why, and
it does not invent a reason: a failure becomes ``TRANSPORT_FAILED`` with a
sanitised, PII-free detail.

That is a deliberate limitation, not an oversight. Surfacing richer diagnostics
means changing ``email_service`` to return its provider result, which is a
separate change with its own authorization. **ponytail: coarse transport
diagnostics; surface `_send`'s `{error, id}` if per-provider failure attribution
is ever needed.**

WHAT NEVER CROSSES THE BOUNDARY
-------------------------------
Raw provider error text, provider message ids, and the recipient's address.
D-3 forbids a provider id becoming the correlation id; D-5.5 forbids Worker
contact data in the diagnostic. Both are enforced here by never putting them
into the result in the first place.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from .contracts import Channel, ReasonCode

_log = logging.getLogger("zoiko.notification_platform.email")


def resolve_destination(db: Session, user_id: int) -> Optional[str]:
    """The recipient's address, read at transport time.

    An ADDRESS, resolved from the IDENTITY the handoff named. The identity is
    authoritative and frozen (D-3.7); the address is current and is history the
    moment it is used. The adapter returns it to the transport and never to the
    caller — no platform result carries it.
    """
    from database import models as db_models

    row = db.query(db_models.User.email).filter(
        db_models.User.id == int(user_id)).first()
    if row is None or not row[0]:
        return None
    return str(row[0]).strip() or None


def send(
    db: Session, *, user_id: int, subject: str, html: str,
) -> Tuple[bool, ReasonCode, Optional[str]]:
    """Transmit, and normalise the outcome.

    Returns ``(ok, reason_code, failure_detail)``. ``failure_detail`` is None on
    success and a short sanitised string otherwise — never provider text.
    """
    destination = resolve_destination(db, user_id)
    if not destination:
        # The identity is fine; there is simply no address to send to. Distinct
        # from suppression (nothing declined it) and from a channel being
        # unconfigured (the channel is fine).
        return False, ReasonCode.RECIPIENT_UNDELIVERABLE, \
            "no destination on file for the recipient on channel email"

    try:
        from app import email_service
    except Exception:  # pragma: no cover - import failure is a platform fault
        _log.exception("email transport unavailable")
        return False, ReasonCode.PLATFORM_ERROR, "email transport unavailable"

    if not getattr(email_service, "RESEND_API_KEY", "") \
            and not getattr(email_service, "SMTP_PASS", ""):
        # Neither provider is configured. A configuration fault, and saying so
        # distinctly is what lets an operator fix the right thing.
        return False, ReasonCode.CHANNEL_NOT_CONFIGURED, \
            "no email provider credentials configured for this deployment"

    try:
        # NO ``db`` / ``recipient_user_id``: governance already ran at the
        # platform layer. See the module docstring — passing them would
        # double-evaluate suppression and could silently convert a transmit
        # decision into a non-transmission reported as success.
        ok = bool(email_service._send(to=destination, subject=subject,
                                      html=html))
    except Exception as exc:
        # The exception TYPE only. Its message can quote the address, the
        # subject or a provider payload, any of which would breach D-5.5.
        _log.exception("email transport raised")
        return False, ReasonCode.TRANSPORT_FAILED, \
            f"email transport raised {type(exc).__name__}"

    if ok:
        return True, ReasonCode.TRANSPORTED, None
    return False, ReasonCode.TRANSPORT_FAILED, \
        "all configured email providers refused the message"


#: The one channel this module serves.
CHANNEL = Channel.EMAIL

__all__ = ["CHANNEL", "resolve_destination", "send"]
