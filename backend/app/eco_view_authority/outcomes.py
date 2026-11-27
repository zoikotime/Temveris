"""
The typed vocabulary of one ECO view-authority decision.
========================================================

A VALUE, not an exception — the precedent ``eval_day``, ``work_ledger_producer``,
``eval_event`` and ``alert`` all set. The caller cannot accidentally read a
denial as a grant, because the authorized outcomes and the denial are different
members of one enum and the dataclass refuses to carry a reason without one.

§32 warns that enum values are a product surface that cannot be corrected later.
Every value here is written once and never derived from a variable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class ViewAuthorityOutcome(str, enum.Enum):
    """The result of asking whether one caller may read one Worker's ECO records."""

    WORKER_SELF = "WORKER_SELF"
    """The caller is the Worker the records belong to."""

    TENANT_ADMINISTRATOR = "TENANT_ADMINISTRATOR"
    """The caller holds this tenant's explicitly persisted ECO view grant."""

    DENIED = "DENIED"
    """No authority. ``reason`` says which condition failed; nothing is read."""


class DenialReason(str, enum.Enum):
    """Why authority was refused. Never collapsed into one generic reason.

    A denial that cannot say which condition failed is indistinguishable from a
    bug, and an operator debugging a legitimate access problem would have to
    guess.
    """

    NO_TENANT_CONTEXT = "NO_TENANT_CONTEXT"
    """The caller has no ``organization_id``. This INCLUDES ``super_admin``,
    whose tenant id is cleared on login: platform-owner status is not
    tenant-administrator status, and a caller belonging to no tenant cannot
    hold a tenant-scoped capability."""

    CALLER_UNKNOWN = "CALLER_UNKNOWN"
    """No caller identity was supplied, or it carries no id."""

    TARGET_UNKNOWN = "TARGET_UNKNOWN"
    """No user with that id exists. Fail closed rather than treating an absent
    Worker as trivially readable."""

    CROSS_TENANT = "CROSS_TENANT"
    """The target Worker belongs to a different organization. Denied even when
    the record identifiers are known — knowing an id is not authority."""

    NO_GRANT = "NO_GRANT"
    """The caller is not the Worker and holds no persisted ECO view grant for
    this organization. Absence is a denial, never a fallback to a role."""

    GRANT_AMBIGUOUS = "GRANT_AMBIGUOUS"
    """More than one grant row matched one (organization, user). The unique
    constraint makes this unreachable through the ORM; it is still refused
    rather than resolved by picking one, because choosing among contradictory
    authority records is a policy decision nobody ruled."""

    GRANT_MALFORMED = "GRANT_MALFORMED"
    """A matched grant row failed re-verification against the tenant and user it
    was selected for. Defensive: a row that does not describe what it was
    fetched for is not evidence of anything."""


@dataclass(frozen=True)
class ViewAuthorityDecision:
    """One decision, in a form a caller cannot misread as success.

    There is no ``allowed: bool`` field to check instead of the outcome, and no
    field that carries a record — this type answers "may they read", never
    "here is the data".
    """

    outcome: ViewAuthorityOutcome
    caller_id: Optional[int] = None
    target_user_id: Optional[int] = None
    #: The tenant the decision was made INSIDE. Derived from the caller, never
    #: from a request parameter, and left None on a denial that never
    #: established one.
    organization_id: Optional[int] = None
    reason: Optional[DenialReason] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome is ViewAuthorityOutcome.DENIED:
            if self.reason is None:
                raise ValueError("a DENIED decision must carry a reason")
            return
        if self.reason is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a denial reason")
        if self.organization_id is None:
            raise ValueError(
                f"{self.outcome.value} must name the tenant it was made inside")

    @property
    def is_authorized(self) -> bool:
        """True only for a granted outcome. The one place truthiness is defined."""
        return self.outcome is not ViewAuthorityOutcome.DENIED


__all__ = [
    "ViewAuthorityOutcome",
    "DenialReason",
    "ViewAuthorityDecision",
]
