"""
The configuration-version seam between Step 5 and Step 6.
=========================================================

STEP 5 of the owner-authorised Phase 2 foundation.

WHY A PORT AND NOT A TABLE
--------------------------
When Step 5 was written, Step 6 (immutable, effective-dated configuration) was
not yet authorised and the versioned store did not exist. Rather than stop,
Step 5 defined the *contract* the resolver needs and resolved against it,
leaving the storage decision where the owner had put it.

That has proved to be more than a workaround. Step 6 now supplies two
implementations — an in-memory reference and a database-backed adapter — and
because the contract was fixed first, both are held to the same suite rather
than each being tested against whatever it happens to do. The resolver itself
still requires no schema change and imports nothing from the storage layer.

A consequence worth stating plainly: a caller with no captured configuration
receives ``NO_VERSION`` for every date. That is the correct fail-closed answer,
not a gap — evaluation must not proceed on configuration nobody has captured.

WHAT AN IMPLEMENTATION MUST GUARANTEE
-------------------------------------
* ``version_as_of`` returns the immutable version effective on the date, or
  ``None`` when the date predates the first captured version. It MUST NOT fall
  back to live configuration (Gate 7 historical floor / correction C-06).
* ``requirement_candidates`` returns candidates for ONE configuration version.
  It must not blend tiers across versions, and must not consult live settings.
* Both must be deterministic: same inputs, same output, forever (INV-8).

ON TIER VOCABULARY — AN OPEN GOVERNANCE ITEM
--------------------------------------------
``RequirementCandidate.tier`` is a provider-declared string. The resolver does
NOT hard-code §10.1's six tokens (``WORKER_OVERRIDE``, ``SHIFT``, ``SCHEDULE``,
``EMPLOYMENT_POLICY``, ``TENANT_TYPE_DEFAULT``, ``TENANT_FALLBACK``).

That is deliberate. Two of those tokens contain "TENANT", and under owner
ruling D-09 Option C technical identifiers, field names and enum values use the
``organization_id`` vocabulary while ``Tenant`` / ``Worker`` remain
Worker-facing copy. Whether these provenance tokens are identifiers (and so
become ORG-prefixed) or specification labels carried verbatim is a naming
decision for the §4.1 amendment and the Cross-Asset Consistency Sheet — neither
of which has been issued. Hard-coding either choice here would pre-empt it, and
§32 warns enum values cannot be corrected later without a breaking change.

The resolver therefore enforces the *precedence rule* (first rank wins, lower
ranks not consulted, never blended) without owning the *names*. See section L of
the Step 5 report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class ConfigVersionRef:
    """An immutable configuration version, identified and effective-dated."""

    config_version_id: str
    effective_from: date


@dataclass(frozen=True)
class RequirementCandidate:
    """One tier's answer to "how many minutes does this Worker owe on this date?".

    ``minutes`` is ``None`` when the tier is configured but supplies no value —
    which is different from supplying an invalid one. The resolver treats the
    first case as "this rank did not answer, consult the next" and the second
    as a recorded configuration defect.
    """

    tier: str
    rank: int
    minutes: Optional[int] = None
    source_ref: Optional[str] = None


@runtime_checkable
class ConfigVersionProvider(Protocol):
    """The Step 6 seam. Implementations must satisfy the guarantees above."""

    def version_as_of(
        self, *, organization_id: int, as_of: date
    ) -> Optional[ConfigVersionRef]:
        """The immutable configuration version effective on ``as_of``.

        Returns ``None`` when ``as_of`` predates the first captured version.
        MUST NOT return live configuration in that case.
        """
        ...

    def requirement_candidates(
        self,
        *,
        organization_id: int,
        user_id: int,
        as_of: date,
        version: ConfigVersionRef,
    ) -> Sequence[RequirementCandidate]:
        """Candidates from every precedence tier, for this version only.

        Order is not relied upon: the resolver sorts by ``rank`` so a provider
        cannot change the outcome by changing its return order (INV-8).
        """
        ...


__all__ = [
    "ConfigVersionRef",
    "RequirementCandidate",
    "ConfigVersionProvider",
]
