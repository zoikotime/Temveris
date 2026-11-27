"""
ZT-SPEC-ECO-002 §36.2 — Neutral reason taxonomy (a definition, nothing more).
=============================================================================

This section resolves defect D-06, the most serious defect in the withdrawn
ZT-WFM-ECO-001 v1.0 draft, which offered "Illness", "Medical appointment" and
"Family responsibility" as clock-out reason options — collecting GDPR Art. 9
special-category data at scale, frequently on a shared device, into an
append-only store from which it cannot be erased.

§36.1 records why that could not be patched downstream: the lawful basis does
not cover "a dropdown at a time clock, offered to every Worker, every day, as a
convenience"; employment-context consent is presumed non-freely-given; erasure
from an append-only store is structurally impossible; and minimisation fails on
its face because the control needs to know *whether an exclusion applies*, never
*why* the Worker was absent.

THE DESIGN MOVE (§36.2)
-----------------------
Illness, medical appointments and caring responsibilities collapse into a single
undifferentiated ``PERSONAL`` category. Per the specification this "is not a loss
of fidelity — it is the point": the control gains nothing from the distinction,
and a Worker needing a genuine medical exclusion has a better route — the Leave
module, which has the lawful basis, access controls, retention model and
approval workflow that health data requires.

WHAT THIS MODULE IS
-------------------
A definition. It is not connected to a database, has no column, no API, no
schema, no modal and no UI. Nothing collects a reason anywhere in this
repository as a result of this file existing.

INVARIANTS THIS DEFINITION ENCODES (asserted in tests)
-------------------------------------------------------
* INV-5 / §36.2 — no category references health, disability, medical care,
  family care, religion or union activity.
* INV-1 / §8.1 — the reason is OPTIONAL. ``PREFER_NOT_TO_SAY`` exists, costs the
  Worker nothing downstream, and is explicitly "Not treated as missing, not
  flagged, no downstream effect" (§36.2). Selecting nothing is a valid
  submission and MUST NOT be recorded as non-compliance.
* §26.2 — no option is required, and none may be presented as the approved choice.

The withdrawn v1.0 flow (a MANDATORY free-text reason, 15–1000 characters,
blocking the clock-out until typed) is superseded and MUST NOT be reused.
"""

from __future__ import annotations

from typing import Dict, Tuple

# ── §36.2 — the taxonomy table, transcribed ──────────────────────────────────
# For each category:
#   label                 — the option wording shown at WF-ECO-02 (§26.2)
#   special_category_risk — the specification's own risk column
#   effect_on_calculation — the specification's own effect column
#
# "effect_on_calculation" is DESCRIPTIVE. No code in this repository acts on it;
# the exclusion engine it describes (§14, §19 step 12) is BLOCKED pending the
# platform primitives P-1/P-4. It is recorded so the mapping is not re-invented
# from memory later.
REASON_CATEGORIES: Dict[str, Dict[str, str]] = {
    "MANAGER_AUTHORISED": {
        "label": "Manager authorised my early finish",
        "special_category_risk": "None",
        "effect_on_calculation":
            "None automatically. Prompts the reviewer to confirm and record "
            "AUTHORISED_RELEASE.",
    },
    "WORK_COMPLETED_RELEASE": {
        "label": "Work completed / authorised release",
        "special_category_risk": "None",
        "effect_on_calculation": "As above.",
    },
    "EMPLOYER_DIRECTED": {
        "label": "Employer-directed closure or early release",
        "special_category_risk": "None",
        "effect_on_calculation":
            "Prompts matching against an EMPLOYER_CLOSURE record.",
    },
    "OPERATIONAL_OR_TECHNICAL": {
        "label": "Operational or technical issue",
        "special_category_risk": "None",
        "effect_on_calculation":
            "Prompts matching against a PLATFORM_OUTAGE or Tenant incident.",
    },
    "TRAVEL_DISRUPTION": {
        "label": "Travel or transport disruption",
        "special_category_risk": "None",
        "effect_on_calculation": "None automatically.",
    },
    "PERSONAL": {
        "label": "Personal reason — no detail required",
        "special_category_risk":
            "None by construction — deliberately undifferentiated",
        "effect_on_calculation":
            "None automatically. Covers illness, appointments, caring and "
            "anything else, without capturing which.",
    },
    "PREFER_NOT_TO_SAY": {
        "label": "Prefer not to say",
        "special_category_risk": "None",
        "effect_on_calculation":
            "None. Not treated as missing, not flagged, no downstream effect.",
    },
    "OTHER": {
        "label": "Other",
        "special_category_risk": "Managed — see §36.3",
        "effect_on_calculation": "None automatically.",
    },
}

# Display order as listed at WF-ECO-02 (§26.2).
REASON_CATEGORY_KEYS: Tuple[str, ...] = (
    "MANAGER_AUTHORISED",
    "WORK_COMPLETED_RELEASE",
    "EMPLOYER_DIRECTED",
    "OPERATIONAL_OR_TECHNICAL",
    "TRAVEL_DISRUPTION",
    "PERSONAL",
    "PREFER_NOT_TO_SAY",
    "OTHER",
)

# INV-1 / §8.1: "The reason field MUST be optional."
REASON_IS_OPTIONAL: bool = True

# INV-5: nothing in this taxonomy collects special-category data. The empty
# tuple is the assertion — it is checked in tests, so any future addition of a
# health/medical/family/religion/union field fails CI rather than shipping.
SPECIAL_CATEGORY_FIELDS_COLLECTED: Tuple[str, ...] = ()

# Vocabulary that MUST NOT appear as a reason option (INV-5, §36.1, defect
# D-06). These are the v1.0 options and their near neighbours. Asserted absent
# from every label and key in tests.
FORBIDDEN_REASON_CONCEPTS: Tuple[str, ...] = (
    "illness",
    "ill",
    "sick",
    "sickness",
    "medical",
    "medicine",
    "doctor",
    "appointment",
    "health",
    "disability",
    "disabled",
    "diagnosis",
    "treatment",
    "therapy",
    "pregnan",       # pregnancy / pregnant
    "family",
    "carer",
    "caring",
    "childcare",
    "bereavement",
    "religion",
    "religious",
    "union",
    "mental",
)
