"""
BREAK CLASSIFICATION (P-1, P-2, C-1) — who determined it, and what they saw.
============================================================================

P-2's defect, restated: the platform has ONE generic break bucket.
``break_sessions.break_type`` is a worker-selected ``'break'|'lunch'`` UI label,
and every break type is subtracted equally. INV-4 requires ``BRK_M`` and
``BRK_R`` be kept legally distinct, and §13.3's worked example shows the cost of
collapsing them: a generic model returns a 30-minute error against a 30-minute
threshold.

WHAT THIS PACKAGE ESTABLISHES
-----------------------------
    EcoClassificationAuthority    an explicit, tenant-scoped grant. Owner
                                  ruling P-1 forbids inferring authority from
                                  ``User.role``, ``Team.head_user_id`` or any
                                  other existing role, so none is consulted.
    BreakClassificationAssertion  one authority's determination about one
                                  break, citing its evidence. Append-only and
                                  versioned; a changed determination
                                  supersedes, never rewrites.
    ClassificationSnapshot        the content-addressed set of assertions ONE
                                  evaluation used, shaped exactly like
                                  ``LeaveInputSnapshot`` because C-1 says so.

WHAT IT DELIBERATELY DOES NOT DECIDE
------------------------------------
* **Whether a break IS a meal or a rest.** P-1 leaves the legal and
  jurisdictional rules explicitly unresolved and forbids inventing them. This
  package models the CHAIN OF CUSTODY — who determined it, when, against which
  evidence, findable again through ``evidence_ref``. When the rules arrive they
  attach there, and no schema here changes.
* **How a classification is TREATED.** That is ``app.break_treatment`` (P-3).
* **Anything before sealing.** Classification consumes sealed evidence only
  (P-2). ``work_ledger_producer`` stays classification-blind, D-18 is untouched,
  and a static guard asserts this package is not imported there.
* **The worker's label as authority.** ``break_sessions.break_type`` remains
  OBSERVATIONAL input. Nothing here reads it, and nothing may infer legal
  sufficiency from it.

THE UNCLASSIFIABLE CASE
-----------------------
An explicit outcome, never an absence. A break with no live assertion, or one
asserted ``UNCLASSIFIABLE``, defers the day under owner ruling D-1 with
``EvaluationDeferralReason.CLASSIFICATION_UNRESOLVED`` — contributing a truthful
0 rather than being silently read as ``BRK_M``, ``BRK_R`` or ``WRK``.
"""

from .errors import (
    BreakClassificationError,
    ClassificationImmutable,
    ClassificationRefused,
    ConflictingClassification,
    InvalidClassificationAssertion,
    InvalidClassificationAuthority,
)
from .models import (
    ASSERTION_TABLE,
    AUTHORITY_TABLE,
    SNAPSHOT_TABLE,
    BreakClassificationAssertion,
    ClassificationSnapshot,
    EcoClassificationAuthority,
    canonical_members_json,
    compute_assertion_content_hash,
    compute_snapshot_hash,
)
from .outcomes import (
    REASON_CONFLICTING_ASSERTIONS,
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_NOT_SEALED,
    REASON_MALFORMED_SOURCE_REF,
    REASON_NO_LEDGER_DAY,
    REASON_PROFILE_AMBIGUOUS,
    REASON_PROFILE_INVALID,
    REASON_UNKNOWN_CLASSIFICATION,
    ClassificationOutcome,
    ClassificationResolution,
)
from .service import (
    BREAK_SOURCE_PREFIX,
    assert_break_classification,
    get_or_create_classification_snapshot,
    grant_classification_authority,
    resolve_classification_for_day,
)
from .vocabulary import (
    CLASSIFICATION_RESULTS,
    DETERMINATE_RESULTS,
    ClassificationResult,
    is_valid_classification,
    parse_classification,
)

__all__ = [
    # vocabulary
    "ClassificationResult", "CLASSIFICATION_RESULTS", "DETERMINATE_RESULTS",
    "is_valid_classification", "parse_classification",
    # models
    "EcoClassificationAuthority", "BreakClassificationAssertion",
    "ClassificationSnapshot", "compute_assertion_content_hash",
    "compute_snapshot_hash", "canonical_members_json",
    "AUTHORITY_TABLE", "ASSERTION_TABLE", "SNAPSHOT_TABLE",
    # outcomes
    "ClassificationOutcome", "ClassificationResolution",
    "REASON_LEDGER_NOT_SEALED", "REASON_NO_LEDGER_DAY",
    "REASON_LEDGER_INTEGRITY_FAULT", "REASON_CONFLICTING_ASSERTIONS",
    "REASON_UNKNOWN_CLASSIFICATION", "REASON_MALFORMED_SOURCE_REF",
    "REASON_PROFILE_AMBIGUOUS", "REASON_PROFILE_INVALID",
    # service
    "grant_classification_authority", "assert_break_classification",
    "get_or_create_classification_snapshot", "resolve_classification_for_day",
    "BREAK_SOURCE_PREFIX",
    # errors
    "BreakClassificationError", "InvalidClassificationAuthority",
    "InvalidClassificationAssertion", "ClassificationImmutable",
    "ConflictingClassification", "ClassificationRefused",
]
