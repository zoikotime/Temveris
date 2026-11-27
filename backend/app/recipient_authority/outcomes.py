"""
The RECIPIENT-AUTHORITY vocabulary.
===================================

ZT-SPEC-ECO-002 v2.0 §33.1, owner ruling D-3.

Separate from ``app.alert.outcomes`` on purpose. ALERT's ``NotificationAudience``
says who an already-authorized notification ARTIFACT is rendered for; this says
who the organisation's authoritative recipients ARE. The two vocabularies agree
on the WORKER value today, and a test pins that agreement, but they answer
different questions and R3 forbids substituting one surface for the other on
the strength of a coincidence.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


class EcoAudience(str, enum.Enum):
    """The four §33.1 audiences. A CLOSED vocabulary.

    The specification's matrix names exactly these four and nothing else. Owner
    ruling D-3 replaced the earlier A/B/C recipient options with them, so a
    fifth value is not an additive extension here — it is a different matrix,
    and it needs a spec amendment rather than an enum member.
    """

    WORKER = "WORKER"
    PRIMARY_ADMIN = "PRIMARY_ADMIN"
    DIRECT_MANAGER = "DIRECT_MANAGER"
    HR_ADMIN = "HR_ADMIN"


#: Matrix order. §33.1 lists the audiences left to right in this order and the
#: snapshot preserves it, so two resolutions of the same tenant compare
#: byte-for-byte without anybody sorting them at the call site.
AUDIENCE_ORDER: Tuple[EcoAudience, ...] = (
    EcoAudience.WORKER,
    EcoAudience.PRIMARY_ADMIN,
    EcoAudience.DIRECT_MANAGER,
    EcoAudience.HR_ADMIN,
)

#: The audiences an organisation may DESIGNATE a holder for.
#:
#: WORKER is absent because the Worker is the event's subject, not a
#: configurable party: a designation row for WORKER would let a tenant redirect
#: a Worker's own notification to somebody else, which is a disclosure, not a
#: configuration.
#:
#: DIRECT_MANAGER is absent because a manager is a per-Worker relationship and
#: this table is organisation-scoped. An org-wide "the direct manager" row
#: would be an invented hierarchy — exactly what §6 of the authorization and
#: ruling D-3 forbid. See ``BASIS_NO_CANONICAL_MANAGER``.
DESIGNATABLE_AUDIENCES = frozenset({
    EcoAudience.PRIMARY_ADMIN.value,
    EcoAudience.HR_ADMIN.value,
})


class AudienceState(str, enum.Enum):
    """Whether an audience resolved, and never merely a truthy user list."""

    RESOLVED = "RESOLVED"
    """At least one authoritative identity was found."""

    UNRESOLVED = "UNRESOLVED"
    """No identity. The reason is carried in ``basis`` and is never a guess."""


# ── why an audience resolved, or did not ────────────────────────────────────
# Recorded in the snapshot so a historical read can answer "on what authority?"
# without re-resolving anything.

BASIS_EVENT_SUBJECT = "EVENT_SUBJECT"
"""WORKER: the Worker the event is about. Not designatable, never inferred."""

BASIS_DESIGNATION = "ECO_RECIPIENT_DESIGNATION"
"""A row in ``eco_recipient_designation``. The only designation authority."""

BASIS_NOT_DESIGNATED = "NOT_DESIGNATED"
"""§33.1 "If configured", and it is not. A legitimate steady state, NOT a
fault: the audience is simply absent for this tenant."""

BASIS_NO_CANONICAL_MANAGER = "NO_CANONICAL_MANAGER_RELATIONSHIP"
"""DIRECT_MANAGER, permanently in this repository as it stands.

There is no reporting line anywhere in the schema: no ``manager_id``, no
``reports_to``, no reporting-line table. ``teams.head_user_id`` exists, but
reaching it means joining a Worker's free-text ``User.team`` string to
``Team.name`` — which is inference from team membership, and §6 forbids exactly
that. So the audience is UNRESOLVED and says so, rather than PRIMARY_ADMIN
being quietly substituted for it."""


class CrossTenantRecipient(Exception):
    """A designation names a user who belongs to a different organisation.

    Raised, never returned. An absent audience is an ordinary outcome; a
    recipient authority that crosses a tenant boundary is a correctness
    violation, and resolving it to "unresolved" would let the caller proceed as
    though the tenant were merely unconfigured.
    """


class RecipientAuthorityUnavailable(Exception):
    """The resolver was given something it cannot resolve against — a missing
    organisation or worker identity. Explicit, because a silently empty
    resolution is indistinguishable from a correctly unconfigured tenant."""


@dataclass(frozen=True)
class AudienceResolution:
    """One audience's answer. Always present for all four, even when empty."""

    audience: EcoAudience
    state: AudienceState
    user_ids: Tuple[int, ...]
    basis: str

    def as_snapshot(self) -> Dict[str, object]:
        return {
            "audience": self.audience.value,
            "state": self.state.value,
            "user_ids": list(self.user_ids),
            "basis": self.basis,
        }


@dataclass(frozen=True)
class ResolvedRecipients:
    """The full §33.1 answer for one organisation and one Worker."""

    organization_id: int
    worker_user_id: int
    audiences: Tuple[AudienceResolution, ...]

    def by_audience(self, audience: EcoAudience) -> AudienceResolution:
        for entry in self.audiences:
            if entry.audience is audience:
                return entry
        raise KeyError(audience)  # pragma: no cover - AUDIENCE_ORDER is total

    @property
    def resolved_user_ids(self) -> Tuple[int, ...]:
        """Every distinct identity named, in matrix order then id order.

        De-duplicated ACROSS audiences: one user who holds two audiences is one
        person. The per-audience entries keep the full attribution, so this
        loses nothing — it answers "who" while ``audiences`` answers "as what".
        """
        out: List[int] = []
        seen = set()
        for entry in self.audiences:
            for uid in entry.user_ids:
                if uid not in seen:
                    seen.add(uid)
                    out.append(uid)
        return tuple(out)


__all__ = [
    "AUDIENCE_ORDER",
    "BASIS_DESIGNATION",
    "BASIS_EVENT_SUBJECT",
    "BASIS_NOT_DESIGNATED",
    "BASIS_NO_CANONICAL_MANAGER",
    "AudienceResolution",
    "AudienceState",
    "CrossTenantRecipient",
    "DESIGNATABLE_AUDIENCES",
    "EcoAudience",
    "RecipientAuthorityUnavailable",
    "ResolvedRecipients",
]
