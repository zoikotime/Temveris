"""
Intraday leave failures.
========================

STEP 7 of the owner-authorised Phase 2 foundation.

Each of these refuses to record something rather than recording an approximation
of it. That is the whole disposition of this module: a leave interval nobody
stated is not a leave interval this layer will invent.
"""

from __future__ import annotations


class LeaveIntervalError(Exception):
    """Base for intraday leave failures."""


class LeaveIntervalIntegrityFault(LeaveIntervalError):
    """The offered intervals are inadmissible as evidence.

    Carries the specific violations rather than a bare message, so an operator
    is told which interval is wrong and why. The set is refused whole: a
    partially-admissible set is not repaired, clamped or trimmed, because a
    Worker's leave record silently adjusted to fit is worse than one that was
    rejected loudly.
    """

    def __init__(self, message: str, violations=()) -> None:
        super().__init__(message)
        self.violations = tuple(violations)


class LeaveRequestNotTenanted(LeaveIntervalError):
    """The parent leave request carries no organization.

    ``LeaveRequest.organization_id`` is nullable in the existing schema, so a
    request can exist with no tenant. Intraday intervals refuse to inherit that:
    an interval whose organization is unknown cannot be isolated, filtered or
    audited by tenant. Fixing the parent request is an operational action, not
    something this layer guesses at.
    """


class LeaveIntervalOwnershipMismatch(LeaveIntervalError):
    """The stated Worker or organization does not match the parent request.

    Recording intervals against a mismatched identity would attach one Worker's
    leave to another's record.
    """


class StaleLeaveIntervalVersion(LeaveIntervalError):
    """A correction was computed against a version that is no longer current.

    Two administrators correcting the same request concurrently must not have
    their edits interleaved: the later writer is rejected and re-runs against
    what is now current. Merging two independently-computed interval sets would
    produce a third that neither administrator authored.
    """


class LeaveIntervalImmutable(LeaveIntervalError):
    """A recorded interval or set would be altered or removed.

    Intraday leave is append-only. A correction records a NEW set version; the
    prior version stays readable so the record as it stood on any past date can
    always be reproduced.
    """


__all__ = [
    "LeaveIntervalError",
    "LeaveIntervalIntegrityFault",
    "LeaveRequestNotTenanted",
    "LeaveIntervalOwnershipMismatch",
    "StaleLeaveIntervalVersion",
    "LeaveIntervalImmutable",
]
