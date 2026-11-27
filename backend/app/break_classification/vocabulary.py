"""
The CLOSED classification vocabulary (owner ruling P-1).
=======================================================

Three values, and a fourth would be a specification change rather than a code
change — the same rule ``NettingMode`` states for §15's three modes.
"""

from __future__ import annotations

import enum
from typing import Any, FrozenSet, Optional


class ClassificationResult(str, enum.Enum):
    """What an authority asserted about one break.

    ``BRK_M`` and ``BRK_R`` are the two ledger ``WorkState`` members INV-4
    requires be kept legally distinct. They are spelled identically here on
    purpose: an assertion names the canonical state, so nothing has to map one
    vocabulary onto another and no third spelling of "meal break" exists.
    """

    BRK_M = "BRK_M"
    """Meal break. §13.2's ``meal_qualifies`` decides whether it counts;
    P-4 fixes that it never qualifies as REST through the rest path."""

    BRK_R = "BRK_R"
    """Rest break. Qualifies only where the resolved profile says
    ``rest_qualifies``, capped cumulatively per day (P-4)."""

    UNCLASSIFIABLE = "UNCLASSIFIABLE"
    """The authority looked and could not determine the category.

    An EXPLICIT outcome, not an absence. §13.2's ``unrecorded_break_treatment``
    defaults to DEFER, and owner ruling D-1 makes that concrete: the day defers
    with ``CLASSIFICATION_UNRESOLVED`` rather than being silently read as
    ``BRK_M``, ``BRK_R`` or ``WRK``. Recording "we do not know" is honest;
    guessing is the D-01 defect this whole control exists to prevent."""


#: Every result an assertion may carry.
CLASSIFICATION_RESULTS: FrozenSet[str] = frozenset(
    r.value for r in ClassificationResult)

#: The results that actually classify a break. ``UNCLASSIFIABLE`` is a
#: determination, not a classification, so it is deliberately not here.
DETERMINATE_RESULTS: FrozenSet[str] = frozenset({
    ClassificationResult.BRK_M.value,
    ClassificationResult.BRK_R.value,
})


def is_valid_classification(value: Any) -> bool:
    """True only for one of the three governed values. Exact match.

    No case folding, no trimming, no alias table: ``brk_r`` is not ``BRK_R``,
    and a near-miss must fail rather than be helpfully corrected into a legal
    determination nobody made.
    """
    if isinstance(value, ClassificationResult):
        return True
    return isinstance(value, str) and value in CLASSIFICATION_RESULTS


def parse_classification(value: Any) -> Optional[ClassificationResult]:
    """The enum member for ``value``, or None if it is not a governed value."""
    if isinstance(value, ClassificationResult):
        return value
    if isinstance(value, str) and value in CLASSIFICATION_RESULTS:
        return ClassificationResult(value)
    return None


__all__ = [
    "ClassificationResult",
    "CLASSIFICATION_RESULTS",
    "DETERMINATE_RESULTS",
    "is_valid_classification",
    "parse_classification",
]
