"""
Closed vocabulary for the governed jurisdiction mapping.
========================================================

Owner ruling B (D1 mechanism B), B8 and B9.

TWO DIFFERENT FACTS, DELIBERATELY SEPARATED
-------------------------------------------
* **Classification** — is a country EU, is it UK, does it require employee-
  representative consultation. PLATFORM-GOVERNED. A Tenant may never redefine
  it, because it is a statement of law rather than a statement about the
  Tenant.
* **Assignment** — which jurisdictions apply to one Tenant. Established by the
  platform, correctable by ``{admin, hr}`` (B8: C).

Conflating them would let a Tenant declare itself outside the EU, which is the
one thing the classification rule exists to prevent.

EMPTY IS NOT MISSING (B9)
-------------------------
An assignment recorded with NO countries is a Tenant DECLARING that no governed
jurisdiction applies, and jurisdiction-dependent gates read it as
``NOT_APPLICABLE`` — a passing state. No assignment at all is
``EVIDENCE_MISSING``. The two are represented differently by construction: an
empty declaration IS a row, and a missing one is the absence of any row. They
cannot be collapsed by accident.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

#: ISO 3166-1 alpha-2, and nothing else (owner ruling B.1).
ISO_ALPHA2 = re.compile(r"^[A-Z]{2}$")

#: The jurisdictions §40.4 NAMES as requiring employee-representative
#: consultation. Germany, the Netherlands, Austria and France, and NOT one
#: country more: §40.4 says "and elsewhere", which is an instruction that the
#: mapping must be EXTENSIBLE, never a licence to infer a list.
SPEC_NAMED_CONSULTATION_COUNTRIES = ("AT", "DE", "FR", "NL")

#: The date the platform's initial classification is in force FROM.
#:
#: Deliberately far enough back to cover every date this system can be asked
#: about. It is not a claim about when Austria joined the EU -- inventing
#: membership history would be inventing exactly what B forbids. It says: for
#: every date Temveris evaluates, this is the platform's classification.
#: A real change of status is recorded as a LATER row that supersedes.
SPEC_CLASSIFICATION_EFFECTIVE_FROM = date(1970, 1, 1)

#: The governed mapping's initial content (owner ruling B).
#:
#: (country_code, is_eu, is_uk, requires_consultation)
#:
#: EXACTLY the four countries §40.4 names, and not one more. GB is absent on
#: purpose: §40.4 does not name it, and seeding it would be inventing a row
#: the owner did not authorise. A Tenant assigned an unseeded country fails
#: CLOSED -- the platform has no classification in force for it, which is
#: "applicability unresolved", never "does not apply".
SPEC_CLASSIFICATIONS = (
    ("AT", True, False, True),
    ("DE", True, False, True),
    ("FR", True, False, True),
    ("NL", True, False, True),
)


class AssignmentSource(str, enum.Enum):
    """Who recorded a Tenant's jurisdiction assignment (B8: C)."""

    PLATFORM = "PLATFORM"
    """Temveris established it. The initial assignment."""

    TENANT = "TENANT"
    """An authorized Tenant actor corrected it. Carries actor and role."""


class JurisdictionError(Exception):
    """Base for refusals from this package."""


class InvalidCountryCode(JurisdictionError):
    """Not ISO 3166-1 alpha-2."""


class ClassificationNotGoverned(JurisdictionError):
    """A Tenant actor attempted to set a country's legal classification.

    B8 is explicit: "Country legal classification remains platform-controlled."
    A Tenant may correct WHICH jurisdictions apply to it; it may never change
    what a jurisdiction legally IS.
    """


class UnauthorizedAssignment(JurisdictionError):
    """The actor may not record or correct a Tenant assignment."""


@dataclass(frozen=True)
class JurisdictionResolution:
    """What applies to ONE Tenant as of ONE date. Immutable.

    ``is_assigned`` is the B9 distinction and is the first thing any caller
    must branch on: False means nobody has established the Tenant's
    jurisdictions, which is not the same as establishing that none apply.
    """

    organization_id: int
    as_of: date
    is_assigned: bool
    countries: Sequence[str] = field(default_factory=tuple)
    assignment_id: Optional[int] = None
    source: Optional[str] = None

    #: Resolved from the classification in force on ``as_of`` — never today's.
    is_eu: bool = False
    is_uk: bool = False
    requires_consultation: bool = False

    #: Assigned countries the platform has NO classification in force for.
    #:
    #: These are the reason the three booleans above cannot be trusted. An
    #: unclassified country is a question the platform has not answered, and
    #: the answer might have been "EU" — so a caller must fail closed rather
    #: than read False as "no". Empty is the normal case.
    unclassified: Sequence[str] = field(default_factory=tuple)

    @property
    def is_declared_empty(self) -> bool:
        """An assignment exists and names no country (B9 → NOT_APPLICABLE)."""
        return self.is_assigned and not self.countries

    @property
    def applicability_resolved(self) -> bool:
        """Whether the flags may be relied on at all.

        False when any assigned country is unclassified. This is NOT the same
        as every flag being False: a country explicitly classified
        ``is_eu=False, is_uk=False, requires_consultation=False`` IS resolved,
        and does reach NOT_APPLICABLE. Silence does not.
        """
        return not self.unclassified

    @property
    def dpia_substantive_applies(self) -> bool:
        """§40.2: the substantive requirement is EU (owner ruling C: B)."""
        return self.is_eu

    @property
    def dpia_gate_applies(self) -> bool:
        """§44.3: the rollout evidence gate is EU/UK (owner ruling C: B)."""
        return self.is_eu or self.is_uk


__all__ = [
    "ISO_ALPHA2",
    "SPEC_CLASSIFICATIONS",
    "SPEC_CLASSIFICATION_EFFECTIVE_FROM",
    "SPEC_NAMED_CONSULTATION_COUNTRIES",
    "AssignmentSource",
    "ClassificationNotGoverned",
    "InvalidCountryCode",
    "JurisdictionError",
    "JurisdictionResolution",
    "UnauthorizedAssignment",
]
