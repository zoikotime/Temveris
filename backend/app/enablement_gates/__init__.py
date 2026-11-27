"""
The §44.3 enablement-gate assessment (ZT-SPEC-ECO-002 v2.0).
============================================================

Nine gates, assessed per Tenant, read-only.

    assess_enablement_gates(db, organization_id=..., shadow=...) -> EnablementReadiness

It answers "does this Tenant have evidence satisfying every required §44.3
gate?" and nothing else. It writes nothing and transitions no rollout. Under
owner ruling D6 ``rollout.transition`` composes it and refuses to promote
SHADOW → ENABLED against a NOT_READY verdict — the refusal happens there, in
the component that owns the state.

A gate is satisfied by TENANT-SPECIFIC evidence, never by the capability being
implemented. The three compliance gates read real per-Tenant records; a Tenant
with none still reports EVIDENCE_MISSING, and a Tenant with no jurisdiction
assignment reports EVIDENCE_MISSING rather than NOT_APPLICABLE, because
silence is not an exemption.
"""

from .outcomes import (
    GATE_ORDER, PASSING_STATUSES, EnablementReadiness, GateId, GateResult,
    GateStatus,
)
from .ports import ShadowObservation
from .service import assess_enablement_gates

__all__ = [
    "EnablementReadiness",
    "GATE_ORDER",
    "GateId",
    "GateResult",
    "GateStatus",
    "PASSING_STATUSES",
    "ShadowObservation",
    "assess_enablement_gates",
]
