"""
ZT-SPEC-ECO-002 §31 — Locked Copy Register validation.
=======================================================

§31: "These strings are legally load-bearing. They MUST NOT be paraphrased,
shortened for layout, or rewritten by a content pass without Legal review."

AC-20 requires "Locked strings match the register exactly in the default
locale". There is no locale layer and no surface yet — the surfaces are BLOCKED
— so what is asserted here is the register itself: that all twelve keys exist,
that each string is character-for-character what the specification says, and
that nothing has been quietly reworded.

The expected values below are an INDEPENDENT transcription from §31. They are
deliberately duplicated rather than imported, because a test that compares the
register to itself proves nothing. If these two transcriptions ever disagree,
one of them drifted, and that is exactly the event this file exists to catch.
"""

from __future__ import annotations

import pytest

from app.early_clock_out import copy_register as C
from app.early_clock_out import terminology as T

# ── §31 table, transcribed independently of copy_register.py ─────────────────
EXPECTED_LOCKED_COPY = {
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


# ══════════════════════════════════════════════════════════════════════════════
# Completeness
# ══════════════════════════════════════════════════════════════════════════════

def test_register_has_exactly_the_twelve_specification_keys():
    assert set(C.LOCKED_COPY) == set(EXPECTED_LOCKED_COPY)
    assert len(C.LOCKED_COPY) == 12


def test_no_key_is_missing_and_none_was_invented():
    missing = set(EXPECTED_LOCKED_COPY) - set(C.LOCKED_COPY)
    extra = set(C.LOCKED_COPY) - set(EXPECTED_LOCKED_COPY)
    assert not missing, f"locked strings missing from the register: {sorted(missing)}"
    assert not extra, f"strings invented that §31 does not define: {sorted(extra)}"


def test_no_duplicate_key_literal_in_the_register_source():
    """A dict literal silently keeps the LAST value for a repeated key, so a
    copy-paste slip could overwrite a locked string with no error anywhere.
    Comparing ``len(dict)`` to ``len(set(dict))`` cannot catch that — it is
    true by construction. Parsing the source is the only real check.
    """
    import ast
    import inspect

    wanted = ("LOCKED_COPY", "LOCKED_COPY_SURFACES")
    tree = ast.parse(inspect.getsource(C))
    checked = []
    for node in ast.walk(tree):
        # Both registers are ANNOTATED assignments (`X: Dict[str, str] = {...}`),
        # which parse to ast.AnnAssign rather than ast.Assign. Handling only
        # ast.Assign silently inspected nothing and passed vacuously.
        if isinstance(node, ast.AnnAssign):
            targets = [node.target.id] if isinstance(node.target, ast.Name) else []
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        else:
            continue
        name = next((t for t in targets if t in wanted), None)
        if name is None:
            continue
        assert isinstance(node.value, ast.Dict), f"{name} is not a dict literal"
        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        assert not duplicates, f"{name} contains duplicate key literal(s): {duplicates}"
        assert len(keys) == 12, f"{name} has {len(keys)} key literals, expected 12"
        checked.append(name)
    assert sorted(checked) == sorted(wanted), (
        f"expected to inspect both registers, inspected: {checked}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Exactness — spelling, capitalisation, punctuation, typography
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("key", sorted(EXPECTED_LOCKED_COPY))
def test_each_locked_string_is_character_for_character_correct(key):
    assert C.LOCKED_COPY[key] == EXPECTED_LOCKED_COPY[key]


def test_typographic_apostrophes_are_preserved_not_normalised():
    """A content pass or an editor's autocorrect turning U+2019 into an ASCII
    apostrophe is exactly the silent rewording §31 forbids, and would break an
    exact-match locale check."""
    assert "’" in C.LOCKED_COPY["eco.advisory.title"]
    assert "’" in C.LOCKED_COPY["eco.advisory.threshold"]
    assert "’" in C.LOCKED_COPY["eco.degraded.body"]
    assert "'" not in C.LOCKED_COPY["eco.advisory.title"]
    assert "'" not in C.LOCKED_COPY["eco.degraded.body"]


def test_em_dash_is_preserved_in_the_personal_reason_label():
    assert "—" in C.LOCKED_COPY["eco.reason.personal"]
    assert "-" not in C.LOCKED_COPY["eco.reason.personal"]


def test_no_string_is_empty_or_padded():
    for key, value in C.LOCKED_COPY.items():
        assert value, f"{key} is empty"
        assert value == value.strip(), f"{key} has stray leading/trailing whitespace"


def test_threshold_status_wording_is_the_locked_form():
    """§4.1 locks this: "threshold breached", "threshold violated", "over limit"
    and "non-compliant" are all banned alternatives."""
    assert C.LOCKED_COPY["eco.status.threshold"] == "Threshold reached"


# ══════════════════════════════════════════════════════════════════════════════
# §31.1 interaction — the one mandated exemption
# ══════════════════════════════════════════════════════════════════════════════

def test_every_locked_string_except_the_admin_disclaimer_is_terminology_clean():
    for key, value in C.LOCKED_COPY.items():
        if key in C.LOCKED_COPY_TERMINOLOGY_EXEMPT:
            continue
        assert T.find_prohibited_terms(value) == [], (
            f"{key} contains §31.1 terminology: {T.find_prohibited_terms(value)}"
        )


def test_the_exemption_is_exactly_one_key_and_is_the_admin_disclaimer():
    assert C.LOCKED_COPY_TERMINOLOGY_EXEMPT == ("eco.admin.disclaimer",)


def test_the_disclaimer_uses_misconduct_only_to_negate_it():
    """§29.1 / §33.4 mandate this string verbatim; INV-9 is served BY it, not
    breached by it. Pinning the negating construction stops a well-meaning
    rewrite that drops the word — and with it the legal protection."""
    disclaimer = C.LOCKED_COPY["eco.admin.disclaimer"]
    assert "It is not a finding of misconduct" in disclaimer
    assert "carries no recommendation" in disclaimer
    assert "must be made by a person" in disclaimer
    assert T.find_prohibited_terms(disclaimer) == ["misconduct"]


# ══════════════════════════════════════════════════════════════════════════════
# Surface mapping (§31 "WHERE" column)
# ══════════════════════════════════════════════════════════════════════════════

def test_every_locked_string_has_a_recorded_surface():
    assert set(C.LOCKED_COPY_SURFACES) == set(C.LOCKED_COPY)
    for key, surface in C.LOCKED_COPY_SURFACES.items():
        assert surface, f"{key} has no surface recorded"


def test_degraded_body_belongs_to_the_degraded_surface():
    assert C.LOCKED_COPY_SURFACES["eco.degraded.body"] == "WF-ECO-02D"


def test_admin_disclaimer_covers_notifications_not_just_the_review_screen():
    """§33.4: mandatory in every administrator notification, in the body, not a
    footer."""
    surface = C.LOCKED_COPY_SURFACES["eco.admin.disclaimer"]
    assert "WF-ECO-04" in surface
    assert "notification" in surface


# ══════════════════════════════════════════════════════════════════════════════
# §36.3 / §36.4 locked notices
# ══════════════════════════════════════════════════════════════════════════════

def test_free_text_notice_matches_the_specification():
    assert C.REASON_FREE_TEXT_NOTICE == (
        "Please don’t include health, medical or other sensitive personal "
        "details here. If your absence relates to health, submit a leave request "
        "instead."
    )


def test_free_text_cap_is_280_characters():
    assert C.REASON_FREE_TEXT_MAX_CHARS == 280


def test_guidance_line_matches_the_specification():
    assert C.REASON_GUIDANCE_LINE == (
        "You never have to explain a personal reason. Choosing ‘Personal "
        "reason—no detail required’ or ‘Prefer not to say’ has "
        "exactly the same effect as any other option."
    )


def test_guidance_line_states_the_no_penalty_equivalence():
    """§36.4's purpose is "to remove the pressure that produces
    over-disclosure". The equivalence clause is the operative part."""
    assert "exactly the same effect as any other option" in C.REASON_GUIDANCE_LINE
    assert "never have to explain" in C.REASON_GUIDANCE_LINE


def test_locked_notices_carry_no_prohibited_terminology():
    assert T.find_prohibited_terms(C.REASON_FREE_TEXT_NOTICE) == []
    assert T.find_prohibited_terms(C.REASON_GUIDANCE_LINE) == []


def test_transcription_discrepancy_between_section_31_and_section_36_4_is_preserved():
    """OBSERVATION, NOT A CORRECTION.

    §31 renders the option as "Personal reason — no detail required" (spaced
    em dash); §36.4 quotes it as "Personal reason—no detail required"
    (unspaced). Both are reproduced exactly as they appear in their own
    sections. Normalising either would be an unauthorised edit to a locked
    string, so the difference is pinned here and referred to Legal / the
    Cross-Asset Consistency Sheet rather than silently resolved.
    """
    assert C.LOCKED_COPY["eco.reason.personal"] == "Personal reason — no detail required"
    assert "Personal reason—no detail required" in C.REASON_GUIDANCE_LINE
    assert C.LOCKED_COPY["eco.reason.personal"] not in C.REASON_GUIDANCE_LINE


# ══════════════════════════════════════════════════════════════════════════════
# The register stays a register
# ══════════════════════════════════════════════════════════════════════════════

def test_register_is_data_only_and_is_not_wired_to_any_surface():
    """The register is CONSUMED by a surface; it must never BE one.

    The deferred and §28.1 status copy is authoritative and is now rendered
    through the visibility surface — ``eco_day_view`` reads these registers and
    serves them at ``/api/eco/day`` for WF-ECO-03. That makes this guard more
    load-bearing, not less: the rendering lives in the renderer, and this module
    stays declarative data with no request handling, no rendering and no
    persistence, so there is exactly one place a label can come from.

    Surfaces that remain unimplemented — WF-ECO-04 among them — change nothing
    here; they will consume the same register the same way.
    """
    import inspect
    src = inspect.getsource(C)
    for forbidden in ("APIRouter", "@router", "Depends(", "db.query",
                      "session.add", "render_template", "jsonify",
                      "HTTPException", "sqlalchemy", "Column("):
        assert forbidden not in src, (
            f"copy_register.py appears to be growing behaviour ({forbidden!r}); "
            "it must remain declarative data that surfaces read, never a surface"
        )


# ══════════════════════════════════════════════════════════════════════════════
# §28.1 status vocabulary — owner-authorized, and NOT part of the §31 register
# ══════════════════════════════════════════════════════════════════════════════
# GV-17 (§24.7) needs a per-day Status label for a deferred day. §28.1 states
# the six-value Status vocabulary is "locked ... (§31)" while §31 registers only
# `eco.status.threshold` among them — a specification defect, recorded rather
# than silently resolved.
#
# The owner authorized ONE of those six labels, "Deferred". It lives in its own
# register so the §31 guards above keep asserting "exactly the twelve §31 keys"
# WITHOUT amendment: the anti-drift mechanism is extended, never relaxed.
#
# Transcribed independently here for the same reason the §31 table is — a test
# that compares a register to itself proves nothing.
#: All SIX §28.1 status values, transcribed independently of the register.
#: ``Deferred`` was authorized first, for GV-17; the owner later ruled the
#: remaining five verbatim, completing the vocabulary §28.1 describes.
EXPECTED_SECTION_28_1_STATUS_COPY = {
    "eco.status.complete":       "Complete",
    "eco.status.counted":        "Counted",
    "eco.status.deferred":       "Deferred",
    "eco.status.excluded":       "Excluded",
    "eco.status.notaworkingday": "Not a working day",
    "eco.status.underreview":    "Under review",
}


def test_the_section_31_register_is_untouched_by_the_28_1_additions():
    """The load-bearing claim. §31 still means exactly §31.

    Six labels have now been added under §28.1 authority and NONE of them
    entered ``LOCKED_COPY``. This is not a §31 amendment.
    """
    assert len(C.LOCKED_COPY) == 12
    assert len(C.LOCKED_COPY_SURFACES) == 12
    for key in EXPECTED_SECTION_28_1_STATUS_COPY:
        assert key not in C.LOCKED_COPY, key
        assert key not in C.LOCKED_COPY_SURFACES, key
    assert set(C.LOCKED_COPY) & set(C.SECTION_28_1_STATUS_COPY) == set(), (
        "a key is registered under both §31 and §28.1 authority; exactly one "
        "source must be authoritative for any given string")


def test_all_six_status_labels_are_registered_exactly_once():
    assert set(C.SECTION_28_1_STATUS_COPY) == set(
        EXPECTED_SECTION_28_1_STATUS_COPY)
    assert len(C.SECTION_28_1_STATUS_COPY) == 6


@pytest.mark.parametrize("key", sorted(EXPECTED_SECTION_28_1_STATUS_COPY))
def test_each_status_label_is_character_for_character_correct(key):
    """Anti-drift: these are the §28.1 values, not paraphrases of them.

    Parametrised so a failure names the label that drifted rather than
    reporting a whole-dict mismatch.
    """
    assert C.SECTION_28_1_STATUS_COPY[key] == \
        EXPECTED_SECTION_28_1_STATUS_COPY[key]


def test_no_status_label_carries_stray_whitespace_or_casing_drift():
    """Capitalisation and whitespace are part of the approved string.

    "Under review" is sentence case, not Title Case; "Not a working day"
    likewise. A well-meaning title-casing pass would change approved copy.
    """
    for key, value in C.SECTION_28_1_STATUS_COPY.items():
        assert value == value.strip() and value != "", key
        assert "  " not in value, key
        assert value[0].isupper(), key
        # Only the first word is capitalised in the multi-word labels.
        rest = value.split()[1:]
        assert all(w == w.lower() for w in rest), (
            f"{key} looks title-cased: {value!r}")


def test_the_whole_register_matches_the_independent_transcription():
    """Whole-dict equality, so an EXTRA key fails here as well as a changed
    one — the parametrised test above only covers expected keys."""
    assert C.SECTION_28_1_STATUS_COPY == EXPECTED_SECTION_28_1_STATUS_COPY


def test_no_key_may_be_registered_without_explicit_owner_authorization():
    """Prevents arbitrary future additions.

    Being listed in §28.1 does NOT make a label production-approved — the owner
    ruled that explicitly, and each of the six was authorized by its own
    decision. A seventh key requires editing ``SECTION_28_1_AUTHORIZED_KEYS``,
    which is a visible, reviewable act rather than a dict entry nobody notices.
    """
    assert C.SECTION_28_1_AUTHORIZED_KEYS == (
        "eco.status.complete",
        "eco.status.counted",
        "eco.status.deferred",
        "eco.status.excluded",
        "eco.status.notaworkingday",
        "eco.status.underreview",
    )
    assert set(C.SECTION_28_1_STATUS_COPY) == set(C.SECTION_28_1_AUTHORIZED_KEYS)
    assert len(C.SECTION_28_1_AUTHORIZED_KEYS) == len(
        set(C.SECTION_28_1_AUTHORIZED_KEYS)), "duplicate authorized key"


def test_the_six_labels_are_exactly_the_28_1_vocabulary():
    """No seventh status was invented, and none of the six was dropped."""
    assert set(C.SECTION_28_1_STATUS_COPY.values()) == {
        "Complete", "Counted", "Deferred", "Excluded",
        "Not a working day", "Under review"}


def test_every_status_key_uses_the_three_segment_convention():
    """``eco.<group>.<name>`` — the convention every registered key follows."""
    for key in C.SECTION_28_1_STATUS_COPY:
        parts = key.split(".")
        assert len(parts) == 3, f"{key} is not three segments"
        assert parts[0] == "eco" and parts[1] == "status", key
        assert key == key.lower(), key
        assert not any(ch in key for ch in "_- "), key


def test_every_status_label_has_a_recorded_surface():
    """Same invariant §31's WHERE column carries, applied to this register."""
    assert set(C.SECTION_28_1_STATUS_SURFACES) == set(C.SECTION_28_1_STATUS_COPY)
    for key, surface in C.SECTION_28_1_STATUS_SURFACES.items():
        # §28.1 fixes the vocabulary for WF-ECO-03; §29's WF-ECO-04 table shows
        # the same Status column with the same values.
        assert "WF-ECO-03" in surface and "WF-ECO-04" in surface, key


def test_the_status_labels_carry_no_prohibited_terminology():
    """§31.1 applies to this register too, with NO exemption.

    The one mandated exemption is ``eco.admin.disclaimer`` under §31, and it
    does not extend here: nothing in §28.1 mandates a prohibited term.
    """
    for key, value in C.SECTION_28_1_STATUS_COPY.items():
        assert T.find_prohibited_terms(value) == [], (
            f"{key} contains §31.1 terminology: {T.find_prohibited_terms(value)}")
        assert key not in C.LOCKED_COPY_TERMINOLOGY_EXEMPT, key
    assert C.LOCKED_COPY_TERMINOLOGY_EXEMPT == ("eco.admin.disclaimer",)


def test_only_deferred_carries_an_explanatory_body():
    """The owner ruled the other five label-only.

    An explanatory sentence for a status nobody asked to have explained would
    be invented copy, so the asymmetry is pinned rather than left to drift.
    """
    assert C.SECTION_28_1_STATUSES_WITH_BODY == ("eco.status.deferred",)
    assert set(C.SECTION_28_1_STATUSES_WITH_BODY) <= set(
        C.SECTION_28_1_STATUS_COPY)
    # TWO bodies exist and BOTH are deferred bodies — owner ruling D-2 added a
    # second when D-1 admitted a second deferral CONDITION. The §28.1 LABEL is
    # still one ("Deferred"); what doubled is the explanation, because the
    # original names the exit and would be false for a classification
    # deferral. The asymmetry this guard pins is unchanged: no OTHER status
    # gained a body.
    assert set(C.OWNER_AUTHORIZED_COPY) == {
        "eco.deferred.body", "eco.classification_deferred.body"}
    for status in C.SECTION_28_1_STATUS_COPY:
        if status in C.SECTION_28_1_STATUSES_WITH_BODY:
            continue
        stem = status.rsplit(".", 1)[-1]
        assert f"eco.{stem}.body" not in C.OWNER_AUTHORIZED_COPY, stem


# ══════════════════════════════════════════════════════════════════════════════
# Owner-authored production copy — a THIRD authority, neither §31 nor §28.1
# ══════════════════════════════════════════════════════════════════════════════
# §24.7 requires a deferred day to be EXPLAINABLE, not merely labelled, and no
# sentence for that exists in §24.7, §28, §29, §31, §32 or §33. The owner
# authored and approved one for GV-17.
#
# It is registered under OWNER authority, not §31 — no external §31 approval was
# obtained — and the tests below hold it to everything §31 copy is held to:
# character-for-character fidelity against an independent transcription, §31.1
# terminology with NO exemption, and a recorded surface.
EXPECTED_OWNER_AUTHORIZED_COPY = {
    "eco.deferred.body":
        "Your exit is still being resolved, so today’s shortfall is not "
        "final; while this day is deferred, it contributes 0 minutes to your "
        "weekly total and will be evaluated when the exit is resolved.",
    # Owner ruling D-2. Authored separately rather than by generalising the
    # body above: that one names the EXIT twice, and a Worker whose exit was
    # verified hours ago must not be told it is still being resolved.
    "eco.classification_deferred.body":
        "Your working-time classification is still being resolved, so "
        "today’s shortfall is not final; while this day is deferred, it "
        "contributes 0 minutes to your weekly total and will be evaluated "
        "when the classification is resolved.",
}


def test_the_owner_authorized_body_is_registered_exactly_once():
    assert set(C.OWNER_AUTHORIZED_COPY) == set(EXPECTED_OWNER_AUTHORIZED_COPY)
    assert len(C.OWNER_AUTHORIZED_COPY) == 2
    assert C.OWNER_AUTHORIZED_KEYS == (
        "eco.deferred.body", "eco.classification_deferred.body")
    assert set(C.OWNER_AUTHORIZED_COPY) == set(C.OWNER_AUTHORIZED_KEYS)


def test_the_owner_authorized_body_is_character_for_character_correct():
    """Anti-drift on the approved wording. Independent transcription above.

    The typographic apostrophe in "today’s" (U+2019) is load-bearing: a
    straight quote is a different string, and normalising it would be an
    unauthorised edit to approved copy.
    """
    assert C.OWNER_AUTHORIZED_COPY == EXPECTED_OWNER_AUTHORIZED_COPY
    for key in C.OWNER_AUTHORIZED_KEYS:
        body = C.OWNER_AUTHORIZED_COPY[key]
        assert "’" in body and "'" not in body, key
        assert body == body.strip() and body.endswith("."), key

    # The two bodies are NOT interchangeable (D-2). Each names the condition it
    # explains and neither names the other's.
    exit_body = C.OWNER_AUTHORIZED_COPY["eco.deferred.body"]
    classification_body = C.OWNER_AUTHORIZED_COPY[
        "eco.classification_deferred.body"]
    assert exit_body != classification_body
    assert "exit" in exit_body and "classification" not in exit_body
    assert ("classification" in classification_body
            and "exit" not in classification_body)


def test_the_approved_body_says_all_four_required_things():
    """The four points the owner required the wording to communicate. Asserted
    against the registered value, so a later reword that drops one fails."""
    body = C.OWNER_AUTHORIZED_COPY["eco.deferred.body"]
    assert "exit is still being resolved" in body          # pending/unverified
    assert "shortfall is not final" in body                # not final
    assert "contributes 0 minutes" in body                 # contributes 0
    assert "evaluated when the exit is resolved" in body   # evaluates on resolution
    # Must not imply the day is excused (§24.7: "neither counted nor excused").
    assert "excus" not in body.lower()


def test_the_approved_body_carries_no_prohibited_terminology():
    """§31.1 applies with NO exemption. The one mandated exemption belongs to
    ``eco.admin.disclaimer`` under §31 and does not extend to this register."""
    for key, value in C.OWNER_AUTHORIZED_COPY.items():
        assert T.find_prohibited_terms(value) == [], (
            f"{key} contains §31.1 terminology: {T.find_prohibited_terms(value)}")
    assert "eco.deferred.body" not in C.LOCKED_COPY_TERMINOLOGY_EXEMPT


def test_the_approved_body_has_a_recorded_surface():
    assert set(C.OWNER_AUTHORIZED_SURFACES) == set(C.OWNER_AUTHORIZED_COPY)
    surface = C.OWNER_AUTHORIZED_SURFACES["eco.deferred.body"]
    assert "WF-ECO-03" in surface and "WF-ECO-04" in surface


def test_the_three_registers_stay_separate_and_correctly_attributed():
    """The load-bearing claim: each string says where its authority came from.

    §31 is still exactly §31. The owner-authored body is NOT in it and must
    never be described as §31 production copy.
    """
    assert len(C.LOCKED_COPY) == 12
    assert "eco.deferred.body" not in C.LOCKED_COPY
    assert "eco.deferred.body" not in C.SECTION_28_1_STATUS_COPY
    assert "eco.status.deferred" not in C.OWNER_AUTHORIZED_COPY
    registers = (C.LOCKED_COPY, C.SECTION_28_1_STATUS_COPY,
                 C.OWNER_AUTHORIZED_COPY)
    for i, a in enumerate(registers):
        for b in registers[i + 1:]:
            assert set(a) & set(b) == set(), (
                "a key is registered under two authorities; exactly one source "
                "must be authoritative for any given string")


def test_no_placeholder_or_draft_value_exists_in_any_register():
    """The gate's original purpose, still enforced now that copy has landed:
    nothing empty, nothing provisional, nothing awaiting approval."""
    for register in (C.LOCKED_COPY, C.SECTION_28_1_STATUS_COPY,
                     C.OWNER_AUTHORIZED_COPY):
        for key, value in register.items():
            assert value.strip(), f"{key} holds an empty production value"
            assert "TODO" not in value.upper(), key
            assert "PLACEHOLDER" not in value.upper(), key
            assert "<" not in value and ">" not in value, key


def test_the_status_vocabulary_is_complete_and_lives_under_28_1_authority():
    """Owner scope moved: Deferred-only, then the remaining five verbatim.

    All six now exist — which is what makes a Status column buildable — and all
    six sit in the §28.1 register. None entered §31, and none acquired a body
    beyond the deferred ones §24.7 and owner ruling D-2 required.
    """
    for label in ("Complete", "Counted", "Deferred", "Excluded",
                  "Under review", "Not a working day"):
        assert label in C.SECTION_28_1_STATUS_COPY.values(), label
        assert label not in C.LOCKED_COPY.values(), label
    assert len(C.SECTION_28_1_STATUS_COPY) == 6
    # The LABEL vocabulary is still six. The owner-authorized bodies are two,
    # and both explain the SAME label: D-1 admitted a second deferral condition
    # and D-2 authored a body for it rather than reusing one that names the
    # exit. No seventh label appeared, and no other status gained a body.
    assert len(C.OWNER_AUTHORIZED_COPY) == 2
    assert all(k.endswith("deferred.body") for k in C.OWNER_AUTHORIZED_COPY)


def test_the_28_1_register_stays_declarative_data():
    """Same rule as the §31 register: data, never behaviour."""
    assert isinstance(C.SECTION_28_1_STATUS_COPY, dict)
    assert isinstance(C.SECTION_28_1_STATUS_SURFACES, dict)
    assert isinstance(C.SECTION_28_1_AUTHORIZED_KEYS, tuple)
    for value in C.SECTION_28_1_STATUS_COPY.values():
        assert isinstance(value, str)
