"""
The typed result of an organization's FIRST configuration capture.
==================================================================

A deliberately separate refusal type from the ECO threshold path's. The two
services refuse for overlapping reasons, but an outcome of this service must
never be readable as an approved ECO threshold outcome — including
``EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK``, which shares a name and nothing else.

Every refusal is a value rather than an exception, so a refused attempt can be
audited before the caller sees it. Two prohibited shapes have no refusal reason
at all — an empty snapshot and a threshold-only snapshot are unexpressible,
because ``required_minutes`` is a mandatory parameter of the service.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Optional

#: The §10.1 ladder position for an administrator-supplied organization-wide
#: requirement value (owner ruling 1). ``TENANT_FALLBACK`` is the ladder's
#: last-resort tier, which is exactly what one organization-wide number is:
#: consulted when nothing more specific has been configured. Rank 6 is its
#: position in the six-tier ladder, so ranks 1-5 stay free for the
#: higher-precedence tiers when those are ruled.
#:
#: This increment maps THIS tier only. The remaining five are not defined here
#: and no tier token is invented.
REQUIREMENT_TIER = "TENANT_FALLBACK"
REQUIREMENT_RANK = 6

#: Bounds on the administrator-supplied requirement, in minutes. Transcribed to
#: match — not to redefine — the Step 5 resolver, which records a value outside
#: 1..1440 as an invalid candidate, and the ``ck_config_value_minutes_range``
#: CHECK constraint. Validating here means a value the resolver would later
#: reject is never captured in the first place.
MIN_REQUIRED_MINUTES = 1
MAX_REQUIRED_MINUTES = 1440


class SnapshotShape(str, enum.Enum):
    """Which domains an initial snapshot established (owner ruling 3)."""

    REQUIREMENT_ONLY = "REQUIREMENT_ONLY"
    """Shape A. The organization configured its requirement and no threshold."""

    REQUIREMENT_AND_THRESHOLD = "REQUIREMENT_AND_THRESHOLD"
    """Shape B. Both supplied in the same authorized action."""


class InitialCaptureOutcome(str, enum.Enum):
    CAPTURED = "CAPTURED"
    REFUSED = "REFUSED"


class InitialCaptureRefusal(str, enum.Enum):
    """Why a first capture was refused. Stable tokens; a caller may branch."""

    ACTOR_REQUIRED = "ACTOR_REQUIRED"
    """No actor was supplied. A configuration baseline with no author is not an
    administrator-initiated action."""

    ACTOR_NOT_AUTHORIZED = "ACTOR_NOT_AUTHORIZED"
    """The actor's role is not ``admin`` or ``hr``.

    ``super_admin`` is refused: platform staff must not author a tenant's own
    configuration baseline. A SYSTEM actor is refused too, and that is the one
    place this service is STRICTER than ECO threshold capture — owner decision
    1 requires an explicit administrator action and prohibits creation by any
    implicit background process, which is precisely what a SYSTEM capture is."""

    WRONG_ORGANIZATION = "WRONG_ORGANIZATION"
    """The actor does not belong to the organization being configured.

    An actor carrying no organization at all is refused here as well: the first
    version establishes a tenant's historical floor, so the author must be
    identifiable as belonging to that tenant."""

    REQUIRED_MINUTES_NOT_AN_INTEGER = "REQUIRED_MINUTES_NOT_AN_INTEGER"
    """The requirement is minutes, as a whole number. A float is refused rather
    than rounded, and ``bool`` is refused explicitly because it is an ``int``
    subclass that would otherwise arrive as 1 minute."""

    REQUIRED_MINUTES_OUT_OF_RANGE = "REQUIRED_MINUTES_OUT_OF_RANGE"
    """Outside 1..1440. Zero is refused because a zero requirement would mean
    no shortfall is ever possible; above 1440 a day cannot contain it."""

    EFFECTIVE_FROM_REQUIRED = "EFFECTIVE_FROM_REQUIRED"
    """No usable effective date was supplied.

    Never inferred from the organization's creation date, the current date, the
    earliest attendance record or anything else: the first version fixes the
    historical floor, so the date is a decision, not a derivation."""

    EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK = "EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK"
    """Earlier than the current open workweek's start.

    A backdated first version would assert that this configuration was
    authoritative for completed weeks nobody captured it for. Distinct from the
    ECO threshold path's same-named outcome: different type, different
    service."""

    ORGANIZATION_ALREADY_CONFIGURED = "ORGANIZATION_ALREADY_CONFIGURED"
    """The organization already has at least one configuration version.

    First capture means first. A caller in this position wants the subsequent-
    capture path, and this service does NOT quietly become it — the
    complete-snapshot supersession mechanism belongs to the threshold capture
    path and is not invoked here."""

    VERSION_KEY_ALREADY_IN_USE = "VERSION_KEY_ALREADY_IN_USE"
    """The derived version key is already held.

    Defence in depth. Unreachable through this service, because a key can only
    exist if a version exists, which ``ORGANIZATION_ALREADY_CONFIGURED``
    refuses first. Retained so the guard does not depend on the ordering of the
    check above it."""

    EFFECTIVE_DATE_ALREADY_CLAIMED = "EFFECTIVE_DATE_ALREADY_CLAIMED"
    """A version already starts on that date. Defence in depth, unreachable for
    the same reason as ``VERSION_KEY_ALREADY_IN_USE``."""

    THRESHOLD_NOT_AN_INTEGER = "THRESHOLD_NOT_AN_INTEGER"
    """A threshold was supplied but is not a whole number of minutes. Supplying
    nothing is not this: absent means shape A, not an invalid value."""

    THRESHOLD_OUT_OF_RANGE = "THRESHOLD_OUT_OF_RANGE"
    """A supplied threshold is outside 1..1440."""

    CONCURRENT_CAPTURE = "CONCURRENT_CAPTURE"
    """Another writer claimed this organization's first version while this one
    was in flight. Fails closed; the caller re-reads and re-decides."""

    SNAPSHOT_VERIFICATION_FAILED = "SNAPSHOT_VERIFICATION_FAILED"
    """What was stored does not match what was intended.

    The persisted values are re-read and compared field-for-field, and the
    stored digest is recomputed from the stored rows. A mismatch means the
    write would have established a baseline nobody authored, so it is rolled
    back rather than left standing."""


@dataclass(frozen=True)
class InitialCaptureResult:
    """What happened, in a form the caller cannot misread as success."""

    outcome: InitialCaptureOutcome
    organization_id: int

    shape: Optional[SnapshotShape] = None
    required_minutes: Optional[int] = None
    threshold_minutes: Optional[int] = None
    effective_from: Optional[date] = None
    config_version_id: Optional[str] = None
    content_hash: Optional[str] = None
    value_count: Optional[int] = None

    refusal_reason: Optional[InitialCaptureRefusal] = None

    #: Human-readable detail for an administrator. Never parsed, and never the
    #: source of truth for classification — ``refusal_reason`` is.
    detail: Optional[str] = None

    @property
    def is_captured(self) -> bool:
        return self.outcome is InitialCaptureOutcome.CAPTURED

    def __post_init__(self) -> None:
        if self.outcome is InitialCaptureOutcome.CAPTURED:
            if self.required_minutes is None:
                raise ValueError(
                    "CAPTURED must carry required_minutes; an initial snapshot "
                    "without the requirement domain is prohibited"
                )
            if self.config_version_id is None or self.effective_from is None:
                raise ValueError(
                    "CAPTURED must identify the version it created"
                )
            if self.shape is None or self.content_hash is None:
                raise ValueError("CAPTURED must state its shape and digest")
            if (self.shape is SnapshotShape.REQUIREMENT_AND_THRESHOLD) != (
                    self.threshold_minutes is not None):
                raise ValueError(
                    f"shape {self.shape.value} disagrees with "
                    f"threshold_minutes={self.threshold_minutes!r}"
                )
            if self.refusal_reason is not None:
                raise ValueError("CAPTURED carries no refusal reason")
        else:
            if self.refusal_reason is None:
                raise ValueError("REFUSED must state its reason")
            for field_name in ("required_minutes", "threshold_minutes",
                               "config_version_id", "content_hash",
                               "value_count", "shape"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"{self.refusal_reason.value} wrote nothing, so it "
                        f"must not carry {field_name}"
                    )


__all__ = [
    "REQUIREMENT_TIER",
    "REQUIREMENT_RANK",
    "MIN_REQUIRED_MINUTES",
    "MAX_REQUIRED_MINUTES",
    "SnapshotShape",
    "InitialCaptureOutcome",
    "InitialCaptureRefusal",
    "InitialCaptureResult",
]
