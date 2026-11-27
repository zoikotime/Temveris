"""
Worker-context vocabularies — pure definitions, no I/O, no ORM.
================================================================

WORKER CONTEXT HISTORY foundation, under the owner ruling of this step.

TWO SEPARATE VOCABULARIES, DELIBERATELY NOT ONE
------------------------------------------------
``context_vocabulary`` says which vocabulary a stored value belongs to. Today
exactly one is declared: the PRODUCT employment type the platform already uses.

A legal worker category is NOT declared here, and must not be invented. D11 is
open: whether legal treatment depends on worker context, and what the
authoritative legal vocabulary would be, are Legal/Product decisions. If one is
later defined, it becomes a second vocabulary resolved by the same as-of
function — never a re-reading of the product value under a legal name. Nothing
in this package translates between vocabularies.

PROVENANCE — WHY ``CREATION_DEFAULT`` EXISTS
--------------------------------------------
Three of the five live worker-creation paths never set ``employment_type`` at
all, so the column default applies and the worker is Full-Time because a schema
said so, not because anybody decided:

    employees/router.py:279   invite               -> default
    commercial/identity.py:51 self-service signup  -> default
    org/provisioning_service.py:425 provisioning   -> default
    employees/router.py:393   create               -> explicit
    employees/router.py:512   create with password -> explicit

Recording a default as ``CAPTURED`` would assert a decision nobody made. It is
the same error Step 2 exists to prevent, where a synthesized closure must never
acquire the evidentiary weight of an observed departure. So a defaulted value is
recorded as ``CREATION_DEFAULT`` and is permanently distinguishable, and per the
owner ruling nothing may infer historical Full-Time status from it alone.

``ATTESTED`` IS DELIBERATELY ABSENT
-----------------------------------
H-1 is the approved treatment for unrecoverable history: it stays UNKNOWN. No
attestation mechanism is designed or implemented. Introducing the value here
would make the mechanism one commit away; it is omitted so that adding it
requires an owner decision, not an edit.
"""

from __future__ import annotations

import enum
from typing import Any, FrozenSet


class WorkerContextProvenance(str, enum.Enum):
    """How a recorded worker-context period came to exist."""

    CAPTURED = "CAPTURED"
    """An explicit administrative classification decision."""

    CORRECTION = "CORRECTION"
    """A correction of an earlier timeline. Appends a new version; the prior
    version stays byte-identical and readable."""

    CREATION_DEFAULT = "CREATION_DEFAULT"
    """The value that resulted from the column default at worker creation,
    nobody having chosen it. NOT evidence of a decision, and never evidence of
    historical classification before the capture date."""

    # ATTESTED is NOT defined. H-1 is the approved treatment for unrecoverable
    # history; adding attestation requires a separate owner decision.


#: Provenance values that represent a deliberate human decision. A defaulted
#: value is not one, which is the whole point of keeping it separate.
DELIBERATE_PROVENANCE: FrozenSet[WorkerContextProvenance] = frozenset({
    WorkerContextProvenance.CAPTURED,
    WorkerContextProvenance.CORRECTION,
})

#: The product employment-type vocabulary — the values ``User.employment_type``
#: already holds, carried verbatim. Not renamed, not normalised into a legal
#: category.
VOCABULARY_PRODUCT_EMPLOYMENT_TYPE: str = "product.employment_type"

#: Every vocabulary this package recognises. Exactly one, until Legal defines
#: another under D11. A value in an undeclared vocabulary is refused rather than
#: stored, so a typo cannot create a second silent vocabulary.
KNOWN_VOCABULARIES: FrozenSet[str] = frozenset({
    VOCABULARY_PRODUCT_EMPLOYMENT_TYPE,
})


def is_valid_provenance(value: Any) -> bool:
    """True only for one of the three approved provenance values. Exact match."""
    if isinstance(value, WorkerContextProvenance):
        return True
    return isinstance(value, str) and value in {p.value for p in WorkerContextProvenance}


def parse_provenance(value: Any) -> WorkerContextProvenance:
    """Coerce to a provenance value, raising on anything unapproved.

    Introducing a fourth value — ``ATTESTED`` in particular — is a governance
    decision, not a code change. This function is where that is enforced.
    """
    if isinstance(value, WorkerContextProvenance):
        return value
    if isinstance(value, str) and value in {p.value for p in WorkerContextProvenance}:
        return WorkerContextProvenance(value)
    approved = ", ".join(sorted(p.value for p in WorkerContextProvenance))
    raise ValueError(
        f"{value!r} is not an approved worker-context provenance. Approved: "
        f"{approved}. Adding a value requires an owner ruling, not a code change."
    )


def is_known_vocabulary(value: Any) -> bool:
    return isinstance(value, str) and value in KNOWN_VOCABULARIES


__all__ = [
    "WorkerContextProvenance",
    "DELIBERATE_PROVENANCE",
    "VOCABULARY_PRODUCT_EMPLOYMENT_TYPE",
    "KNOWN_VOCABULARIES",
    "is_valid_provenance",
    "parse_provenance",
    "is_known_vocabulary",
]
