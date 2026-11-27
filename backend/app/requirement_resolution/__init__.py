"""
Temveris — Strict Requirement Resolution (fail-closed evaluation path)
=======================================================================

STEP 5 of the owner-authorised Phase 2 platform foundation, under owner ruling
**D-10 Option A**: clock-out remains fail-open and non-blocking; ECO evaluation
becomes fail-closed.

STATUS
------
Step 5 delivers the strict resolver and the configuration-version port it
resolves against. It is NOT wired to anything. The existing fail-open resolver,
``attendance_policy.resolve_expected_work_seconds``, is untouched and remains
the clock-out path's answer.

THE SEPARATION, IN ONE LINE
---------------------------
A Worker whose requirement cannot be resolved still clocks out; their day
simply is not evaluated.

WHAT STEP 5 REQUIRED OF STEP 6
------------------------------
``ConfigVersionProvider`` is the seam. Step 6 (immutable, effective-dated
configuration) supplies it — see ``app.config_versioning``, which provides both
an in-memory reference implementation and a database-backed adapter held to the
same contract suite. Step 5 itself needed no schema change and still has none.

(An earlier version of this note cited a "four-head Alembic condition" as
context. That finding was a parser error and has been withdrawn; the migration
graph has a single head. See ``app.work_ledger`` for the correction.)

WHAT MUST NEVER BE ADDED HERE
-----------------------------
* A default requirement. There is deliberately no constant in this package that
  could become one. §10.2 forbids assuming zero or a default, and the result
  type refuses to carry a number on a non-resolved outcome.
* A fallback to live configuration for a historical date (Gate 7 floor).
* Any worked-time calculation — this package resolves what is REQUIRED, never
  what was WORKED. Asserted by an import-graph test.
* Any Activity / screenshot / application-name dependency (INV-6, INV-7).
* Any change to the clock-out path. Collapsing fail-open and fail-closed is the
  specific error D-10 exists to prevent.

OPEN GOVERNANCE ITEM
--------------------
§10.1's six precedence tokens are not hard-coded here; tiers are
provider-declared strings. Two of those tokens are TENANT-prefixed, and whether
they are identifiers (ORG-prefixed under D-09 Option C) or specification labels
carried verbatim is a decision for the §4.1 amendment and the Cross-Asset
Consistency Sheet, neither of which has been issued. See ``ports`` for the
reasoning.
"""

from .outcomes import (
    ResolutionOutcome,
    NON_EVALUABLE_OUTCOMES,
    InvalidCandidate,
    RequirementResolution,
)
from .ports import (
    ConfigVersionRef,
    RequirementCandidate,
    ConfigVersionProvider,
)
from .resolver import resolve_requirement_strict, MAX_REQUIRED_MINUTES

__all__ = [
    "ResolutionOutcome",
    "NON_EVALUABLE_OUTCOMES",
    "InvalidCandidate",
    "RequirementResolution",
    "ConfigVersionRef",
    "RequirementCandidate",
    "ConfigVersionProvider",
    "resolve_requirement_strict",
    "MAX_REQUIRED_MINUTES",
]
