"""
Temveris — VIEW-AUTHORITY: who may READ a tenant's ECO records.
================================================================

ZT-SPEC-ECO-002 v2.0. Owner ruling B2, Option B, for GV-17 (§24.7).

STATUS: IMPLEMENTED, NOT WIRED, NOTHING EXPOSED. No route, scheduler or
service consults it. The table has zero rows.

THE PUBLIC ENTRY POINT

    require_eco_view_authority(db, caller=..., target_user_id=...)

WHY IT EXISTS
-------------
§24.7 requires a deferred day to be "visible to both Worker and administrator
so that a week which appears artificially low is explainable". The ALERT audit
established that this is discharged by the WF-ECO-03 / WF-ECO-04 explanation
surfaces (§28.1, §29) rather than by a notification — §33.1's matrix has no
deferral row and §34 separates the explanation surface from delivery.

Building those surfaces needs an answer to a question the repository could not
answer: **who is the "administrator"?** There is no capability, permission,
scope or ACL model anywhere in 59 ORM models; authorization is hand-rolled per
router against a nullable free-text ``User.role``. The owner ruled that ECO
must not adopt that, and authorized this primitive instead.

VIEW AUTHORITY IS NOT RECIPIENT AUTHORITY
------------------------------------------
    VIEW-AUTHORITY        may this caller READ this record        <- this package
    RECIPIENT-AUTHORITY   who should be TOLD about this record    <- app.recipient_authority

The second was GV-07's blocker and was satisfied under Stage 5 (ruling D-3).
That changes nothing here: the two packages share no code, and nothing in this
one resolves a recipient, creates a designated reviewer, or implies delivery.
GV-07 now waits on ALERT-DELIVERY alone.

WHAT IS DELIBERATELY ABSENT
---------------------------
* **No HTTP surface.** No route, no FastAPI dependency wrapper, no schema. The
  owner authorized the primitive, not the endpoints.
* **No ECO read API.** This says who MAY read; it returns no records.
* **No worker-subset scoping.** The owner explicitly left open whether the
  administrator grant is tenant-wide or scoped to a subset of Workers, and
  directed that no narrower model be invented. The grant is tenant-wide and
  carries no subset column, so adding one later is additive.
* **No grant-management service.** Creating and revoking grants is an
  administrative action with its own authorization question, which nobody has
  ruled on. Tests insert rows directly; no application path does.
* **No role mapping.** Not even a seed. A grant is recorded, never derived.
"""

from .models import EcoViewGrant
from .outcomes import (
    DenialReason,
    ViewAuthorityDecision,
    ViewAuthorityOutcome,
)
from .service import classify_grants, require_eco_view_authority

__all__ = [
    # public entry point
    "require_eco_view_authority",
    # vocabulary
    "ViewAuthorityOutcome",
    "DenialReason",
    "ViewAuthorityDecision",
    # storage
    "EcoViewGrant",
    # internals a test may reach for
    "classify_grants",
]
