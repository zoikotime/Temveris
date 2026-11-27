"""Typed refusals for break classification."""

from __future__ import annotations


class BreakClassificationError(Exception):
    """Base for every refusal this package raises."""


class InvalidClassificationAuthority(BreakClassificationError):
    """An authority value outside its governed domain."""


class InvalidClassificationAssertion(BreakClassificationError):
    """An assertion value outside its governed domain, refused before anything
    reaches the session."""


class ClassificationImmutable(BreakClassificationError):
    """An attempt to UPDATE or DELETE a recorded authority, assertion or
    snapshot.

    An assertion is evidence about what an authority determined. A changed
    determination is a NEW version that supersedes the old one; rewriting it
    would make a historical evaluation's stated basis change under it.
    """


class ConflictingClassification(BreakClassificationError):
    """Two live assertions disagree about one break.

    Refused rather than resolved by ordering, recency or majority. Picking a
    winner would be inventing the legal rule P-1 explicitly withholds.
    """


class ClassificationRefused(BreakClassificationError):
    """The post-seal precondition was not met — an unsealed or missing ledger,
    an integrity fault, or a source reference that does not name a break this
    day's evidence contains."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


__all__ = [
    "BreakClassificationError",
    "InvalidClassificationAuthority",
    "InvalidClassificationAssertion",
    "ClassificationImmutable",
    "ConflictingClassification",
    "ClassificationRefused",
]
