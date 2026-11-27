"""
The notification platform's dispatch contract.
==============================================

ZT-SPEC-ECO-002 v2.0 §34, notification-platform owner rulings D-1…D-4,
architecture decision D-A (internal platform module) and D-B (a distinct
non-suppressible administrator class).

    dispatch(handoff: NotificationHandoff) -> DispatchResult

THE BOUNDARY THIS FILE DRAWS
----------------------------
Everything a CALLER may state is in ``NotificationHandoff``. Everything the
PLATFORM decides is in ``DispatchResult``. The two structures share no field,
which is the whole point: a caller cannot name a channel, a preference outcome,
a suppression decision or a retry instruction, because there is nowhere to put
one. §34 says channel selection, preference resolution, retry and suppression
"MUST NOT be reimplemented" outside this service, and the shape of the contract
is the first line of that enforcement — not a convention somebody has to
remember.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

#: D-5.5. The bound is normative because the ECO attempt table is append-only
#: and therefore un-redactable: a diagnostic written once cannot be trimmed
#: later.
MAX_FAILURE_DETAIL_CHARS = 512


class DispatchOutcome(str, enum.Enum):
    """D-2: exactly two values. Suppression is NOT a third one.

    A suppressed notification is ``DISPATCHED`` carrying a suppression
    ``reason_code``, because the handoff completed and the platform's policy
    decided against transmission. Adding a third member here would reopen a
    locked ruling.
    """

    DISPATCHED = "dispatched"
    FAILED = "failed"


class Channel(str, enum.Enum):
    """The logical channels the platform can actually deliver on TODAY.

    EMAIL only, and deliberately so. The repository has one working transport;
    SMS, push and in-app exist as preference booleans with nothing behind them,
    and the authorization forbids inventing them. Resend and SMTP are two
    PROVIDERS of this one channel, not two channels — a Resend failure that
    falls back to SMTP is still ``EMAIL``.

    Adding a member is a platform change. ECO neither defines nor validates
    this vocabulary.
    """

    EMAIL = "email"


class ReasonCode(str, enum.Enum):
    """A CLOSED vocabulary. Never provider text.

    D-5.5 requires the diagnostic to be a service reason code, and an
    enumerated code is what makes the no-PII guarantee structural rather than
    aspirational: a member of this enum cannot contain a name or an address,
    whereas a provider's error string can — a Resend 422 body echoes the
    recipient it rejected.

    Five categories, as the authorization requires them to be distinguishable.
    """

    # ── successful transport ────────────────────────────────────────────────
    TRANSPORTED = "TRANSPORTED"
    """The channel accepted the message. Not a receipt: §34 and D-5.1 are
    explicit that "notified" means the handoff was made and the outcome
    recorded, never confirmed end-user receipt."""

    # ── platform suppression (outcome is still DISPATCHED) ──────────────────
    SUPPRESSED_BY_PREFERENCE = "SUPPRESSED_BY_PREFERENCE"
    """The recipient's notification preferences declined this class."""

    SUPPRESSED_QUIET_HOURS = "SUPPRESSED_QUIET_HOURS"
    """Inside the recipient's quiet hours, and the class is quiet-hours
    eligible. ECO's administrator class never reaches this (D-B)."""

    SUPPRESSED_UNSUBSCRIBED = "SUPPRESSED_UNSUBSCRIBED"
    """The destination is on the unsubscribe list for this class."""

    # ── transport failure ───────────────────────────────────────────────────
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    """Every provider for the selected channel refused or errored."""

    # ── configuration failure ───────────────────────────────────────────────
    NO_ELIGIBLE_CHANNEL = "NO_ELIGIBLE_CHANNEL"
    """Channel selection completed and chose nothing. Distinct from
    suppression: suppression is a POLICY decision applied to a channel that was
    selected, whereas this is the absence of a selection. D-2's ordering
    presupposes a selected channel, so this can only be FAILED."""

    RECIPIENT_UNDELIVERABLE = "RECIPIENT_UNDELIVERABLE"
    """The recipient identity resolves to no usable destination on the selected
    channel — no address on file, or one the channel rejects as malformed.

    NOT the same as ECO's ``RECIPIENT_UNRESOLVED`` (D-1), which is about an
    AUDIENCE nobody was designated for and is decided by ECO before any handoff
    is built. This is a delivery-time fact about a recipient ECO did resolve."""

    CHANNEL_NOT_CONFIGURED = "CHANNEL_NOT_CONFIGURED"
    """The deployment has no credentials for the selected channel."""

    # ── platform failure ────────────────────────────────────────────────────
    PLATFORM_ERROR = "PLATFORM_ERROR"
    """The platform itself failed. Under D-A the module is in-process, so this
    is an exception rather than a network error — and §34's "the record is
    authoritative; delivery is best-effort" means it must never propagate into
    ECO's record as anything but a failed dispatch."""

    INVALID_HANDOFF = "INVALID_HANDOFF"
    """The handoff is malformed, names an unknown audience or type, or crosses
    a tenant boundary. Nothing was dispatched."""

    SHADOW_REFUSED = "SHADOW_REFUSED"
    """Defence in depth only. ECO must not call ``dispatch()`` in SHADOW at
    all; this exists so that a bug on the ECO side fails closed rather than
    sending. It is NOT a second rollout authority, and ECO must not treat it as
    an ordinary delivery failure."""


