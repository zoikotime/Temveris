"""Typed results for classification resolution."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import FrozenSet, Mapping, Optional

from .errors import InvalidClassificationAssertion

#: Refusal reasons, mirroring ``app.eval_day.errors``' string-constant style so
#: a caller can carry the reason through without re-wording it.
REASON_LEDGER_NOT_SEALED = "CLASSIFICATION_LEDGER_NOT_SEALED"
REASON_NO_LEDGER_DAY = "CLASSIFICATION_NO_LEDGER_DAY"
REASON_LEDGER_INTEGRITY_FAULT = "CLASSIFICATION_LEDGER_INTEGRITY_FAULT"
REASON_CONFLICTING_ASSERTIONS = "CLASSIFICATION_CONFLICTING_ASSERTIONS"
REASON_UNKNOWN_CLASSIFICATION = "CLASSIFICATION_UNKNOWN_VALUE"
REASON_MALFORMED_SOURCE_REF = "CLASSIFICATION_MALFORMED_SOURCE_REF"

#: The break-treatment profile itself could not be resolved deterministically.
#:
#: NOT the same failure as an unresolved break classification. The owner ruling
#: on the opt-in gate is explicit that only ``NO_VERSION`` means "no applicable
#: profile exists, therefore classification is not required"; a profile that
#: cannot be resolved must FAIL CLOSED rather than be read as an absent one.
#: These two reasons carry that distinction to the recording boundary, which
#: refuses the day before any classification or evaluation proceeds — so they
#: never become ``CLASSIFICATION_UNRESOLVED``, which would say the breaks were
#: looked at and found indeterminate when in fact nothing was looked at.
REASON_PROFILE_AMBIGUOUS = "BREAK_TREATMENT_PROFILE_AMBIGUOUS"
REASON_PROFILE_INVALID = "BREAK_TREATMENT_PROFILE_INVALID"


class ClassificationOutcome(str, enum.Enum):
    """Whether a day's break classification could be established."""

    RESOLVED = "RESOLVED"
    """Every break on the day carries exactly one live, determinate
    assertion — or the day contains no breaks at all, which is an explicit
    EMPTY snapshot rather than an absence."""

    UNRESOLVED = "UNRESOLVED"
    """At least one break is UNCLASSIFIABLE, or has no live assertion.

    The day defers: owner ruling D-1 maps this to
    ``EvaluationDeferralReason.CLASSIFICATION_UNRESOLVED``. It is a truthful
    determination, NOT a refusal — the day is expected to become evaluable."""

    REFUSED = "REFUSED"
    """A precondition failed: unsealed or missing ledger, integrity fault,
    malformed source reference, conflicting assertions, or an unknown
    classification value. No ECO result may be derived."""


UNUSABLE_OUTCOMES: FrozenSet[ClassificationOutcome] = frozenset({
    ClassificationOutcome.UNRESOLVED,
    ClassificationOutcome.REFUSED,
})


@dataclass(frozen=True)
class ClassificationResolution:
    """The answer to "how were this day's breaks classified?"."""

    outcome: ClassificationOutcome
    organization_id: int
    user_id: int
    work_date: date

    #: The content-addressed key of the frozen assertion set. Present for
    #: RESOLVED and for UNRESOLVED — an unresolved day still froze exactly what
    #: it saw, which is what makes its deferral replayable and what makes the
    #: later resolution a *different* snapshot and therefore a new generation.
    snapshot_key: Optional[str] = None

    #: ``source_ref -> classification``, for the members that were determinate.
    classifications: Mapping[str, str] = field(default_factory=dict)

    #: The source refs that could not be determined. Empty unless UNRESOLVED.
    unclassifiable_refs: tuple = ()

    reason: Optional[str] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome is ClassificationOutcome.REFUSED:
            if self.reason is None:
                raise InvalidClassificationAssertion(
                    "REFUSED requires a reason")
            if self.snapshot_key is not None:
                raise InvalidClassificationAssertion(
                    "REFUSED cannot carry a snapshot_key; nothing was frozen")
        if self.outcome is ClassificationOutcome.RESOLVED:
            if self.unclassifiable_refs:
                raise InvalidClassificationAssertion(
                    "RESOLVED cannot carry unclassifiable refs")
            if self.snapshot_key is None:
                raise InvalidClassificationAssertion(
                    "RESOLVED requires a snapshot_key")
        if self.outcome is ClassificationOutcome.UNRESOLVED:
            if not self.unclassifiable_refs:
                raise InvalidClassificationAssertion(
                    "UNRESOLVED requires at least one unclassifiable ref")

    @property
    def is_resolved(self) -> bool:
        return self.outcome is ClassificationOutcome.RESOLVED

    @property
    def is_refused(self) -> bool:
        return self.outcome is ClassificationOutcome.REFUSED

    @property
    def defers(self) -> bool:
        """True when the day must be recorded as CLASSIFICATION_UNRESOLVED."""
        return self.outcome is ClassificationOutcome.UNRESOLVED


__all__ = [
    "ClassificationOutcome",
    "ClassificationResolution",
    "UNUSABLE_OUTCOMES",
    "REASON_LEDGER_NOT_SEALED",
    "REASON_NO_LEDGER_DAY",
    "REASON_LEDGER_INTEGRITY_FAULT",
    "REASON_CONFLICTING_ASSERTIONS",
    "REASON_UNKNOWN_CLASSIFICATION",
    "REASON_MALFORMED_SOURCE_REF",
    "REASON_PROFILE_AMBIGUOUS",
    "REASON_PROFILE_INVALID",
]
