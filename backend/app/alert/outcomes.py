"""
The typed vocabulary of one ALERT run.
======================================

A VALUE, not an exception, for every ordinary outcome — the same precedent
``eval_day``, ``work_ledger_producer`` and ``eval_event`` set. Exceptions are
reserved for contract violations a caller must not be able to ignore.

WHY THE NAMES ARE FIXED HERE
----------------------------
§32 warns that event names, field names and enum values are a product surface
that cannot be corrected later without a breaking change. Every value below is
written once, scanned by the §31.1 prohibited-term guard, and never derived
from a variable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Optional


class NotificationAudience(str, enum.Enum):
    """WHO a notification artifact is for. A SEPARATE axis from purpose.

    Deliberately not part of the six-field logical episode identity (R3): an
    episode is a fact about a Worker-week, and how many audiences it is
    rendered for cannot change what episode it is.
    """

    WORKER = "WORKER"
    """The Worker the week belongs to. The ONLY audience authorized today."""


#: The audiences this increment may create rows for.
#:
#: WORKER only, and the REASON changed under Stage 5 without the set moving.
#:
#: Until then no designated-recipient model, role, setting or resolver existed
#: anywhere in the repository, so an ADMINISTRATOR row could not be created for
#: want of anybody to put in it. ``app.recipient_authority`` now resolves the
#: four §33.1 audiences by identity under ruling D-3, so that reason is spent.
#: The set stays WORKER-only because DELIVERING to a non-Worker audience is
#: ALERT-DELIVERY, which is still unauthorized — having an authoritative
#: recipient and being permitted to notify them are different questions, and
#: Stage 5 answered only the first. Widening this set is Stage 6's decision.
#:
#: The column remains deliberately open, so that widening needs no migration.
AUTHORIZED_AUDIENCES = frozenset({NotificationAudience.WORKER.value})


class NotificationType(str, enum.Enum):
    """WHAT the notification is for. A separate axis from audience.

    Its own vocabulary rather than a reuse of ``EventType``. For this increment
    the mapping to EVAL-EVENT is one-to-one and a test pins it, but they are
    different surfaces and owner ruling R3 forbids substituting one for the
    other on the strength of today's coincidence.
    """

    THRESHOLD_REACHED = "THRESHOLD_REACHED"
    THRESHOLD_WITHDRAWN = "THRESHOLD_WITHDRAWN"


class RecipientKind(str, enum.Enum):
    """The kind of party a recipient identity denotes.

    An open vocabulary carrying exactly one value today. A future REVIEWER or
    ADMINISTRATOR kind is additive and changes no existing row.
    """

    WORKER = "WORKER"


class NotificationOutcome(str, enum.Enum):
    """What one ALERT run did. Exactly one per call."""

    RECORDED = "RECORDED"
    """A new immutable notification row was appended."""

    ALREADY_RECORDED = "ALREADY_RECORDED"
    """The identity already held a row with the same immutable content, so
    nothing was written and the existing row is returned. Repeated processing
    is therefore idempotent without a boolean or an in-memory memo."""

    REFUSED = "REFUSED"
    """No notification was recordable. ``refusal`` says why; nothing written."""


class RefusalReason(str, enum.Enum):
    """Why a run recorded nothing. Never collapsed into one generic reason."""

    ROLLOUT_OFF = "ROLLOUT_OFF"
    """D8: OFF means no operational ECO notification processing. OFF is also
    the answer to every uncertainty, so an unreadable control refuses here."""

    OPERATIONAL_UNDER_SHADOW_ROLLOUT = "OPERATIONAL_UNDER_SHADOW_ROLLOUT"
    """D8: SHADOW must have no operational effect. An OPERATIONAL-mode event
    under a SHADOW rollout would be exactly that, so it is refused rather than
    quietly recorded."""

    AUDIENCE_NOT_AUTHORIZED = "AUDIENCE_NOT_AUTHORIZED"
    """The audience is not in ``AUTHORIZED_AUDIENCES``. Today that means
    anything other than WORKER — including ADMINISTRATOR, which has no
    authoritative recipient contract (R2)."""

    RECIPIENT_UNRESOLVED = "RECIPIENT_UNRESOLVED"
    """The event's frozen recipient snapshot does not name a usable identity.
    Fail closed: no substitute recipient is ever chosen."""

    EVENT_NOT_FOUND = "EVENT_NOT_FOUND"
    """No ``eco_threshold_event`` row with that id. ALERT never creates one."""

    EVENT_TYPE_NOT_NOTIFIABLE = "EVENT_TYPE_NOT_NOTIFIABLE"
    """The event carries a type ALERT has no notification purpose for."""


@dataclass(frozen=True)
class RecipientIdentity:
    """WHO a notification is for — an identity, never an address.

    ``User.email`` is unique but MUTABLE, so it can never be the identity: a
    Worker who changes their address must remain the same recipient. The
    address is read per delivery attempt and recorded there as history.
    """

    kind: RecipientKind
    user_id: int

    def as_json(self) -> Mapping[str, object]:
        return {"kind": self.kind.value, "user_id": int(self.user_id)}


@dataclass(frozen=True)
class NotificationResult:
    """What happened, in a form a caller cannot misread as success."""

    outcome: NotificationOutcome
    organization_id: int
    user_id: int
    workweek_key: str
    tier: str
    mode: str
    sequence: int

    notification_type: Optional[NotificationType] = None
    audience: Optional[NotificationAudience] = None
    notification_id: Optional[int] = None
    originating_event_id: Optional[int] = None

    refusal: Optional[RefusalReason] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome is NotificationOutcome.REFUSED:
            if self.refusal is None:
                raise ValueError("a REFUSED result must carry a refusal reason")
            if self.notification_id is not None:
                raise ValueError("a refusal wrote nothing, so it carries no id")
            return
        if self.refusal is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a refusal reason")
        if self.notification_id is None:
            raise ValueError(
                f"{self.outcome.value} must identify the notification it concerns")

    @property
    def wrote_notification(self) -> bool:
        """True only when this run actually appended a row."""
        return self.outcome is NotificationOutcome.RECORDED


__all__ = [
    "NotificationAudience",
    "AUTHORIZED_AUDIENCES",
    "NotificationType",
    "RecipientKind",
    "NotificationOutcome",
    "RefusalReason",
    "RecipientIdentity",
    "NotificationResult",
]
