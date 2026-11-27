"""
Closed vocabulary for ECO compliance evidence (D2, D3, D4, D7).
===============================================================

THREE RECORDS, ONE SHAPE
------------------------
DPIA evidence, the consultation attestation and the worker-notice confirmation
are different facts with different applicability, but they share one contract
the owner ruled three times over: append-only, corrections supersede, actor and
role recorded, effective-dated, retained for the lifetime of the Tenant unless
superseded (ruling A: C).

WHAT TEMVERIS DOES NOT DO
--------------------------
It does not assess adequacy. Section 44.3 is explicit for the consultation
attestation -- "Temveris records the attestation; it does not assess its
adequacy" -- and the same posture governs the other two. There is no APPROVED
or REJECTED status here, because a status vocabulary that could express a
judgement would invite one.
"""

from __future__ import annotations

import enum


class EvidenceStatus(str, enum.Enum):
    """What the Tenant asserted. Recorded, never adjudicated."""

    COMPLETED = "COMPLETED"
    """The obligation was discharged. For DPIA: the assessment was completed.
    For consultation: consultation took place. For worker notice: Workers were
    informed."""

    NOT_REQUIRED = "NOT_REQUIRED"
    """The Tenant asserts the obligation does not apply to it. Distinct from
    a jurisdiction-derived NOT_APPLICABLE, which is computed from the governed
    mapping rather than asserted -- keeping the two apart is what stops an
    assertion from overriding the mapping."""


#: Retention, per owner ruling A: C. Recorded as a named constant rather than a
#: duration because there is no duration: section 37 defines four retention
#: classes and none covers compliance evidence, so inventing a number here
#: would be inventing the policy the ruling deliberately did not create.
RETENTION_POLICY = "TENANT_LIFETIME_UNLESS_SUPERSEDED"


class ComplianceEvidenceError(Exception):
    """Base for refusals from this package."""


class UnauthorizedEvidenceActor(ComplianceEvidenceError):
    """The actor may not record this evidence.

    Owner ruling D fixed ONE authorization vocabulary for ECO governance acts:
    ``{admin, hr}``. Recording compliance evidence is such an act.
    """


class EvidenceImmutable(ComplianceEvidenceError):
    """An attempt to rewrite recorded evidence.

    D2, D3 and D4 all say the same thing: append-only, corrections supersede,
    no UPDATE-in-place of substantive evidence.
    """


__all__ = [
    "ComplianceEvidenceError",
    "EvidenceImmutable",
    "EvidenceStatus",
    "RETENTION_POLICY",
    "UnauthorizedEvidenceActor",
]
