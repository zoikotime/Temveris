"""
ZT-SPEC-ECO-002 §36.2 — neutral reason taxonomy validation.
============================================================

This taxonomy resolves defect D-06, the most serious defect in the withdrawn
v1.0 draft: "Illness", "Medical appointment" and "Family responsibility" offered
as clock-out reason options, collecting GDPR Art. 9 special-category data at
scale on a shared device into an append-only store.

AC-05 is the criterion these tests encode:

    "GIVEN the reason list at clock-out
     THEN no option references health, disability, medical care, family care,
     religion or union activity, AND 'Prefer not to say' is present, AND no
     option is required. [INV-5]"

The taxonomy here is a DEFINITION. There is no column, no API, no modal, no UI
and no persistence — those all sit behind the BLOCKED calculation core. What
these tests protect is that when the surface is eventually built, the categories
it is built from cannot have drifted back toward v1.0.
"""

from __future__ import annotations

import pytest

from app.early_clock_out import copy_register as C
from app.early_clock_out import reason_taxonomy as R
from app.early_clock_out import terminology as T

EXPECTED_KEYS = (
    "MANAGER_AUTHORISED",
    "WORK_COMPLETED_RELEASE",
    "EMPLOYER_DIRECTED",
    "OPERATIONAL_OR_TECHNICAL",
    "TRAVEL_DISRUPTION",
    "PERSONAL",
    "PREFER_NOT_TO_SAY",
    "OTHER",
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Exactly the approved categories — no more, no fewer
# ══════════════════════════════════════════════════════════════════════════════

def test_taxonomy_has_exactly_the_eight_specification_categories():
    assert set(R.REASON_CATEGORIES) == set(EXPECTED_KEYS)
    assert R.REASON_CATEGORY_KEYS == EXPECTED_KEYS
    assert len(R.REASON_CATEGORIES) == 8


def test_display_order_matches_wf_eco_02():
    """§26.2 lists the options in this order, with "Prefer not to say" ahead of
    "Other" so declining is never the last resort on the list."""
    assert R.REASON_CATEGORY_KEYS.index("PERSONAL") < \
           R.REASON_CATEGORY_KEYS.index("PREFER_NOT_TO_SAY")
    assert R.REASON_CATEGORY_KEYS[-1] == "OTHER"


def test_every_category_has_a_label_and_the_specification_columns():
    for key, entry in R.REASON_CATEGORIES.items():
        assert entry.get("label"), f"{key} has no label"
        assert entry.get("special_category_risk"), f"{key} has no risk column"
        assert entry.get("effect_on_calculation"), f"{key} has no effect column"


# ══════════════════════════════════════════════════════════════════════════════
# 2. INV-5 — no special-category collection (AC-05)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("key", EXPECTED_KEYS)
def test_no_category_key_references_a_special_category_concept(key):
    lowered = key.lower()
    for banned in R.FORBIDDEN_REASON_CONCEPTS:
        assert banned not in lowered, (
            f"category key {key} references the special-category concept "
            f"{banned!r} — this is defect D-06 (INV-5)"
        )


@pytest.mark.parametrize("key", EXPECTED_KEYS)
def test_no_selectable_label_references_a_special_category_concept(key):
    """Scoped to the LABEL — the only text a Worker can select.

    PERSONAL's descriptive `effect_on_calculation` legitimately says it "covers
    illness, appointments, caring and anything else, without capturing which".
    That sentence is the specification explaining WHY the category is
    undifferentiated; it is documentation, not a field, and is not offered to
    anybody. Scanning it would flag the very design decision that fixes D-06.
    """
    label = R.REASON_CATEGORIES[key]["label"].lower()
    for banned in R.FORBIDDEN_REASON_CONCEPTS:
        assert banned not in label, (
            f"reason label {R.REASON_CATEGORIES[key]['label']!r} references "
            f"{banned!r} — INV-5 forbids collecting this at the clock-out surface"
        )


def test_the_three_withdrawn_v1_options_are_absent():
    """The exact options §36 names as the defect."""
    labels = {e["label"].lower() for e in R.REASON_CATEGORIES.values()}
    keys = {k.lower() for k in R.REASON_CATEGORIES}
    for withdrawn in ("illness", "medical appointment", "family responsibility"):
        assert not any(withdrawn in lbl for lbl in labels), withdrawn
        assert not any(withdrawn.replace(" ", "_") in k for k in keys), withdrawn


def test_no_special_category_field_is_collected():
    """The empty tuple is the assertion. Adding a health/medical/family field
    fails here rather than shipping."""
    assert R.SPECIAL_CATEGORY_FIELDS_COLLECTED == ()


def test_personal_is_deliberately_undifferentiated():
    """§36.2: "None by construction — deliberately undifferentiated". This is
    the design move that makes the taxonomy lawful, so it is pinned."""
    entry = R.REASON_CATEGORIES["PERSONAL"]
    assert "undifferentiated" in entry["special_category_risk"].lower()
    assert entry["label"] == "Personal reason — no detail required"
    assert entry["label"] == C.LOCKED_COPY["eco.reason.personal"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. INV-1 / §8.1 — the reason is optional and declining is free
# ══════════════════════════════════════════════════════════════════════════════

def test_the_reason_is_optional():
    assert R.REASON_IS_OPTIONAL is True


def test_prefer_not_to_say_exists_and_matches_the_locked_string():
    assert "PREFER_NOT_TO_SAY" in R.REASON_CATEGORIES
    assert R.REASON_CATEGORIES["PREFER_NOT_TO_SAY"]["label"] == \
        C.LOCKED_COPY["eco.reason.decline"] == "Prefer not to say"


def test_declining_carries_no_downstream_effect():
    """§8.1: it "carries no downstream penalty and is not treated as a missing
    value". §36.2: "Not treated as missing, not flagged, no downstream effect"."""
    effect = R.REASON_CATEGORIES["PREFER_NOT_TO_SAY"]["effect_on_calculation"]
    assert "Not treated as missing" in effect
    assert "no downstream effect" in effect


def test_no_category_mandates_free_text():
    """v1.0 refused to close the session until a 15–1000 character reason was
    typed. Nothing here may reintroduce a mandatory free-text field.

    OTHER is the only category carrying free text at all, and §36.3 makes it
    "Capped at 280 characters, optional".
    """
    assert R.REASON_CATEGORIES["OTHER"]["effect_on_calculation"] == "None automatically."
    assert C.REASON_FREE_TEXT_MAX_CHARS == 280
    # No category may claim to compel anything.
    for key, entry in R.REASON_CATEGORIES.items():
        effect = entry["effect_on_calculation"].lower()
        for compelling in ("mandatory", "must be provided", "reason required"):
            assert compelling not in effect, f"{key} -> {compelling}"


def test_no_category_is_an_investigation_or_misconduct_bucket():
    for key, entry in R.REASON_CATEGORIES.items():
        blob = f"{key} {entry['label']}"
        assert T.find_prohibited_terms(blob) == [], (
            f"category {key} uses §31.1 terminology in its key or label"
        )
        for banned in ("investigation", "disciplinary", "hr_review", "hr review"):
            assert banned not in blob.lower(), f"{key} -> {banned}"


# ══════════════════════════════════════════════════════════════════════════════
# 4. It stays a definition
# ══════════════════════════════════════════════════════════════════════════════

def test_taxonomy_module_is_declarative_only():
    """No database, no API, no UI — the surfaces that would consume this are
    BLOCKED pending the platform primitives."""
    import inspect
    src = inspect.getsource(R)
    for forbidden in ("APIRouter", "@router", "Depends(", "db.query",
                      "session.add", "Column(", "Base)", "HTTPException",
                      "sqlalchemy", "pydantic"):
        assert forbidden not in src, (
            f"reason_taxonomy.py appears to be growing behaviour ({forbidden!r}); "
            "§36.2 is a definition only while the ECO surfaces are blocked"
        )


def test_module_does_not_import_the_orm_or_the_worked_time_calculation():
    """Guards INV-2 by construction: a reason taxonomy has no business reading
    worked time, and importing it would be the first step toward a second
    source of truth."""
    import inspect
    src = inspect.getsource(R)
    assert "_worked" not in src
    assert "session_worked_seconds" not in src
    assert "from database" not in src


def test_the_whole_package_computes_nothing():
    """The package-wide form of the guard above.

    ``app/early_clock_out/`` is specification artifacts only. Its prohibition
    used to rest on the calculation primitives being ABSENT; six of the seven
    have since been built, each in its own package, so the prohibition now
    rests on not duplicating them here. That is a claim about every module in
    the package, not just the reason taxonomy.

    Asserted by AST import scan rather than text search, so a docstring that
    NAMES a forbidden package in order to forbid it does not trip the guard —
    which is exactly what the corrected module docstring does.
    """
    import ast
    import pathlib

    package = (pathlib.Path(__file__).resolve().parents[1]
               / "app" / "early_clock_out")

    #: The six packages that now hold the primitives. Importing any of them
    #: here would be the first step toward computing worked time twice.
    FORBIDDEN = ("app.work_ledger", "app.break_classification",
                 "app.break_treatment", "app.clock_session_boundary",
                 "app.eval_day", "app.eval_week", "app.eval_event",
                 "app.policy_version", "app.rollout", "app.eco_driver",
                 "database", "sqlalchemy", "app._worked")

    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        offenders = [m for m in imported
                     if any(m == f or m.startswith(f + ".")
                            for f in FORBIDDEN)]
        assert not offenders, f"{path.name} imports {offenders}"

        # And nothing in the package does arithmetic on time.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = (getattr(node.func, "id", None)
                        or getattr(node.func, "attr", None))
                assert name not in ("timedelta", "total_seconds"), path.name
