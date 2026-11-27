"""
The governed jurisdiction mapping (owner rulings B, B8, B9).
============================================================

Country classification is PLATFORM-GOVERNED and effective-dated. A Tenant's
applicable jurisdiction SET is established by the platform and correctable by
``{admin, hr}``. Both are append-only: corrections supersede, and historical
records are never mutated.

``OrgSettings.primary_country`` is NOT authoritative for ECO and is read by
nothing here.

An assignment recorded with no countries is a DECLARATION that none applies
(B9 -> NOT_APPLICABLE). No assignment at all is nobody having established the
answer (-> EVIDENCE_MISSING). ``JurisdictionResolution.is_assigned`` is the
distinction, and it is the first thing a caller branches on.
"""

from .models import (
    JurisdictionClassification, TenantJurisdictionAssignment,
    TenantJurisdictionAssignmentCountry,
)
from .outcomes import (
    ISO_ALPHA2, SPEC_NAMED_CONSULTATION_COUNTRIES, AssignmentSource,
    ClassificationNotGoverned, InvalidCountryCode, JurisdictionError,
    JurisdictionResolution, UnauthorizedAssignment,
)
from .service import (
    assign_platform, classification_as_of, correct_assignment,
    record_classification, resolve,
)

__all__ = [
    "AssignmentSource",
    "ClassificationNotGoverned",
    "ISO_ALPHA2",
    "InvalidCountryCode",
    "JurisdictionClassification",
    "JurisdictionError",
    "JurisdictionResolution",
    "SPEC_NAMED_CONSULTATION_COUNTRIES",
    "TenantJurisdictionAssignment",
    "TenantJurisdictionAssignmentCountry",
    "UnauthorizedAssignment",
    "assign_platform",
    "classification_as_of",
    "correct_assignment",
    "record_classification",
    "resolve",
]
