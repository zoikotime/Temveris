"""
Temveris — Early Clock-Out (ZT-SPEC-ECO-002 v2.0) — SPECIFICATION ARTIFACTS ONLY
=================================================================================

WHAT THIS PACKAGE IS
--------------------
Declarative, non-executing artifacts transcribed from ZT-SPEC-ECO-002 v2.0:

  * ``terminology``    — §31.1 prohibited terminology + a pure string scanner
  * ``copy_register``  — §31 locked copy register, §36.3 and §36.4 locked notices
  * ``reason_taxonomy``— §36.2 neutral reason taxonomy (a definition, nothing more)

Nothing here computes anything about time, reads the database, or is wired into
any route. This package is NOT registered in ``main.py`` — deliberately, in the
same way ``app/attendance_policy`` was a dormant policy foundation before it had
consumers.

WHAT THIS PACKAGE IS NOT — AND MUST NOT BECOME
----------------------------------------------
This package still computes NOTHING, and that prohibition has not expired —
but the REASON it exists has changed, so read the current one rather than the
original.

ORIGINALLY, a three-phase read-only audit blocked the calculation engine
because the platform lacked the primitives ZT-SPEC-ECO-002 requires. Six of
those seven have since been built, each in its own package:

  P-1  sealed state interval ledger          ``app.work_ledger``
       (``ledger_version``, ordered sealed intervals)
  P-2  deterministic BRK_M / BRK_R classifier ``app.break_classification``
       with independent treatment (INV-4)     ``app.break_treatment``
  P-3  departure provenance separating a genuine clock-out from a
       system-synthesized auto-close (§24.6 / GV-16)
                                              ``app.clock_session_boundary``
  P-4  minute-granular leave (§14.2)          ``app.eval_day.leave_snapshot``
  P-5  historically resolvable policy versioning (§17 / GV-21)
                                              ``app.policy_version``
  P-7  durable feature flag for §44 shadow    ``app.rollout``

  P-6  Master Reference Architecture and Cross-Asset Consistency Sheet —
       STILL ABSENT. The specification itself is not in this repository
       either, which is why owner rulings and the §42 golden vectors are the
       authoritative artifacts a reader should reach for.

SO THE PROHIBITION NOW RESTS ON DUPLICATION, NOT ABSENCE. Those packages are
where the primitives live. Building any of them a second time HERE would
create exactly the second source of truth the original block existed to
prevent — the same defect, arrived at from the opposite direction.

DO NOT ADD TO THIS PACKAGE:
  * any qualifying-minutes, shortfall or attributable-shortfall calculation
  * any weekly fold or threshold engine
  * a local ledger, classifier, policy store, leave mapping or break mapping
  * any read of ``app/_worked.py`` or of ``clock_out - clock_in - breaks``

Computing worked time here would create a second source of truth — precisely the
drift INV-2 and §7 exist to prevent, and the defect (D-03) for which the
superseded ZT-WFM-ECO-001 v1.0 draft was withdrawn.

The withdrawn v1.0 implementation still exists elsewhere in the repository and is
disabled (``EARLY_CLOCK_OUT_FEATURE_ENABLED = False``). It is historical evidence
only. Do not enable it, reuse its free-text reason model, or treat it as a
foundation for v2.0.
"""

from .terminology import (
    PROHIBITED_TERMS,
    PROHIBITED_PHRASES,
    PROHIBITED_ENTRY_COUNT,
    NEUTRAL_VOCABULARY,
    find_prohibited_terms,
    scan_text,
    is_clean,
    format_hits,
)
from .copy_register import (
    LOCKED_COPY,
    LOCKED_COPY_SURFACES,
    LOCKED_COPY_TERMINOLOGY_EXEMPT,
    SECTION_28_1_AUTHORIZED_KEYS,
    SECTION_28_1_STATUS_COPY,
    SECTION_28_1_STATUS_SURFACES,
    SECTION_28_1_STATUSES_WITH_BODY,
    OWNER_AUTHORIZED_KEYS,
    OWNER_AUTHORIZED_COPY,
    OWNER_AUTHORIZED_SURFACES,
    REASON_FREE_TEXT_NOTICE,
    REASON_GUIDANCE_LINE,
    REASON_FREE_TEXT_MAX_CHARS,
)
from .reason_taxonomy import (
    REASON_CATEGORIES,
    REASON_CATEGORY_KEYS,
    REASON_IS_OPTIONAL,
    SPECIAL_CATEGORY_FIELDS_COLLECTED,
    FORBIDDEN_REASON_CONCEPTS,
)

__all__ = [
    # §31.1
    "PROHIBITED_TERMS",
    "PROHIBITED_PHRASES",
    "PROHIBITED_ENTRY_COUNT",
    "NEUTRAL_VOCABULARY",
    "find_prohibited_terms",
    "scan_text",
    "is_clean",
    "format_hits",
    # §31 / §36.3 / §36.4
    "LOCKED_COPY",
    "LOCKED_COPY_SURFACES",
    "LOCKED_COPY_TERMINOLOGY_EXEMPT",
    # §28.1 status vocabulary — owner-authorized, NOT §31-registered
    "SECTION_28_1_AUTHORIZED_KEYS",
    "SECTION_28_1_STATUS_COPY",
    "SECTION_28_1_STATUS_SURFACES",
    "SECTION_28_1_STATUSES_WITH_BODY",
    # owner-authored production copy — NOT §31 authority
    "OWNER_AUTHORIZED_KEYS",
    "OWNER_AUTHORIZED_COPY",
    "OWNER_AUTHORIZED_SURFACES",
    "REASON_FREE_TEXT_NOTICE",
    "REASON_GUIDANCE_LINE",
    "REASON_FREE_TEXT_MAX_CHARS",
    # §36.2
    "REASON_CATEGORIES",
    "REASON_CATEGORY_KEYS",
    "REASON_IS_OPTIONAL",
    "SPECIAL_CATEGORY_FIELDS_COLLECTED",
    "FORBIDDEN_REASON_CONCEPTS",
]
