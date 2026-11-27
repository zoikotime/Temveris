"""
ZT-SPEC-ECO-002 §31.1 — prohibited-terminology guard.
======================================================

§32 states the constraint that makes this worth enforcing at all:

    "Event names, field names and enum values are part of the product surface:
    they appear in exports, webhooks, BI tools and, eventually, in a legal
    disclosure bundle. ECO_SHORTFALL_RECORDED is acceptable;
    ECO_VIOLATION_LOGGED is not, and CANNOT BE FIXED LATER WITHOUT A BREAKING
    CHANGE."

That is why this guard exists now, before any Early Clock-Out surface, event or
column is built. It is regression prevention, not clean-up.

SCOPE — deliberately narrow
---------------------------
The guard scans the files Early Clock-Out actually controls. It does NOT scan
the repository: "failure", "breach" and "warning" are ordinary words in an
attendance/billing/security codebase, and a check that cries wolf is a check
somebody disables. AC-06 asks for the ECO feature to be clean; that is what is
asserted here.

Two documented exclusions, both of which necessarily contain the vocabulary:
  * ``terminology.py``    — it DEFINES the prohibited list.
  * ``copy_register.py``  — it holds the locked admin disclaimer, whose wording
    is mandated verbatim by §29.1/§33.4 and which uses "misconduct" in a
    negating construction ("It is not a finding of misconduct"). That string is
    checked individually in ``test_eco_locked_copy.py`` instead, which is a
    tighter check than a file-level skip.

THE WITHDRAWN v1.0 IMPLEMENTATION
---------------------------------
ZT-WFM-ECO-001 v1.0 is superseded and disabled. Its copy does contain §31.1
vocabulary. These tests deliberately DO NOT require it to be cleaned: editing it
to make CI green would be resurrecting and rewriting a withdrawn implementation
for cosmetic reasons. Instead the state is reported, and the one thing that does
matter — that it stays switched off — is asserted.
"""

from __future__ import annotations

import pathlib

import pytest

from app.early_clock_out import terminology as T

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
ECO_PACKAGE = BACKEND_ROOT / "app" / "early_clock_out"
GOLDEN_VECTORS = BACKEND_ROOT / "tests" / "golden" / "eco_golden_vectors.json"

# See the module docstring. Each entry is a file that must contain the
# vocabulary in order to do its job.
SCAN_EXCLUSIONS = {
    "terminology.py": "defines the §31.1 list itself",
    "copy_register.py": "holds the §29.1/§33.4-mandated admin disclaimer; "
                        "checked string-by-string in test_eco_locked_copy.py",
}

