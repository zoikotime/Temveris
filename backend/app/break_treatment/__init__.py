"""
BREAK-TREATMENT PROFILE — the §13.2 artifact ``break_treatment_ref`` points at.
==============================================================================

``PolicyVersion.break_treatment_ref`` has existed since POLICY-VERSION as an
opaque, nullable reference with **nothing behind it**. The policy package's own
docstring forbids using that column to encode a profile's contents, so until an
artifact existed the reference could only ever dangle. This package supplies it.

WHAT §13.2 DEFINES
------------------
    meal_qualifies               boolean
    rest_qualifies               boolean
    rest_qualifying_cap_minutes  integer, null = uncapped
    meal_shortfall_is_reportable boolean
    unrecorded_break_treatment   enum AS_WORK · AS_GAP · DEFER (default DEFER)

THIS INCREMENT CARRIES THE FIRST THREE (owner ruling P-3 + P-4)
---------------------------------------------------------------
``meal_qualifies``, ``rest_qualifies`` and ``rest_qualifying_cap_minutes`` are
the attributes P-4's already-closed semantics need. The remaining two are NOT
added here: ``meal_shortfall_is_reportable`` routes to a compliance surface
that does not exist, and ``unrecorded_break_treatment`` selects between
behaviours (AS_WORK / AS_GAP / DEFER) of which only DEFER is authorised — D-1
made the unclassifiable case defer, and inventing the other two branches would
be inventing behaviour. Adding them later is additive.

RESOLVED HISTORICALLY, EXACTLY LIKE POLICY-VERSION
---------------------------------------------------
A profile is immutable, append-only and effective-dated, and an evaluation
resolves the version in force ON THE WORK DATE. Below the first capture the
answer is ``NO_VERSION`` and the caller fails closed — never today's profile,
and never a neutral default, for the same reason §15 forbids assuming ``NONE``
netting.

WHAT THIS PACKAGE DELIBERATELY IS NOT
-------------------------------------
* **Not a classifier.** It says how a classification is TREATED, never what a
  break IS. Establishing that is ``app.break_classification`` (P-1).
* **Not netting.** ``rest_qualifying_cap_minutes`` caps qualifying rest within
  one day; MODEL-NETTING's per-day and per-period surplus caps are a different
  mechanism and are not here.
* **Not a jurisdiction model.** No legal rule selects a profile; a Tenant
  captures one.
"""

from .errors import (
    BreakTreatmentError,
    BreakTreatmentImmutable,
    BreakTreatmentProfileCollision,
    InvalidBreakTreatmentProfile,
)
from .models import BreakTreatmentProfile, compute_profile_content_hash
from .outcomes import ProfileOutcome, ProfileResolution
from .service import capture_break_treatment_profile, resolve_profile_as_of

__all__ = [
    "BreakTreatmentProfile",
    "compute_profile_content_hash",
    "ProfileOutcome",
    "ProfileResolution",
    "capture_break_treatment_profile",
    "resolve_profile_as_of",
    "BreakTreatmentError",
    "InvalidBreakTreatmentProfile",
    "BreakTreatmentImmutable",
    "BreakTreatmentProfileCollision",
]