#: Reason codes that accompany ``DISPATCHED``. Everything else means FAILED.
#:
#: This mapping is what stops the two axes drifting apart: a future member
#: cannot be added without deciding, explicitly, which side of it the code
#: falls on.
DISPATCHED_REASON_CODES = frozenset({
    ReasonCode.TRANSPORTED,
    ReasonCode.SUPPRESSED_BY_PREFERENCE,
    ReasonCode.SUPPRESSED_QUIET_HOURS,
    ReasonCode.SUPPRESSED_UNSUBSCRIBED,
})

#: The subset meaning "the platform decided not to transmit". Recorded so a
#: reader answering "was this recipient actually informed?" has one place to
#: look, rather than re-deriving the set from the member names.
SUPPRESSION_REASON_CODES = frozenset({
    ReasonCode.SUPPRESSED_BY_PREFERENCE,
    ReasonCode.SUPPRESSED_QUIET_HOURS,
    ReasonCode.SUPPRESSED_UNSUBSCRIBED,
})


class DispatchMode(str, enum.Enum):
    """Carried on the handoff so a SHADOW call fails closed (defence in depth).

    ECO owns the rollout decision and must not call ``dispatch()`` in SHADOW.
    The platform refusing one is a backstop, never a second authority.
    """

    OPERATIONAL = "OPERATIONAL"
    SHADOW = "SHADOW"


class InvalidHandoff(Exception):
    """The handoff cannot be dispatched and nothing was attempted."""


@dataclass(frozen=True)
class NotificationHandoff:
    """What a caller states. Immutable.

    NOTE WHAT IS ABSENT, and that the absence is the contract: no channel, no
    provider, no address, no preference result, no suppression result, no retry
    policy, no rendered channel-specific body. A caller cannot supply any of
    them because the structure has no field for one.
    """

    organization_id: int
    eco_event_id: int
    recipient_user_id: int
    audience: str
    notification_type: str
    #: The locked copy keys and their values. Legitimately carries Worker PII —
    #: §33.3's NOTIF-A-01 names the Worker, bounded by §33.4. That is CONTENT.
    #: D-5.5's no-PII rule governs the DIAGNOSTIC, which is a different field
    #: on a different structure, and the two must not be conflated.
    content_inputs: Mapping[str, Any] = field(default_factory=dict)
    #: ECO's frozen event-time recipient snapshot (D-3.7). The platform reads
    #: it and MUST NOT rewrite it or re-resolve recipient authority from it.
    recipient_snapshot: Mapping[str, Any] = field(default_factory=dict)
    #: D-4. Derived from ``(eco_event_id, recipient_user_id)``; see
    #: ``idempotency.request_key``.
    idempotency_key: str = ""
    mode: DispatchMode = DispatchMode.OPERATIONAL

    def canonical_payload(self) -> Dict[str, Any]:
        """The bytes the request hash is taken over (D-4, clause 4).

        Deliberately EXCLUDES ``idempotency_key`` (it is the lookup key, not
        payload) and ``mode`` (an OPERATIONAL retry of a SHADOW-refused call is
        a different request, not a payload mutation — and a SHADOW call never
        reaches the store in the first place).
        """
        return {
            "organization_id": int(self.organization_id),
            "eco_event_id": int(self.eco_event_id),
            "recipient_user_id": int(self.recipient_user_id),
            "audience": str(self.audience),
            "notification_type": str(self.notification_type),
            "content_inputs": dict(self.content_inputs or {}),
            "recipient_snapshot": dict(self.recipient_snapshot or {}),
        }


@dataclass(frozen=True)
class DispatchResult:
    """What the platform decided. Immutable.

    Every field is platform-owned. ECO persists them verbatim and interprets
    none of them — in particular it does not re-derive ``outcome`` from
    ``reason_code`` or vice versa.
    """

    outcome: DispatchOutcome
    channel: Optional[Channel]
    reason_code: ReasonCode
    #: D-3: platform-generated, one per handoff, stable across platform-internal
    #: retries, NEVER a provider message id.
    correlation_id: str
    #: D-5.5: <= 512 characters, no Worker PII, NULL permitted. Sanitised — raw
    #: provider error text never reaches it.
    failure_detail: Optional[str] = None

    def __post_init__(self) -> None:
        expected = (DispatchOutcome.DISPATCHED
                    if self.reason_code in DISPATCHED_REASON_CODES
                    else DispatchOutcome.FAILED)
        if self.outcome is not expected:
            raise ValueError(
                f"{self.reason_code.value} implies {expected.value}, "
                f"not {self.outcome.value}")
        if self.failure_detail is not None:
            if len(self.failure_detail) > MAX_FAILURE_DETAIL_CHARS:
                raise ValueError(
                    f"failure_detail is {len(self.failure_detail)} characters; "
                    f"D-5.5 caps it at {MAX_FAILURE_DETAIL_CHARS}")
        if not self.correlation_id:
            raise ValueError("correlation_id is required on every result")

    @property
    def suppressed(self) -> bool:
        """True when the platform decided not to transmit.

        §34 records delivery state so a Tenant asserting "the Worker was
        informed" can be checked against what happened. Under D-2 a suppressed
        and a transmitted notification BOTH read ``dispatched``, so ``outcome``
        alone cannot answer that question — this property is the supported way
        to ask it, and reading ``outcome`` without ``reason_code`` overstates
        delivery.
        """
        return self.reason_code in SUPPRESSION_REASON_CODES


__all__ = [
    "MAX_FAILURE_DETAIL_CHARS",
    "DISPATCHED_REASON_CODES",
    "SUPPRESSION_REASON_CODES",
    "Channel",
    "DispatchMode",
    "DispatchOutcome",
    "DispatchResult",
    "InvalidHandoff",
    "NotificationHandoff",
    "ReasonCode",
]