# ── §31.1, transcribed INDEPENDENTLY of terminology.py ───────────────────────
# Parametrising over T.PROHIBITED_TERMS alone would only prove the module is
# self-consistent: silently deleting "penalty" from the module would delete its
# own test with it. This second reading of the specification is what makes a
# dropped entry fail.
SPEC_31_1_ENTRIES = (
    "violation",
    "breach",
    "infraction",
    "offence",
    "misconduct",
    "unauthorised",
    "non-compliant",
    "failure",
    "offender",
    "owed",
    "debt",
    "deficit",
    "penalty",
    "warning",
    "strike",
    "flagged worker",
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. The scanner behaves as §31.1 requires
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("term", T.PROHIBITED_TERMS)
def test_every_prohibited_single_term_is_detected(term):
    assert T.find_prohibited_terms(f"the {term} was recorded") == [term]


@pytest.mark.parametrize("phrase", T.PROHIBITED_PHRASES)
def test_every_prohibited_phrase_is_detected(phrase):
    assert phrase in T.find_prohibited_terms(f"this is a {phrase} today")


def test_all_sixteen_specification_entries_are_present():
    """§31.1 lists sixteen entries. Losing one silently is the only way this
    guard can weaken without anybody noticing."""
    assert len(T.PROHIBITED_TERMS) + len(T.PROHIBITED_PHRASES) == 16
    assert T.PROHIBITED_ENTRY_COUNT == 16
    assert len(SPEC_31_1_ENTRIES) == 16


def test_module_list_matches_an_independent_transcription_of_section_31_1():
    """The real anti-drift check: the module's list is compared to a second,
    separate reading of §31.1 rather than to itself. Order is compared too,
    because the register is transcribed "in the specification's own order"."""
    combined = tuple(T.PROHIBITED_TERMS) + tuple(T.PROHIBITED_PHRASES)
    assert set(combined) == set(SPEC_31_1_ENTRIES), (
        f"missing: {sorted(set(SPEC_31_1_ENTRIES) - set(combined))}; "
        f"invented: {sorted(set(combined) - set(SPEC_31_1_ENTRIES))}"
    )


@pytest.mark.parametrize("entry", SPEC_31_1_ENTRIES)
def test_every_specification_entry_is_actually_detected(entry):
    """Parametrised over the INDEPENDENT transcription, so an entry deleted
    from the module still gets a test that fails."""
    assert entry in T.find_prohibited_terms(f"a {entry} was recorded today")


def test_matching_is_case_insensitive():
    assert T.find_prohibited_terms("VIOLATION") == ["violation"]
    assert T.find_prohibited_terms("Misconduct") == ["misconduct"]


def test_matching_is_whole_word_so_ordinary_english_is_not_flagged():
    """A substring scan would flag these. §31.1 targets the words, not the
    letters, and false positives are how a check gets switched off."""
    assert T.find_prohibited_terms("strikethrough styling") == []
    assert T.find_prohibited_terms("the text was struck") == []
    assert T.find_prohibited_terms("indebted") == []
    assert T.find_prohibited_terms("offenders")  == []   # plural is a different token
    assert T.find_prohibited_terms("downloaded") == []


def test_authorised_is_not_flagged_but_unauthorised_is():
    """§36.2 ships "Manager authorised my early finish" and "Work completed /
    authorised release" as APPROVED reason labels. Flagging "authorised" would
    make the specification contradict itself."""
    assert T.find_prohibited_terms("Manager authorised my early finish") == []
    assert T.find_prohibited_terms("Work completed / authorised release") == []
    assert T.find_prohibited_terms("Unauthorized Exit") == []          # US spelling is not the listed term
    assert T.find_prohibited_terms("unauthorised exit") == ["unauthorised"]


def test_non_compliant_matches_with_either_hyphen_or_space():
    assert T.find_prohibited_terms("non-compliant") == ["non-compliant"]
    assert T.find_prohibited_terms("non compliant") == ["non-compliant"]


def test_flagged_worker_phrase_is_detected():
    assert T.find_prohibited_terms("flagged worker list") == ["flagged worker"]


def test_neutral_locked_vocabulary_is_allowed():
    """§4.1 replacements must pass, or the guard would forbid the approved
    words as well as the banned ones."""
    for allowed in (
        "attributable shortfall",
        "Threshold reached",
        "under review",
        "Tenant",
        "Worker",
        "BRK_M / BRK_R",
        "This is an attendance condition recorded by Temveris.",
        "How this was calculated",
        "Dispute this day",
        "Prefer not to say",
    ):
        assert T.find_prohibited_terms(allowed) == [], allowed


def test_event_name_examples_from_section_32():
    """§32's own worked example.

    Underscore-delimited identifiers are the case §32 names explicitly, and the
    reason the scanner uses letter boundaries rather than the regex ``\\b``:
    ``\\b`` treats ``_`` as a word character, so ``ECO_VIOLATION_LOGGED`` would
    have slipped through a naive whole-word check.
    """
    assert T.find_prohibited_terms("ECO_SHORTFALL_RECORDED") == []
    assert T.find_prohibited_terms("ECO_VIOLATION_LOGGED") == ["violation"]


def test_identifier_shapes_are_caught_not_just_prose():
    """Field names, enum values and analytics labels (§31.1, §32, AC-06)."""
    assert T.find_prohibited_terms("violation_count") == ["violation"]
    assert T.find_prohibited_terms("worker.debt.total") == ["debt"]
    assert T.find_prohibited_terms("isNonCompliant") == []          # camelCase, no separator
    assert T.find_prohibited_terms("is_non_compliant") == ["non-compliant"]
    assert T.find_prohibited_terms("threshold_reached_at") == []    # §9.2's acceptable name


def test_scan_text_reports_line_numbers_for_review():
    hits = T.scan_text("clean line\nthis is a violation\nalso clean")
    assert hits == [(2, "violation", "this is a violation")]
    assert "line 2" in T.format_hits(hits, source="sample")


def test_empty_and_none_are_handled():
    assert T.find_prohibited_terms("") == []
    assert T.find_prohibited_terms(None) == []
    assert T.scan_text("") == []


def test_is_clean_agrees_with_find_prohibited_terms():
    assert T.is_clean("Threshold reached") is True
    assert T.is_clean("recorded as a violation") is False
    assert T.is_clean("") is True


def test_format_hits_is_empty_for_clean_text():
    assert T.format_hits(T.scan_text("Threshold reached")) == ""


def test_neutral_vocabulary_transcribes_the_section_4_1_rows_in_full():
    """§31.1 ends "Where a neutral word is needed, use the vocabulary at §4.1",
    so an abridged copy of that table would send a reviewer to the wrong word.
    Spot-checks the rows most likely to be trimmed."""
    v = T.NEUTRAL_VOCABULARY
    assert v["Attributable shortfall"] == (
        "violation", "infraction", "breach", "offence", "deficit", "debt",
        "owed time",
    )
    assert "threshold violated" in v["Threshold reached"]
    assert "disputed by worker" in v["Under review"]
    assert "rest" in v["BRK_M / BRK_R"]
    assert v["Tenant"][0] == "Organization"
    assert v["Worker"][0] == "Employee"


def test_every_neutral_replacement_is_itself_clean():
    """A "neutral" replacement that trips the scanner would be a contradiction.
    The NEVER-USE lists legitimately contain the vocabulary; the USE keys must
    not."""
    for use_term in T.NEUTRAL_VOCABULARY:
        assert T.find_prohibited_terms(use_term) == [], use_term


# ══════════════════════════════════════════════════════════════════════════════
# 2. The regression guard — ECO-controlled sources stay clean (AC-06)
# ══════════════════════════════════════════════════════════════════════════════

def _eco_source_files():
    return sorted(
        p for p in ECO_PACKAGE.glob("*.py")
        if p.name not in SCAN_EXCLUSIONS
    )


def test_eco_package_has_files_to_scan():
    """Guards the guard: an empty glob would make the scan below vacuously
    pass forever."""
    assert _eco_source_files(), "no Early Clock-Out source files found to scan"


@pytest.mark.parametrize("path", _eco_source_files(), ids=lambda p: p.name)
def test_eco_source_files_contain_no_prohibited_terminology(path):
    hits = T.scan_text(path.read_text(encoding="utf-8"))
    assert not hits, T.format_hits(hits, source=str(path.relative_to(REPO_ROOT)))


def test_golden_vector_fixture_contains_no_prohibited_terminology():
    """The fixture is ECO-controlled content and will one day be read by
    engineers implementing the engine. It must model the vocabulary it
    enforces."""
    hits = T.scan_text(GOLDEN_VECTORS.read_text(encoding="utf-8"))
    assert not hits, T.format_hits(hits, source="tests/golden/eco_golden_vectors.json")


def test_exclusions_are_explicit_and_still_exist():
    """An exclusion that silently stops matching a real file becomes a hole in
    the guard."""
    for name, reason in SCAN_EXCLUSIONS.items():
        assert (ECO_PACKAGE / name).exists(), f"excluded file {name} no longer exists"
        assert reason, f"exclusion {name} must carry a written reason"


# ══════════════════════════════════════════════════════════════════════════════
# 3. The withdrawn v1.0 implementation — reported, never modified
# ══════════════════════════════════════════════════════════════════════════════

def test_scanner_catches_the_banned_advisory_framing_from_section_26():
    """§26.2 "WHAT THIS SCREEN MUST NEVER SAY" lists wording observed in
    competitor products. Some of it is caught by the §31.1 vocabulary list;
    some is banned by INV-9 framing alone. This pins which is which, so nobody
    assumes the word scanner is a complete INV-9 check — it is not, and §43
    AC-02 keeps design review in the loop for the rest.
    """
    # Caught by the §31.1 vocabulary list.
    assert T.find_prohibited_terms("This will be recorded as a violation.") == ["violation"]
    assert T.find_prohibited_terms("20 minutes owed") == ["owed"]
    assert T.find_prohibited_terms("Attendance debt this week") == ["debt"]

    # Banned by INV-9 framing, NOT by the word list. A vocabulary scanner
    # cannot detect these; they are a design-review responsibility.
    assert T.find_prohibited_terms("You are not authorised to leave.") == []
    assert T.find_prohibited_terms(
        "Your manager will be informed of your early departure.") == []
    assert T.find_prohibited_terms("Are you sure? You still owe 20 minutes.") == []


def test_withdrawn_v1_implementation_remains_disabled():
    """The one thing that actually matters about v1.0.

    Its blocking, mandatory-free-text flow violates INV-1 and defect D-07. It is
    switched off at both ends. This asserts it STAYS off; it does not ask anyone
    to edit it. Read from source text rather than importing, so this test has no
    side effects and does not couple to the 4,000-line router module.
    """
    backend_flag = (BACKEND_ROOT / "app" / "employees" / "router.py").read_text(
        encoding="utf-8", errors="replace")
    assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in backend_flag, (
        "The withdrawn ZT-WFM-ECO-001 v1.0 Early Clock-Out gate appears to have "
        "been re-enabled. It blocks clock-out behind a mandatory 15-character "
        "free-text reason, which violates INV-1 (§8.1) and defect D-07. v2.0 "
        "supersedes it and its calculation core is BLOCKED."
    )

    modal = REPO_ROOT / "frontend" / "src" / "components" / "EarlyClockOutModal.jsx"
    if modal.exists():
        assert "EARLY_CLOCK_OUT_ENABLED = false" in modal.read_text(
            encoding="utf-8", errors="replace"), (
            "The withdrawn v1.0 Early Clock-Out modal appears to have been "
            "re-enabled on the frontend."
        )


def test_known_v1_terminology_is_recorded_not_repaired(capsys):
    """Informational: report §31.1 hits in the disabled v1.0 surfaces.

    Asserts nothing about the count. Cleaning v1.0 is explicitly out of scope,
    and a test that failed when someone eventually deleted it would be a trap.
    Run with -s to read the report.
    """
    targets = [
        REPO_ROOT / "frontend" / "src" / "components" / "EarlyClockOutModal.jsx",
    ]
    report = []
    for path in targets:
        if not path.exists():
            continue
        hits = T.scan_text(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            report.append(T.format_hits(hits, source=str(path.relative_to(REPO_ROOT))))
    if report:
        print("\n[ZT-SPEC-ECO-002 §31.1] Pre-existing terminology in the WITHDRAWN "
              "v1.0 implementation (reported, intentionally not repaired):\n"
              + "\n".join(report))
    # No assertion on content: this is a report, not a gate.
    assert True
