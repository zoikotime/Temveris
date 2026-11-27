"""
Temveris — the central notification platform boundary.
=======================================================

ZT-SPEC-ECO-002 v2.0 §34; notification-platform owner rulings D-1…D-4;
architecture decisions D-A (internal platform module) and D-B (a distinct
non-suppressible administrator class).

STATUS: FOUNDATION IMPLEMENTED, NOT WIRED TO ECO. No ECO path calls
``dispatch()`` yet; the idempotency table has zero rows.

THE PUBLIC ENTRY POINT

    dispatch(db, handoff: NotificationHandoff) -> DispatchResult

WHY IT IS IN-PROCESS (D-A)
--------------------------
§34 separates RESPONSIBILITY, not processes. It requires that channel
selection, preference resolution, retry and suppression are not reimplemented
by the caller — not that their owner runs in a different container. The owner
ruled an internal platform module, which satisfies §34 without inventing a
deployable, a service-authentication mechanism or a tracing stack that this
repository does not have.

The boundary is therefore enforced by discipline plus tests rather than by a
network hop, following ``app.eco_view_authority``'s precedent: a self-contained
primitive whose isolation is pinned by AST import guards. Specifically:

    ECO packages import ``app.notification_platform`` and NOTHING below it.
    ECO packages import ``app.email_service`` NEVER.
    This package imports no ECO package.

WHAT ECO OWNS, AND MAY NOT DELEGATE
-----------------------------------
event identity · recipient authority · recipient snapshot · content inputs ·
rollout · the ECO delivery record.

WHAT THIS PACKAGE OWNS, AND ECO MAY NOT DO
------------------------------------------
channel selection · preference resolution · suppression · retry · transport ·
dispatch · ``correlation_id`` · request idempotency · ``DispatchResult``.

``NotificationHandoff`` has no field for a channel, a provider, an address, a
preference result, a suppression result or a retry instruction, so a caller
cannot supply one. The contract enforces the split; no convention has to be
remembered.

WHAT IS DELIBERATELY ABSENT
---------------------------
* **No second channel.** EMAIL only — the one transport this repository has.
  ``in_app``/``push``/``sms`` are preference booleans with nothing behind them,
  and claiming them would claim deliveries that cannot occur.
* **No provider in the contract.** Resend and SMTP are two providers of the one
  EMAIL channel. A Resend failure falling back to SMTP is still ``EMAIL``.
* **No provider identifiers crossing the boundary.** D-3: the correlation id is
  minted here and is never a provider message id.
* **No raw provider error text.** ``reason_code`` is a closed enumeration and
  ``failure_detail`` is sanitised — which is what makes D-5.5's PII bound
  structural rather than a promise.
* **No retry.** The platform owns it; none is implemented yet, and ECO must
  never add one.
* **No HTTP surface.** No route, no schema, no dependency wrapper.
* **No ECO integration.** ``dispatch()`` is callable and proven; nothing in
  ``app/eval_event`` or ``app/alert`` calls it. Wiring it is a separate
  authorization, and GV-07/GV-18 remain blocked.

SHADOW
------
ECO must not call ``dispatch()`` in SHADOW. The platform refusing a SHADOW
handoff is defence in depth only — a backstop against an ECO-side bug, never a
second rollout authority. A SHADOW call writes nothing and creates no
idempotency row.
"""

from .channels import notification_class_for, select_channel
from .contracts import (
    DISPATCHED_REASON_CODES,
    MAX_FAILURE_DETAIL_CHARS,
    SUPPRESSION_REASON_CODES,
    Channel,
    DispatchMode,
    DispatchOutcome,
    DispatchResult,
    InvalidHandoff,
    NotificationHandoff,
    ReasonCode,
)
from .correlation import (
    MAX_CORRELATION_ID_CHARS,
    is_platform_correlation_id,
    new_correlation_id,
)
from .idempotency import (
    DispatchInProgress,
    RequestHashConflict,
    request_hash,
    request_key,
)
from .models import NotificationDispatchRequest
from .service import dispatch

__all__ = [
    # public entry point
    "dispatch",
    # contract
    "NotificationHandoff",
    "DispatchResult",
    "DispatchOutcome",
    "DispatchMode",
    "Channel",
    "ReasonCode",
    "DISPATCHED_REASON_CODES",
    "SUPPRESSION_REASON_CODES",
    "MAX_FAILURE_DETAIL_CHARS",
    # correlation (D-3)
    "MAX_CORRELATION_ID_CHARS",
    "new_correlation_id",
    "is_platform_correlation_id",
    # idempotency (D-4)
    "request_key",
    "request_hash",
    "DispatchInProgress",
    "RequestHashConflict",
    "NotificationDispatchRequest",
    # channel selection
    "select_channel",
    "notification_class_for",
    # failures
    "InvalidHandoff",
]
