"""
The typed result of a working-day classification.
=================================================

A VALUE, not an exception, following the ``eco_threshold_capture`` and
``work_ledger_producer`` precedent: an outcome the caller must inspect is
harder to drop on the floor than an exception someone catches broadly.

WHY ``UNCLASSIFIED`` CANNOT CARRY A BOOLEAN
------------------------------------------
Owner ruling D17-A. "The classifier could not establish whether this is a
working day" and "this is not a working day" are different facts, and only the
second may zero a day (D8-A). The dataclass therefore REFUSES construction of
an ``UNCLASSIFIED`` result that carries ``is_working_day``, the same guarantee
``DayEvaluation`` and ``ProducerResult`` already make for their own
non-computed states — a caller cannot read a classification off a refusal.

``UNCLASSIFIED`` is never persisted, and no ``UNKNOWN`` evaluation state is
introduced: R4's six ``EcoDayState`` values stay locked.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import time
from typing import Optional


class ClassificationOutcome(str, enum.Enum):
    """What the classifier established. Exactly one per call."""

    WORKING_DAY = "WORKING_DAY"
    """Work was expected. The day is evaluated normally: a working day with no
    intervals at all is still a working day and may become a shortfall
    (D17-A). Absence of work is never evidence of a non-working day."""

    NOT_A_WORKING_DAY = "NOT_A_WORKING_DAY"
    """No work was expected (§24.2). Everything zeroes (R5) and a record is
    still written so the week fold stays complete and explainable."""

    UNCLASSIFIED = "UNCLASSIFIED"
    """The classification could not be established. Fails closed (D8-A): no
    ``NOT_A_WORKING_DAY`` is written, no ``UNKNOWN`` state is invented, and the
    day stays outside the zeroed path until a valid classification exists."""


class ClassificationRefusal(str, enum.Enum):
    """Why a classification could not be established. Never collapsed into one
    generic reason: an operator has to be able to tell "nobody configured a
    working week" from "the resolver reported a label we do not admit"."""

    SOURCE_NOT_CONFIGURED = "SOURCE_NOT_CONFIGURED"
    """The resolver's source label is ``system`` — its documented fail-open
    default, meaning no tier configured the working week. Permanently
    inadmissible (D6-A): a system default must never produce an authoritative
    classification, however convenient the answer would be."""

    SOURCE_UNKNOWN = "SOURCE_UNKNOWN"
    """The resolver reported a label outside the ratified vocabulary. Refused
    rather than mapped onto a permitted one, because inferring a more specific
    source than the resolver stated is exactly the fabrication D5-A's
    ratification forbids."""

    NOT_SEALED = "NOT_SEALED"
    """A frozen classification was requested for a ledger whose state carries
    none — ``OPEN``, ``MISSING_CLOCK_OUT`` or ``LEDGER_INTEGRITY_FAULT``. Those
    are lifecycle or fault conditions, not classifications, and none of them
    may be read as one."""

    CONTEXT_MISSING = "CONTEXT_MISSING"
    """A sealed ledger carries no usable ``working_day_source``. The frozen
    context is the authoritative history (D3-A), so an incomplete one is
    refused rather than repaired from current configuration."""


@dataclass(frozen=True)
class WorkingDayClassification:
    """The classification, in a form a caller cannot misread as a boolean.

    ``is_working_day`` is populated ONLY on ``WORKING_DAY`` and
    ``NOT_A_WORKING_DAY``; ``refusal`` only on ``UNCLASSIFIED``. Enforced at
    construction, so ``if classification.is_working_day`` cannot silently read
    ``None`` as "not a working day".
    """

    outcome: ClassificationOutcome
    is_working_day: Optional[bool] = None

    #: The verbatim resolver tier that produced the answer. D11-A: this is the
    #: whole provenance requirement — no actor, no timestamp, no classification
    #: object. Absent on a refusal.
    working_day_source: Optional[str] = None

    #: ── D-1.1: the scheduled end, from the SAME resolution ──────────────────
    #: `EffectiveSchedule.end_local` and its `sources["end_local"]` tier, taken
    #: from the ONE schedule resolution that produced the working-day verdict
    #: above (the classifier owns that call; this module holds only its
    #: result). They ride here so the producer can freeze both halves of the
    #: day's schedule evidence without resolving the schedule a second time —
    #: "the schedule must be resolved once for the sealing operation".
    #:
    #: Both are ADVISORY to the classification and never affect it: unlike
    #: `working_day_source`, a `scheduled_end_source` of "system" does not
    #: refuse (owner ruling D-2.1), and a missing scheduled end is a recorded
    #: absence rather than an unestablished classification. They are therefore
    #: unconstrained by `__post_init__`, including on UNCLASSIFIED, where they
    #: stay None because no resolution was trusted.
    scheduled_end_local: Optional[time] = None
    scheduled_end_source: Optional[str] = None

    refusal: Optional[ClassificationRefusal] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome is ClassificationOutcome.UNCLASSIFIED:
            if self.refusal is None:
                raise ValueError(
                    "an UNCLASSIFIED classification must carry a refusal reason"
                )
            if self.is_working_day is not None:
                raise ValueError(
                    "UNCLASSIFIED must not carry a working-day boolean: "
                    "'cannot be established' is not 'not a working day' (D17-A)"
                )
            if self.working_day_source is not None:
                raise ValueError(
                    "UNCLASSIFIED must not carry a source: there is no "
                    "provenance for a classification that was not made"
                )
            return

        if self.refusal is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a refusal reason"
            )
        if self.is_working_day is None:
            raise ValueError(
                f"{self.outcome.value} must state the working-day boolean"
            )
        if not self.working_day_source:
            raise ValueError(
                f"{self.outcome.value} must carry its source label (D11-A)"
            )
        expected = self.outcome is ClassificationOutcome.WORKING_DAY
        if bool(self.is_working_day) is not expected:
            raise ValueError(
                f"{self.outcome.value} contradicts is_working_day="
                f"{self.is_working_day!r}"
            )

    @property
    def is_established(self) -> bool:
        """True when a classification was actually made.

        The one property a caller should branch on before reading
        ``is_working_day``.
        """
        return self.outcome is not ClassificationOutcome.UNCLASSIFIED


__all__ = [
    "ClassificationOutcome",
    "ClassificationRefusal",
    "WorkingDayClassification",
]
