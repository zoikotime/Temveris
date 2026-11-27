"""
ZT-SPEC-ECO-002 §31 — Locked Copy Register (declarative data only).
===================================================================

§31 verbatim:

    "These strings are legally load-bearing. They MUST NOT be paraphrased,
    shortened for layout, or rewritten by a content pass without Legal review.
    Localisation MUST preserve meaning, not literal wording, and each locale
    MUST be reviewed by local counsel."

Every value below is transcribed character-for-character from the specification,
including its typographic apostrophes (U+2019) and em dashes (U+2014). Straight
quotes and hyphens are NOT equivalent here: this register is the comparison
target for AC-20 ("Locked strings match the register exactly in the default
locale"), so normalising the typography would defeat the check it exists to serve.

NOT WIRED TO ANY SURFACE. There is no UI, no API, no template and no i18n
integration. The Early Clock-Out surfaces (WF-ECO-01..05) remain BLOCKED —
building them would mean displaying figures the platform cannot compute. This
register exists now so that whenever those surfaces are built, the wording is
already fixed and reviewable, and so a content pass cannot quietly reword a
legally load-bearing string.

ONE DELIBERATE EXEMPTION FROM §31.1
------------------------------------
``eco.admin.disclaimer`` contains the word "misconduct", which §31.1 lists as
prohibited. This is not a defect and MUST NOT be "fixed": the string is the
specification's own locked wording, mandated verbatim by §29.1 ("The closing
statement is a locked string and MUST appear on this surface and in every
administrator notification") and §33.4 ("mandatory in every administrator
notification, in the body, not in a footer"). It uses the term in a *negating*
construction — "It is not a finding of misconduct" — which is the operative
expression of INV-9 rather than a breach of it.

§31.1 governs copy authored FOR Early Clock-Out surfaces. §31 strings ARE the
approved copy. The exemption is therefore specification-permitted, is limited to
this single key, and is asserted explicitly in
``tests/test_eco_locked_copy.py``.
"""

from __future__ import annotations

from typing import Dict, Tuple

# ── §31 Locked Copy Register ─────────────────────────────────────────────────
# key -> string, transcribed verbatim from the §31 table.
LOCKED_COPY: Dict[str, str] = {
    "eco.advisory.title":
        "You’re clocking out before completing today’s required time",

    "eco.advisory.provisional":
        "These figures are provisional and may change if your timesheet, breaks "
        "or leave are updated later.",

    "eco.advisory.threshold":
        "If this stands, your weekly total will reach the review threshold. You "
        "and your organisation’s designated reviewer would both be notified, "
        "and you can add context or dispute it at any point.",

    "eco.reason.prompt":
        "Add a reason (optional)",

    "eco.reason.personal":
        "Personal reason — no detail required",

    "eco.reason.decline":
        "Prefer not to say",

    "eco.degraded.body":
        "We can’t show your working-time summary right now. Your clock-out "
        "will still be recorded accurately.",

    "eco.status.threshold":
        "Threshold reached",

    "eco.admin.disclaimer":
        "This is an attendance condition recorded by Temveris. It is not a "
        "finding of misconduct and carries no recommendation. Any employment "
        "decision is yours and must be made by a person.",

    "eco.explain.cta":
        "How this was calculated",

    "eco.dispute.cta":
        "Dispute this day",

    "eco.policy.norecalc":
        "Workweeks already completed will not be recalculated.",
}

# ── §31 "WHERE" column — which surface each string belongs to ────────────────
# Recorded so a future implementer places each string on the surface the
# specification assigns it to, rather than guessing.
LOCKED_COPY_SURFACES: Dict[str, str] = {
    "eco.advisory.title":       "WF-ECO-02",
    "eco.advisory.provisional": "WF-ECO-02",
    "eco.advisory.threshold":   "WF-ECO-02",
    "eco.reason.prompt":        "WF-ECO-02",
    "eco.reason.personal":      "WF-ECO-02",
    "eco.reason.decline":       "WF-ECO-02",
    "eco.degraded.body":        "WF-ECO-02D",
    "eco.status.threshold":     "All surfaces",
    "eco.admin.disclaimer":     "WF-ECO-04, all administrator notifications",
    "eco.explain.cta":          "All surfaces showing a figure",
    "eco.dispute.cta":          "WF-ECO-03",
    "eco.policy.norecalc":      "WF-ECO-05",
}

# The single key permitted to contain §31.1 vocabulary — see the module
# docstring. Any addition to this tuple is a Legal question, not a code change.
LOCKED_COPY_TERMINOLOGY_EXEMPT: Tuple[str, ...] = ("eco.admin.disclaimer",)


