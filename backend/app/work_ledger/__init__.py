"""
Temveris — Canonical Sealed Work-State Interval Ledger
======================================================

STEP 1 of the owner-authorised Phase 2 platform foundation, under owner ruling
**D-03 Option A**: a platform-wide sealed work-state interval ledger becomes the
canonical attendance truth.

STATUS
------
Step 1 delivers the ledger primitive: states, provenance, integrity validation,
sealing, supersession, idempotency and optimistic concurrency — with tests.
It is NOT yet wired to anything. Existing clock-out behaviour is unchanged, and
`app/_worked.py::session_worked_seconds` still computes what it computed before.

THE INTENDED END STATE
----------------------
``session_worked_seconds`` is re-expressed as a deterministic fold over these
intervals (Step 9), and all 20 identified worked-time consumers are migrated to
call that fold (Step 10). At that point exactly one answer to "how much did
this Worker work?" exists. This ledger is not a second source of truth; it is
THE source, and the fold is its only reader.

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* Any worked-time calculation. The fold lives in Step 9 and is the single
  implementation; a second one here would be the exact drift INV-2 and §7 exist
  to prevent, and the defect (D-03 in the specification's own register) for
  which ZT-WFM-ECO-001 v1.0 was withdrawn.
* Any import of `app.active_time`, `models.Activity`, `models.ActivityWindow`,
  `models.BrowserActivity` or `models.AppSession`. INV-6 forbids this control
  from reading screenshots, keystroke content or application names at any tier;
  INV-7 keeps scoring out of the decision path. Enforced by an import-graph test.
* Any repair, clamping, merging or skipping of invalid or overlapping evidence.
  §24.15 requires refusal. The existing platform deliberately does the opposite
  for its own dashboard purposes; this ledger must not inherit that behaviour.
* Any inferred departure time. §7.2 calls that "the single worst thing this
  control could do".

MIGRATION
---------
These tables are created by `d4b7e2f9a615_phase2_foundation_ledger_and_config`,
a single-parent revision chained to the head `e4f7a2b91c60`.

CORRECTION OF AN EARLIER NOTE: Step 1 originally recorded that the migration
graph had FOUR heads and that adding a revision would compound the condition.
That was wrong. It came from a parser that matched only single-string
`down_revision` values and so misread the tuple-valued `down_revision` of the
existing merge `z9f8e7d6c5b4`, inventing a second root and three phantom heads.
Verified by AST parse and by Alembic's own ScriptDirectory, the graph has 61
revisions, ONE root (`81c63dee733a`), ONE head, and one legitimate three-parent
merge. `alembic upgrade head` was never ambiguous, and no merge revision was
needed. Step 1's decision to defer the migration rested on a finding that did
not hold.
"""

from .states import (
    WorkState,
    RESERVED_UNSOURCED_STATES,
    BREAK_STATES,
    Provenance,
    VERIFIED_DEPARTURE_PROVENANCE,
    SYNTHESIZED_CLOSURE_PROVENANCE,
    DERIVED_BOUNDARY_PROVENANCE,
    ProvenanceClass,
    ProvenanceImmutable,
    is_valid_provenance,
    parse_provenance,
    classify_provenance,
    is_verified_departure,
    LedgerState,
)
from .provenance import (
    ProvenanceSummary,
    summarise_provenance,
    summarise_ledger_provenance,
    verified_departure_intervals,
    assert_provenance_vocabulary_closed,
)
from .integrity import (
    IntegrityFault,
    CandidateInterval,
    IntegrityViolation,
    IntegrityResult,
    duration_seconds,
    order_intervals,
    validate_intervals,
)
from .models import WorkDayLedger, WorkStateInterval
from .service import (
    DayKey,
    LedgerError,
    StaleLedgerVersion,
    LedgerEvidenceConflict,
    LedgerIntegrityFault,
    LedgerStateContractViolation,
    get_current_ledger,
    get_ledger_version,
    seal_day,
    supersede_day,
    record_integrity_fault,
    read_intervals,
)

__all__ = [
    # states
    "WorkState", "RESERVED_UNSOURCED_STATES", "BREAK_STATES",
    "Provenance", "VERIFIED_DEPARTURE_PROVENANCE",
    "SYNTHESIZED_CLOSURE_PROVENANCE", "DERIVED_BOUNDARY_PROVENANCE",
    "ProvenanceClass", "ProvenanceImmutable", "is_valid_provenance",
    "parse_provenance", "classify_provenance",
    "is_verified_departure", "LedgerState",
    # provenance integration (Step 2)
    "ProvenanceSummary", "summarise_provenance", "summarise_ledger_provenance",
    "verified_departure_intervals", "assert_provenance_vocabulary_closed",
    # integrity
    "IntegrityFault", "CandidateInterval", "IntegrityViolation",
    "IntegrityResult", "duration_seconds", "order_intervals",
    "validate_intervals",
    # models
    "WorkDayLedger", "WorkStateInterval",
    # service
    "DayKey", "LedgerError", "StaleLedgerVersion", "LedgerEvidenceConflict",
    "LedgerIntegrityFault", "LedgerStateContractViolation",
    "get_current_ledger", "get_ledger_version",
    "seal_day", "supersede_day", "record_integrity_fault", "read_intervals",
]
