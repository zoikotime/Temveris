"""Errors the PolicyVersion capture path raises."""

from __future__ import annotations


class PolicyVersionError(Exception):
    """Base for every refusal in this package."""


class InvalidPolicyVersion(PolicyVersionError):
    """A supplied attribute is outside its governed domain.

    Raised BEFORE anything is written, so a rejected capture leaves the store
    byte-identical rather than a partial row to be rolled back.
    """


class PolicyVersionImmutable(PolicyVersionError):
    """An attempt to modify or delete a captured policy.

    The ORM half of the guarantee. The database triggers are the guarantee;
    this raises a sentence instead of an opaque IntegrityError for the common
    case where the ORM can see the attempt.
    """


class PolicyVersionCollision(PolicyVersionError):
    """The version key or the effective date is already claimed.

    Two versions cannot start on the same date — "which policy governed that
    day" must have exactly one answer.
    """


__all__ = [
    "PolicyVersionError",
    "InvalidPolicyVersion",
    "PolicyVersionImmutable",
    "PolicyVersionCollision",
]
