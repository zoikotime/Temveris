"""
The typed result of resolving an ECO threshold.
===============================================

Five outcomes, one of which carries a number. The invariant is enforced at
construction, not by convention: a caller cannot obtain an object that reports
"unresolved" and also carries a threshold, and cannot obtain a "resolved" one
that carries nothing.

WHY NOT ``Optional[int]``
-------------------------
An optional integer collapses five distinct situations — no configuration
history reaches this date, a version exists but nobody configured a threshold,
somebody configured one wrongly, somebody configured a prohibited per-Worker
one, and a real value — into "a number or nothing". The caller that has to
decide whether an evaluation may proceed then has to reconstruct the difference,
and the one that forgets reads ``None`` as zero. An unresolved threshold is not
a threshold of zero: a threshold of zero would be reached by every workweek.

Same shape as ``requirement_resolution.outcomes``, deliberately: the two layers
fail closed the same way, so a reader of one already knows how to read the other.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Optional


class ThresholdOutcome(str, enum.Enum):
    """Why the reader returned what it returned."""

    RESOLVED = "RESOLVED"
    """Exactly one valid organization-scoped threshold row was in force.
    ``threshold_minutes`` is set."""

    NO_CONFIG_VERSION = "NO_CONFIG_VERSION"
    """The as-of date precedes the earliest captured configuration version.

    There is no historical configuration to resolve against, and live/current
    configuration MUST NOT be substituted — that substitution is the defect the
    versioning layer exists to remove."""

    THRESHOLD_NOT_CONFIGURED = "THRESHOLD_NOT_CONFIGURED"
    """A version governs the date, but it holds no organization-scoped threshold
    row, and no prohibited per-Worker row either.

    Distinct from ``THRESHOLD_INVALID``: nobody configured a threshold, as
    opposed to somebody configuring one that cannot be used."""

    THRESHOLD_INVALID = "THRESHOLD_INVALID"
    """A threshold row exists but cannot be resolved deterministically.

    Wrong tier, absent value, out-of-range value, or two or more candidate rows.
    Recorded as a defect rather than skipped: a malformed threshold is something
    an administrator needs to see, and quietly ignoring it would leave the
    organization with no threshold and no indication of why."""

    WORKER_SCOPED_THRESHOLD_PROHIBITED = "WORKER_SCOPED_THRESHOLD_PROHIBITED"
    """The threshold key is present with a non-NULL ``scope_user_id``.

    There is no per-Worker ECO threshold. Such a row is a configuration
    dimension no ruling permits, so the whole resolution fails closed rather than
    falling back to the organization row beside it — see ``reader``."""


#: Outcomes that permit no threshold comparison of any kind.
NON_EVALUABLE_OUTCOMES = frozenset({
    ThresholdOutcome.NO_CONFIG_VERSION,
    ThresholdOutcome.THRESHOLD_NOT_CONFIGURED,
    ThresholdOutcome.THRESHOLD_INVALID,
    ThresholdOutcome.WORKER_SCOPED_THRESHOLD_PROHIBITED,
})

#: Outcomes an administrator can actually act on. A missing or malformed
#: threshold is fixable configuration; a date below the historical floor is a
#: property of the cutover, not a defect anyone can repair.
ADMINISTRATOR_ACTIONABLE_OUTCOMES = frozenset({
    ThresholdOutcome.THRESHOLD_NOT_CONFIGURED,
    ThresholdOutcome.THRESHOLD_INVALID,
    ThresholdOutcome.WORKER_SCOPED_THRESHOLD_PROHIBITED,
})


@dataclass(frozen=True)
class ThresholdResolution:
    """One organization's ECO threshold as of one workweek start date.

    Frozen: a resolution is evidence about a past workweek, and evidence that
    can be edited after the fact is not evidence.
    """

    outcome: ThresholdOutcome
    organization_id: int

    #: The workweek start date the resolution was made for — the parsed W4
    #: ``workweek_key``, supplied by the caller. Never a clock reading.
    as_of: date

    #: Set ONLY when the outcome is RESOLVED. Never 0, never a default.
    threshold_minutes: Optional[int] = None

    #: The immutable configuration version the answer came from. Present for
    #: every outcome except NO_CONFIG_VERSION, where by definition none applies.
    config_version_id: Optional[str] = None
    effective_from: Optional[date] = None

    #: Why the configuration could not be used, for an administrator. A stable
    #: token, not prose: a caller may branch on it without parsing English.
    defect: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        return self.outcome is ThresholdOutcome.RESOLVED

    @property
    def is_evaluable(self) -> bool:
        """Whether a threshold comparison may proceed.

        A caller that ignores this and reads ``threshold_minutes`` gets
        ``None``, not a number — the type makes fail-closed hard to bypass by
        accident.
        """
        return self.outcome is ThresholdOutcome.RESOLVED

    @property
    def requires_administrator_alert(self) -> bool:
        return self.outcome in ADMINISTRATOR_ACTIONABLE_OUTCOMES

    def __post_init__(self) -> None:
        # The invariant that matters most, enforced by construction rather than
        # by convention.
        if self.outcome is not ThresholdOutcome.RESOLVED:
            if self.threshold_minutes is not None:
                raise ValueError(
                    f"{self.outcome.value} must not carry threshold_minutes "
                    f"(got {self.threshold_minutes!r}); an unresolved threshold "
                    f"is not a threshold of zero"
                )
        elif self.threshold_minutes is None:
            raise ValueError("RESOLVED must carry threshold_minutes")


__all__ = [
    "ThresholdOutcome",
    "NON_EVALUABLE_OUTCOMES",
    "ADMINISTRATOR_ACTIONABLE_OUTCOMES",
    "ThresholdResolution",
]
