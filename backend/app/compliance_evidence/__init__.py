"""
ECO compliance evidence (owner rulings D2, D3, D4, D7).
=======================================================

Three dedicated append-only tables: DPIA evidence, the employee-representative
consultation attestation, and the Tenant-level worker-notice confirmation.

Temveris RECORDS this evidence. It does not assess its adequacy (section
44.3). There is no Worker-level record and no Worker acknowledgement (D4).
Corrections supersede; nothing is ever edited in place.
"""

from .models import (
    EcoConsultationAttestation, EcoDpiaEvidence, EcoWorkerNoticeConfirmation,
)
from .outcomes import (
    RETENTION_POLICY, ComplianceEvidenceError, EvidenceImmutable,
    EvidenceStatus, UnauthorizedEvidenceActor,
)
from .service import (
    consultation_in_force, dpia_in_force, record_consultation_attestation,
    record_dpia_evidence, record_worker_notice, worker_notice_in_force,
)

__all__ = [
    "ComplianceEvidenceError",
    "EcoConsultationAttestation",
    "EcoDpiaEvidence",
    "EcoWorkerNoticeConfirmation",
    "EvidenceImmutable",
    "EvidenceStatus",
    "RETENTION_POLICY",
    "UnauthorizedEvidenceActor",
    "consultation_in_force",
    "dpia_in_force",
    "record_consultation_attestation",
    "record_dpia_evidence",
    "record_worker_notice",
    "worker_notice_in_force",
]