# ═════════════════════════════════════════════════════════════════════════════
# §28.1 status vocabulary — OWNER-AUTHORIZED, NOT §31-REGISTERED
# ═════════════════════════════════════════════════════════════════════════════
# A SEPARATE register, deliberately. ``LOCKED_COPY`` above is the §31 table and
# nothing else, so the independent-transcription guard in
# tests/test_eco_locked_copy.py keeps asserting "exactly the twelve §31 keys"
# without amendment. Merging these in would have meant relaxing that assertion,
# which is the one thing the anti-drift mechanism cannot afford.
#
# THE SPECIFICATION DEFECT THIS RECORDS
# -------------------------------------
# §28.1 states the per-day Status vocabulary is "locked to Complete · Counted ·
# Excluded · Under review · Deferred · Not a working day (§31)" — but §31's
# table registers only ``eco.status.threshold`` among them. The cross-reference
# points at strings §31 does not carry.
#
# That mismatch is a SPECIFICATION defect and is deliberately NOT resolved in
# code. Nothing here amends §31, claims §31 authority, or transcribes the five
# §28.1 labels that have not been ruled on. What it does is record the one
# label the owner explicitly authorized, with its real authority named.
#
# WHY THESE SIX ARE AUTHORIZED
# -----------------------------
# §28.1 enumerates the six values literally, so transcribing them is
# transcription rather than invention — but the owner ruled explicitly that
# being listed in §28.1 does NOT by itself make a label production-approved.
# Each was authorized by a separate decision: ``Deferred`` first, for GV-17
# (§24.7), then the remaining five verbatim once a complete Status column was
# needed. Additions still require editing ``SECTION_28_1_AUTHORIZED_KEYS``
# below — a visible, reviewable act that a test pins — and never happen
# implicitly.
#
# THE EXPLANATORY BODY LIVES ELSEWHERE, AND ONLY ONE STATUS HAS ONE
# ------------------------------------------------------------------
# §24.7 requires a DEFERRED day to be explainable, not merely labelled, and no
# wording for that exists anywhere in the specification. The owner authored one
# and it is registered in ``OWNER_AUTHORIZED_COPY`` below, under owner
# authority rather than §31. The other five statuses were ruled label-only, so
# no body exists or is authorized for them — see
# ``SECTION_28_1_STATUSES_WITH_BODY``.

#: Keys this register is permitted to hold. Owner-authorized.
#:
#: ``Deferred`` was authorized alone for GV-17 (§24.7). The owner subsequently
#: ruled the remaining five verbatim, so the vocabulary §28.1 describes is now
#: complete — which is what makes a WF-ECO-03 Status column buildable at all: a
#: real weekly detail shows all six values in one column, and five of them
#: previously had nothing approved to display.
#:
#: STILL NOT AUTOMATIC. Being listed in §28.1 does not make a label
#: production-approved; each of these six was explicitly ruled. A seventh key
#: requires editing this tuple, which a test pins.
SECTION_28_1_AUTHORIZED_KEYS: Tuple[str, ...] = (
    "eco.status.complete",
    "eco.status.counted",
    "eco.status.deferred",
    "eco.status.excluded",
    "eco.status.notaworkingday",
    "eco.status.underreview",
)

#: key -> string, transcribed verbatim from the §28.1 status vocabulary.
#: The owner ruled the §28.1 words ARE the production strings, so each value
#: below is that section's own wording, unchanged.
SECTION_28_1_STATUS_COPY: Dict[str, str] = {
    "eco.status.complete":
        "Complete",

    "eco.status.counted":
        "Counted",

    "eco.status.deferred":
        "Deferred",

    "eco.status.excluded":
        "Excluded",

    "eco.status.notaworkingday":
        "Not a working day",

    "eco.status.underreview":
        "Under review",
}

#: Where each §28.1 status label is rendered. §28.1 fixes the vocabulary for
#: WF-ECO-03's per-day Status column; §29's WF-ECO-04 table shows the same
#: column with the same values, so both surfaces are recorded from the
#: specification rather than inferred.
SECTION_28_1_STATUS_SURFACES: Dict[str, str] = {
    "eco.status.complete":          "WF-ECO-03, WF-ECO-04",
    "eco.status.counted":           "WF-ECO-03, WF-ECO-04",
    "eco.status.deferred":          "WF-ECO-03, WF-ECO-04",
    "eco.status.excluded":          "WF-ECO-03, WF-ECO-04",
    "eco.status.notaworkingday":    "WF-ECO-03, WF-ECO-04",
    "eco.status.underreview":       "WF-ECO-03, WF-ECO-04",
}

#: ONLY ``Deferred`` carries an explanatory body. §24.7 requires that day to be
#: explainable, and the owner authored one; the owner ruled the other five
#: label-only. That asymmetry is deliberate — an explanatory sentence for a
#: status nobody asked to have explained would be invented copy.
SECTION_28_1_STATUSES_WITH_BODY: Tuple[str, ...] = ("eco.status.deferred",)


