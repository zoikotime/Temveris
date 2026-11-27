"""
Closed vocabulary for the §44.3 enablement-gate assessment.
===========================================================

ZT-SPEC-ECO-002 v2.0 §44.3, transcribed. The nine gates, their blocking
column, and nothing invented:

    GATE                                  REQUIREMENT                                       BLOCKING?
    Workweek and timezone configured      Tenant workweek start, timezone and shift         Yes
                                          attribution rule set
    Requirement fallback configured       Tenant fallback policy exists at rank 6 (§10)     Yes
    Netting decision made explicitly      Not accepted as a default (§15)                   Yes
    Break treatment configured            BRK_M and BRK_R treatment set explicitly          Yes
    At least one administrative recipient §30.1                                             Yes
    Shadow period completed               Two complete workweeks (§44.2)                    Yes
    DPIA completed                        Required for EU/UK Tenants before P1              Yes for EU/UK
    Employee-representative consultation  Attestation recorded where the jurisdiction       Yes where
                                          requires it (§40.4)                               applicable
    Worker notice issued                  Tenant confirms Workers have been informed        Yes
                                          the control is active

WHAT THIS ASSESSMENT ANSWERS
----------------------------
"Does this Tenant have evidence satisfying every required §44.3 gate?"

It does NOT answer "should this Tenant be enabled?", and it is not an
enablement mechanism. Nothing here transitions a rollout, and the service that
produces these results writes nothing at all.

ONE THRESHOLD, NOT TWO
----------------------
§44.3 is titled "Enablement gates" and defines ONE bar. Shadow is not
enablement — §44.2 says a Tenant "enabling this control for the first time runs
it in shadow", so the shadow period is a PREREQUISITE the gate list already
contains, not a separate threshold with its own subset of gates. Splitting the
nine into a SHADOW set and an ENABLED set would be inventing a contract the
specification does not state, so the aggregate has one verdict.

THE FOUR QUESTIONS, KEPT APART
------------------------------
A readiness assessment is worthless if it conflates them:

1. does the CAPABILITY exist in this repository;
2. does TENANT-SPECIFIC evidence exist;
3. is that evidence VALID;
4. is the GATE satisfied.

Only (2), (3) and (4) are a gate result. "The feature is built" is never an
answer, and ``GateStatus`` has no value that could express it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence


class GateId(str, enum.Enum):
    """The nine §44.3 gates. Closed — a tenth is a specification change."""

    WORKWEEK_TIMEZONE_ATTRIBUTION = "WORKWEEK_TIMEZONE_ATTRIBUTION"
    """Tenant workweek start, timezone and shift attribution rule set."""

    REQUIREMENT_FALLBACK = "REQUIREMENT_FALLBACK"
    """Tenant fallback policy exists at rank 6 (§10)."""

    NETTING_DECISION = "NETTING_DECISION"
    """Netting decision made explicitly; not accepted as a default (§15)."""

    BREAK_TREATMENT = "BREAK_TREATMENT"
    """BRK_M and BRK_R treatment set explicitly."""

    ADMINISTRATIVE_RECIPIENT = "ADMINISTRATIVE_RECIPIENT"
    """At least one administrative recipient (§30.1)."""

    SHADOW_PERIOD = "SHADOW_PERIOD"
    """Two complete workweeks completed in shadow (§44.2)."""

    DPIA = "DPIA"
    """DPIA completed. Required for EU/UK Tenants before P1."""

    CONSULTATION_ATTESTATION = "CONSULTATION_ATTESTATION"
    """Employee-representative consultation attestation recorded where the
    jurisdiction requires it (§40.4)."""

    WORKER_NOTICE = "WORKER_NOTICE"
    """Tenant confirms Workers have been informed the control is active."""


#: The gate order §44.3's own table uses. Reports read in specification order
#: so a reader can hold the table and the output side by side.
GATE_ORDER: Sequence[GateId] = tuple(GateId)


class GateStatus(str, enum.Enum):
    """Closed. Every value is one the specification licenses."""

    SATISFIED = "SATISFIED"
    """Tenant-specific evidence exists and is valid."""

    UNSATISFIED = "UNSATISFIED"
    """Evidence exists and was read, and it does not meet the requirement."""

    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    """No tenant-specific evidence exists to assess. Distinct from
    UNSATISFIED on purpose: "nobody has recorded this" and "this was recorded
    and falls short" are different facts, and collapsing them would hide which
    one a Tenant is looking at."""

    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    """Evidence exists but the authoritative source defines it as unusable —
    a corrupt rollout state, a shadow control with no entry timestamp, a
    requirement candidate of zero minutes. Only conditions an existing
    component already rejects are reported this way."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    """The specification's own "Yes for EU/UK" and "Yes where applicable"
    columns. Reachable ONLY where §44.3 makes a gate conditional, and ONLY
    when non-applicability can be established from an authoritative source.
    Inability to establish applicability is EVIDENCE_MISSING, never this."""


#: The statuses that let a blocking gate pass. Named once so the aggregate
#: cannot drift from the per-gate meaning.
PASSING_STATUSES = frozenset({GateStatus.SATISFIED, GateStatus.NOT_APPLICABLE})


@dataclass(frozen=True)
class GateResult:
    """One gate, assessed for one Tenant. Immutable, and PII-free.

    ``evidence`` carries identifiers and values an operator needs to verify the
    finding — a config version key, a designation count, a rollout timestamp —
    and never a Worker's name, address or contact detail.
    """

    gate: GateId
    status: GateStatus
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    missing_evidence: Sequence[str] = field(default_factory=tuple)
    blocking: bool = True

    @property
    def passes(self) -> bool:
        return self.status in PASSING_STATUSES

    def as_row(self) -> Mapping[str, Any]:
        return {
            "gate": self.gate.value,
            "status": self.status.value,
            "blocking": self.blocking,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "missing_evidence": list(self.missing_evidence),
        }


@dataclass(frozen=True)
class EnablementReadiness:
    """The aggregate §44.3 assessment for ONE Tenant.

    ``verdict`` is ``ENABLEMENT_READY`` or ``ENABLEMENT_NOT_READY`` and nothing
    else. It is a statement about EVIDENCE, not an authorization: a Tenant that
    reads READY has satisfied the nine gates and still requires whatever
    human decision the operator's own process demands.
    """

    organization_id: int
    evaluated_at: datetime
    verdict: str
    gates: Sequence[GateResult] = field(default_factory=tuple)

    @property
    def is_ready(self) -> bool:
        return self.verdict == "ENABLEMENT_READY"

    @property
    def blocking_gates(self) -> Sequence[GateResult]:
        """The blocking gates that do not pass, in specification order."""
        return tuple(g for g in self.gates if g.blocking and not g.passes)

    def gate(self, gate_id: GateId) -> GateResult:
        return next(g for g in self.gates if g.gate is gate_id)

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "organization_id": self.organization_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "verdict": self.verdict,
            "blocking_gates_outstanding": len(self.blocking_gates),
            "gates": [g.as_row() for g in self.gates],
        }


__all__ = [
    "EnablementReadiness",
    "GATE_ORDER",
    "GateId",
    "GateResult",
    "GateStatus",
    "PASSING_STATUSES",
]
