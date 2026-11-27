"""
Rollout transition failures.
============================

STEP 8 of the owner-authorised Phase 2 foundation.

Every one of these refuses a transition. None of them is raised by the READ
path: reading a rollout state never raises, because a control that can throw
while being consulted is a control that can take a request down. Reads fail to
OFF instead.
"""

from __future__ import annotations


class RolloutError(Exception):
    """Base for rollout transition failures."""


class InvalidRolloutTransition(RolloutError):
    """The requested transition is not in the lifecycle.

    Most importantly ``OFF → ENABLED``, which does not exist: reaching ENABLED
    requires passing through SHADOW, and that is what makes the observation
    period unavoidable rather than advisory.
    """


class ObservationWindowNotElapsed(RolloutError):
    """SHADOW → ENABLED was requested before the minimum observation period.

    Carries how long is left so an operator is told when to come back, rather
    than being refused without a reason.
    """

    def __init__(self, message: str, *, days_remaining: float) -> None:
        super().__init__(message)
        self.days_remaining = days_remaining


class StaleRolloutVersion(RolloutError):
    """The transition was computed against a state that is no longer current.

    Two administrators acting at once must not have one silently overwrite the
    other — particularly when one of them is rolling back. The loser is told to
    re-read and decide again against what is now true.
    """


class UnknownRolloutControl(RolloutError):
    """A control key that was never declared.

    Refused rather than created on demand: a typo would otherwise produce a
    second, permanently-OFF control that looks real in an admin list.
    """


class RolloutOrganizationRequired(RolloutError):
    """A transition was requested without an organization.

    There is no global rollout switch, so there is no organization-less
    transition. See the package docstring.
    """


class EnablementGatesNotSatisfied(RolloutError):
    """SHADOW → ENABLED was requested while §44.3 gates remain unsatisfied.

    Owner ruling D6 makes the gates a machine-enforced precondition of
    promotion. This is separate from ``ObservationWindowNotElapsed`` on
    purpose: "wait longer" and "this Tenant is not configured or documented for
    live operation" are different problems with different remedies, and one
    error type for both would tell an operator to wait when waiting will not
    help.

    Carries every failing gate, so a blocked promotion is diagnosed once rather
    than one refusal at a time.
    """

    def __init__(self, message: str, *, gates=()) -> None:
        super().__init__(message)
        self.gates = tuple(gates)


__all__ = [
    "RolloutError",
    "InvalidRolloutTransition",
    "ObservationWindowNotElapsed",
    "StaleRolloutVersion",
    "UnknownRolloutControl",
    "RolloutOrganizationRequired",
    "EnablementGatesNotSatisfied",
]