# ═════════════════════════════════════════════════════════════════════════════
# OWNER-AUTHORED PRODUCTION COPY — a THIRD authority, and deliberately so
# ═════════════════════════════════════════════════════════════════════════════
# A third register, because there are now three distinct authorities and
# collapsing them would make the register lie about where a string came from:
#
#   LOCKED_COPY               §31           transcribed from the §31 table
#   SECTION_28_1_STATUS_COPY  §28.1         transcribed from the §28.1 list
#   OWNER_AUTHORIZED_COPY     owner ruling  AUTHORED, not transcribed
#
# The first two transcribe wording the specification already contains. This one
# does not: §24.7 requires a deferred day to be explainable but supplies no
# sentence anywhere in §24.7, §28, §29, §31, §32 or §33, so the wording below
# was authored and approved by the owner for GV-17.
#
# THAT DISTINCTION IS THE POINT. A string here carries owner authority, NOT §31
# authority, and must never be described as §31 production copy — no external
# Legal approval under §31 was obtained. If §31 is later amended to carry this
# string, it moves to ``LOCKED_COPY`` and this entry goes away; until then the
# register states the provenance it actually has.
#
# Everything else that governs §31 copy governs this too: §31.1 prohibited
# terminology applies with NO exemption, a surface must be recorded, and the
# anti-drift guard compares against an independent transcription.

#: Keys this register is permitted to hold. Owner-authorized, one at a time.
#:
#: TWO deferral bodies, and they are NOT interchangeable (owner ruling D-2).
#: The first was authored for GV-17's §24.7 EXIT deferral and names the exit
#: twice. When owner ruling D-1 admitted a second deferral condition — an
#: unresolved break classification — reusing that body would have told a Worker
#: their exit was being resolved when it may have been verified hours earlier.
#: The owner declined the reuse and authored a second body rather than
#: generalising the first into something true of neither.
OWNER_AUTHORIZED_KEYS: Tuple[str, ...] = (
    "eco.deferred.body",
    "eco.classification_deferred.body",
)

#: key -> string, exactly as the owner approved it. Note the typographic
#: apostrophe (U+2019) in "today’s", matching the §31 register's convention;
#: a straight quote here would be a different string.
OWNER_AUTHORIZED_COPY: Dict[str, str] = {
    #: DEPARTURE / EXIT deferral only (§24.7, GV-17).
    "eco.deferred.body":
        "Your exit is still being resolved, so today’s shortfall is not "
        "final; while this day is deferred, it contributes 0 minutes to your "
        "weekly total and will be evaluated when the exit is resolved.",
    #: CLASSIFICATION deferral only (D-1/D-2). Structurally parallel to the
    #: body above and different in exactly the clause that would otherwise be
    #: false: what is unresolved, and what resolving it depends on.
    "eco.classification_deferred.body":
        "Your working-time classification is still being resolved, so today’s "
        "shortfall is not final; while this day is deferred, it contributes 0 "
        "minutes to your weekly total and will be evaluated when the "
        "classification is resolved.",
}

#: Surfaces, per the owner ruling: WF-ECO-03 (Worker) and WF-ECO-04
#: (Administrator), audience model A — one body serves both audiences.
OWNER_AUTHORIZED_SURFACES: Dict[str, str] = {
    "eco.deferred.body":                "WF-ECO-03, WF-ECO-04",
    "eco.classification_deferred.body": "WF-ECO-03, WF-ECO-04",
}


# ── §36.3 — locked notice preceding the optional free text under OTHER ───────
# §36.3: free text is "Capped at 280 characters, optional, and preceded by the
# locked notice". Recorded here as data; no input field exists.
REASON_FREE_TEXT_NOTICE: str = (
    "Please don’t include health, medical or other sensitive personal "
    "details here. If your absence relates to health, submit a leave request "
    "instead."
)

REASON_FREE_TEXT_MAX_CHARS: int = 280


# ── §36.4 — locked guidance line shown with the reason list ──────────────────
# §36.4: "This is a locked string and it MUST be shown, not hidden behind an
# information icon. Its purpose is to remove the pressure that produces
# over-disclosure."
#
# TRANSCRIPTION NOTE (observation, not a correction): the specification renders
# the personal-reason option here as "Personal reason—no detail required"
# (no spaces around the em dash) while §31 and §26.2 render it as
# "Personal reason — no detail required" (spaced). Both are reproduced
# exactly as they appear in their own sections. Normalising them would be an
# unauthorised edit to a locked string; the discrepancy is recorded for Legal /
# the Cross-Asset Consistency Sheet to resolve.
REASON_GUIDANCE_LINE: str = (
    "You never have to explain a personal reason. Choosing ‘Personal "
    "reason—no detail required’ or ‘Prefer not to say’ has "
    "exactly the same effect as any other option."
)
