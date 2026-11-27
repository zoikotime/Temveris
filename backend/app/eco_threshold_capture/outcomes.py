"""
The typed result of an ECO threshold capture, and who performed it.
===================================================================

Every refusal is a value, not an exception: ruling G requires refused attempts
to be audited, and an outcome the caller must inspect is harder to drop on the
floor than an exception someone catches broadly. Structural impossibilities —
a per-Worker scope, an arbitrary tier — are absent from the API instead, so
they have no refusal reason here at all.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Optional

#: Roles authorized to capture an organization's ECO threshold (ruling C).
#:
#: ``super_admin`` is deliberately absent. The threshold is the organization's
#: own policy decision; platform staff capturing it on a tenant's behalf would
#: record a decision the tenant never made, under an attribution that looks
#: like theirs.
AUTHORIZED_ROLES = frozenset({"admin", "hr"})

#: The explicit actor label for an authorized system capture (ruling E). Never a
#: fallback for a human actor who should have been supplied — a capture with no
#: actor at all is refused.
SYSTEM_ACTOR_LABEL = "SYSTEM"


class CaptureOutcome(str, enum.Enum):
    CAPTURED = "CAPTURED"
    REFUSED = "REFUSED"


class RefusalReason(str, enum.Enum):
    """Why a capture was refused. Stable tokens; a caller may branch on them."""

    ACTOR_REQUIRED = "ACTOR_REQUIRED"
    """No actor was supplied. Ruling E: a silent NULL actor is prohibited."""

    ACTOR_NOT_AUTHORIZED = "ACTOR_NOT_AUTHORIZED"
    """The actor's role is not ``admin`` or ``hr`` for this organization —
    ``super_admin`` included (ruling C)."""

    WRONG_ORGANIZATION = "WRONG_ORGANIZATION"
    """The actor belongs to a different organization than the one being
    configured. Organization scope is enforced before anything is written."""

    VALUE_NOT_AN_INTEGER = "VALUE_NOT_AN_INTEGER"
    """The threshold is minutes, as a whole number. A float is refused rather
    than rounded: rounding here would invent a value nobody chose."""

    VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
    """Outside 1..1440 inclusive. Zero is refused because a zero threshold is
    reached by every workweek."""

    EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK = "EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK"
    """Ruling H: an effective date earlier than the current open workweek's
    start would change what a completed workweek resolves to."""

    VERSION_PREDATES_OPEN_WORKWEEK = "VERSION_PREDATES_OPEN_WORKWEEK"
    """The configuration version that would receive the threshold row is itself
    effective before the current open workweek.

    Checked on the version actually being written, not only on the requested
    date — a threshold reaching an older version reaches back into completed
    workweeks without any backdated ``effective_from`` at all."""

    THRESHOLD_ALREADY_CAPTURED = "THRESHOLD_ALREADY_CAPTURED"
    """A threshold is already recorded for that effective date. Configuration is
    immutable: correct it by capturing a LATER version, never by rewriting."""

    EXISTING_VERSION_CANNOT_RECEIVE_THRESHOLD = (
        "EXISTING_VERSION_CANNOT_RECEIVE_THRESHOLD")
    """A configuration version already starts on that effective date.

    Permanent behaviour, per owner ruling 2. ``(organization_id,
    effective_from)`` is unique, so a second version cannot start there; and a
    version is immutable, so the existing one cannot receive a new value. The
    administrator selects a later eligible effective date, and the capture
    supersedes the snapshot from there."""

    VERSION_KEY_ALREADY_IN_USE = "VERSION_KEY_ALREADY_IN_USE"
    """The version key derived from the effective date is already held by a
    configuration version effective on a DIFFERENT date.

    ``version_key`` is caller-supplied elsewhere in Step 6, so another writer
    may already hold a date-shaped key that does not match its own date.
    Detected before the write so the unique constraint's ``IntegrityError`` is
    not misreported as a concurrent capture, which it is not."""

    NO_CONFIGURATION_TO_CARRY_FORWARD = "NO_CONFIGURATION_TO_CARRY_FORWARD"
    """The organization has no configuration version to supersede.

    A capture writes a COMPLETE snapshot, so it needs one to carry forward.
    Nothing is invented here: ruling A prohibits a default and ruling 9
    prohibits a synthetic baseline. Nor is a threshold-only version written —
    that would flip every other domain from "nothing captured" to "captured but
    unresolved", sending an administrator after a tier that was never
    configured. Establishing an organization's first configuration version is a
    Step 6 question."""

    SNAPSHOT_VERIFICATION_FAILED = "SNAPSHOT_VERIFICATION_FAILED"
    """The new snapshot did not verify against the one it superseded.

    Defence in depth: the carried values are re-read from storage and compared
    against the source, the stored digest is recomputed from the stored rows,
    and the superseded version is confirmed unchanged. A mismatch means the
    write would have fabricated configuration history, so it is rolled back."""

    CONCURRENT_CAPTURE = "CONCURRENT_CAPTURE"
    """Another capture claimed the same organization and effective date while
    this one was in flight. Fails closed; the caller re-reads and re-decides."""


@dataclass(frozen=True)
class ActorContext:
    """Who is capturing, and what may be recorded about them durably.

    ``label`` is what survives deletion of the user row (ruling F). It is an
    identifying label and nothing more — no name, no email, no role history.
    """

    label: str
    user_id: Optional[int] = None
    role: Optional[str] = None
    organization_id: Optional[int] = None
    is_system: bool = False

    def __post_init__(self) -> None:
        if not (self.label or "").strip():
            raise ValueError(
                "an actor label is required; ruling E prohibits an actorless "
                "capture, and a blank label is an actorless capture"
            )
        if self.is_system:
            if self.user_id is not None:
                raise ValueError("a SYSTEM capture has no user_id")
        elif self.user_id is None:
            raise ValueError(
                "a human capture must carry the authenticated user's id"
            )

    @classmethod
    def system(cls, label: str = SYSTEM_ACTOR_LABEL) -> "ActorContext":
        """An authorized system capture. Explicit, never inferred."""
        return cls(label=label, user_id=None, role=None, is_system=True)

    @classmethod
    def for_user(cls, user) -> "ActorContext":
        """From an authenticated user record.

        The label prefers a stable identifier over a display name: it has to
        remain meaningful once the row it came from is gone, and it must not
        become a place personal data accumulates.
        """
        user_id = getattr(user, "id", None)
        email = (getattr(user, "email", None) or "").strip()
        label = email or (f"user:{user_id}" if user_id is not None else "")
        return cls(
            label=label,
            user_id=user_id,
            role=getattr(user, "role", None),
            organization_id=getattr(user, "organization_id", None),
            is_system=False,
        )


@dataclass(frozen=True)
class ThresholdCaptureResult:
    """What happened, in a form the caller cannot misread as success.

    ``threshold_minutes`` and the version identity are populated only on
    ``CAPTURED``; ``refusal_reason`` only on ``REFUSED``. Enforced at
    construction, matching the reader and the requirement resolver.
    """

    outcome: CaptureOutcome
    organization_id: int

    threshold_minutes: Optional[int] = None
    effective_from: Optional[date] = None
    config_version_id: Optional[str] = None

    #: The version this capture superseded — the snapshot whose values were
    #: carried forward. Populated on ``CAPTURED``; there is always one, because
    #: a capture with nothing to carry forward is refused.
    superseded_config_version_id: Optional[str] = None

    #: How many values were carried forward from it, excluding the threshold
    #: itself. Lets a caller see that a snapshot was preserved, not replaced.
    carried_value_count: Optional[int] = None

    #: The threshold that was in force for that effective date beforehand, when
    #: one was resolvable. Provenance for the audit record (ruling G).
    previous_threshold_minutes: Optional[int] = None
    previous_config_version_id: Optional[str] = None

    refusal_reason: Optional[RefusalReason] = None

    #: Human-readable detail for an administrator. Never parsed.
    detail: Optional[str] = None

    @property
    def is_captured(self) -> bool:
        return self.outcome is CaptureOutcome.CAPTURED

    def __post_init__(self) -> None:
        if self.outcome is CaptureOutcome.CAPTURED:
            if self.threshold_minutes is None:
                raise ValueError("CAPTURED must carry threshold_minutes")
            if self.config_version_id is None or self.effective_from is None:
                raise ValueError(
                    "CAPTURED must identify the version it was written to"
                )
            if self.superseded_config_version_id is None:
                raise ValueError(
                    "CAPTURED must identify the snapshot it superseded; a "
                    "capture with nothing to carry forward is refused"
                )
            if self.carried_value_count is None:
                raise ValueError("CAPTURED must state how much it carried forward")
            if self.refusal_reason is not None:
                raise ValueError("CAPTURED carries no refusal reason")
        else:
            if self.refusal_reason is None:
                raise ValueError("REFUSED must state its reason")
            if self.threshold_minutes is not None:
                raise ValueError(
                    f"{self.refusal_reason.value} must not carry a captured "
                    f"threshold (got {self.threshold_minutes!r})"
                )
            if (self.superseded_config_version_id is not None
                    or self.carried_value_count is not None):
                raise ValueError(
                    f"{self.refusal_reason.value} wrote no snapshot, so it "
                    f"superseded nothing"
                )


__all__ = [
    "AUTHORIZED_ROLES",
    "SYSTEM_ACTOR_LABEL",
    "CaptureOutcome",
    "RefusalReason",
    "ActorContext",
    "ThresholdCaptureResult",
]
