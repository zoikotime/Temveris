"""
ZT-SPEC-ECO-002 §42 — golden vector fixture validation, and execution of eight.
===============================================================================

WHAT THIS FILE RUNS, AND WHAT IT STILL DOES NOT
-----------------------------------------------
Section 5 EXECUTES six vectors — GV-10, GV-11, GV-13, GV-14, GV-15 and
GV-21 — against the authoritative services, because the primitives their
declared blockers named now exist: minute-granular leave with MODEL-EXCLUSION
(P-4), historically resolvable configuration versioning (P-5, for GV-21's stated
expectation only), and the forward-only Work Ledger producer (PRODUCER, D18-F8)
for the two DST vectors.

GV-12 and GV-28 are executable too, and each is executed where the behaviour it
pins actually lives: ``test_eval_week_fold.py::TestGV12`` with the week fold,
and ``test_working_day_classifier.py::TestGV28`` with the working-day
classifier. The guard at the end of this file names both explicitly, so neither
can be declared executable on the strength of a passing mention in this file.

The other twenty are still not run. P-2 has no deterministic classifier
emitting BRK_M/BRK_R, there is no EVAL-EVENT, and no netting, grace or horizon
model.

Executing any vector against the platform's session-aggregate worked time
— ``(clock_out - clock_in) - break_seconds - offline_seconds`` — is the
construction §7.2 lists FIRST among prohibitions, and is defect D-03, for which
the v1.0 draft was withdrawn. The four executed below do not touch it: they go
through the sealed ledger, the leave authority and the versioned configuration
store, and an import-graph test bars the legacy path from EVAL-DAY entirely.

ARTIFACT METADATA CHANGES ONLY BY OWNER AUTHORIZATION
-----------------------------------------------------
All 28 vectors once carried ``execution = "blocked_pending_platform_primitive"``.
Each has since been re-baselined to ``executable`` by a separate owner
authorization, the last being GV-26 under ruling D-9.1 — §24.15's platform
incident, whose absence had left ``platform_incident_raised`` a claim no test
could fail. The execution tests are the evidence for each decision, never a
substitute for it: a vector is promoted because an owner said so, and the
coupling below is what stops the artifact and the harness disagreeing about it.

WHAT THIS FILE DOES
-------------------
Protects the fixture from drift. §42 calls the vectors normative and defect D-21
records why they matter: "A deterministic engine specified without golden vectors
will be implemented as several mutually incompatible variants." Committing them
BEFORE an engine exists is the only way to guarantee the expected outputs were
not reverse-engineered from whatever the implementation happened to produce.

The values asserted below are transcribed independently from the §42 table, so
the fixture is compared against a second reading rather than against itself.
Vectors chosen for value-level pinning are the ones an implementer under
schedule pressure would be most tempted to bend.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest

GOLDEN_PATH = pathlib.Path(__file__).resolve().parent / "golden" / "eco_golden_vectors.json"

EXPECTED_IDS = tuple(f"GV-{n:02d}" for n in range(1, 29))

REQUIRED_VECTOR_FIELDS = (
    "id",
    "scenario",
    "inputs_verbatim",
    "expected_verbatim",
    "harness",
)

REQUIRED_HARNESS_FIELDS = ("execution", "blocking_dependencies")

#: The two execution states a vector may be in. Closed on purpose: a synonym
#: ("ready", "passed", "implemented", "unblocked", "complete") would let a
#: vector look runnable without a test that runs it.
EXECUTION_BLOCKED = "blocked_pending_platform_primitive"
EXECUTION_EXECUTABLE = "executable"
PERMITTED_EXECUTION_VALUES = frozenset({EXECUTION_BLOCKED, EXECUTION_EXECUTABLE})

# Every dependency code the fixture is allowed to cite, so a typo cannot invent
# a blocker that no audit established.
#
# THE ORIGINAL TEN ARE RETAINED EVEN WHERE UNUSED. P-1, P-3, P-4 and C-2 are no
# longer cited by any vector — the owner-gate audit established their platform
# primitives now exist — but they stay in the vocabulary so the artifact remains
# backward-compatible and a historical blocker can be re-cited without a schema
# change. A code being permitted is not a claim that anything is blocked on it.
_ORIGINAL_DEPENDENCY_CODES = {
    "P-1", "P-2", "P-3", "P-4", "P-5", "C-2",
    "MODEL-NETTING", "MODEL-GRACE", "MODEL-HORIZON", "MODEL-WORKWEEK",
}

#: Added by the metadata re-baseline, so the artifact can name the dependencies
#: the audit actually found rather than the primitives it used to cite.
_REBASELINE_DEPENDENCY_CODES = {
    "EVAL-EVENT",             # weekly threshold crossing / event emission
    "PRODUCER",               # production Work Ledger producer
    "MODEL-VARIANCE",         # schedule-variance mathematics (R12)
    "ALERT",                  # render + durably record an ECO notification
    "STATE-CONTRACT",         # R4 evaluation vocabulary contradiction
    "POLICY-VERSION",         # policy_version semantics and population
    # Worker Context -> requirement resolution. SATISFIED: classification-scoped
    # ConfigValues let one version carry a per-worker-type requirement, and the
    # provider selects between them with worker_context_as_of at the evaluation
    # date. Cites nothing since GV-27's promotion.
    "WORKER-CONTEXT-WIRING",
}

#: Added when the owner split ALERT, which had become one code for two
#: materially different capabilities: the record/render half is BUILT, while
#: delivery and non-Worker recipient resolution are deferred by rulings R6 and
#: R2 respectively. One code could not say that GV-06 and GV-22 are ready while
#: GV-07 is not, so ``ALERT`` was narrowed to record/render and the two
#: unresolved capabilities were named separately.
#:
#: They are their own group rather than more re-baseline codes because they are
#: independently closable — either could land without the other — and each is
#: gated by a different owner ruling.
_ALERT_SPLIT_DEPENDENCY_CODES = {
    # Deliver a recorded ECO notification to a recipient operationally, and
    # represent that delivery's outcome durably. Deferred by R6.
    "ALERT-DELIVERY",
    # Resolve an authoritative non-Worker recipient — the designated reviewer
    # or administrator — from an owner-ruled model rather than by inference.
    # Deferred by R2.
    "RECIPIENT-AUTHORITY",
}

#: Added when the owner ruled GV-17's §24.7 visibility requirement (ruling B3).
#:
#: GV-17 cited ALERT, and the ALERT/visibility audit established that was
#: wrong: §33.1's notification matrix contains NO row for a deferred day, §28.1
#: and §29 place the requirement on the WF-ECO-03 / WF-ECO-04 per-day Status
#: column, and §34 separates the explanation surface from delivery outright —
#: "a Worker who has notifications disabled still has the record, still has the
#: explanation surface". A deferred week also emits no threshold event, so
#: ALERT's entry point (which takes an ``eco_threshold_event.id``) could never
#: fire for it. Implementing ALERT in full would have left GV-17 exactly as
#: blocked as before.
#:
#: Two codes rather than one, for the same reason the ALERT split needed two:
#: GV-17's administrator half was blocked TWICE and independently — the surface
#: did not exist, AND there was no authority to decide who may open it. One
#: code could not have said that the second was built while the first was not,
#: which is exactly what the artifact had to record for the months between
#: them. Both are satisfied now and GV-17 is promoted, so the pair no longer
#: blocks anything; they are kept because the distinction they draw is real and
#: any future read surface will need both halves answered again.
_VISIBILITY_SPLIT_DEPENDENCY_CODES = {
    # Render per-day ECO state to the party entitled to see it (WF-ECO-03 /
    # WF-ECO-04). Satisfied when a day's status and its deferral explanation
    # are readable by an entitled caller.
    #
    # SATISFIED. Blocker B1 — "§31 registers no locked copy for the deferred
    # state" — was discharged when the owner authorized the deferred body and
    # registered the six §28.1 status labels, and ``app.eco_day_view`` now
    # serves both at ``/api/eco/day`` with WF-ECO-03 rendering the Worker's
    # half. The owner ruled that the authenticated, authority-protected read
    # surface discharges the administrator half too: the satisfaction clause
    # above asks for readability by an entitled caller, and WF-ECO-04 is a
    # weekly THRESHOLD review whose subject a deferred week never emits — so
    # requiring it would have repeated the ALERT mistake ruling B3 corrected.
    "VISIBILITY-SURFACE",
    # Determine authoritatively WHO may read a tenant's ECO records. Distinct
    # from RECIPIENT-AUTHORITY, which resolves who is TOLD about one. Satisfied
    # by ``app.eco_view_authority``.
    "VIEW-AUTHORITY",
}

KNOWN_DEPENDENCY_CODES = (
    _ORIGINAL_DEPENDENCY_CODES
    | _REBASELINE_DEPENDENCY_CODES
    | _ALERT_SPLIT_DEPENDENCY_CODES
    | _VISIBILITY_SPLIT_DEPENDENCY_CODES
)


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vectors(golden):
    return golden["vectors"]


@pytest.fixture(scope="module")
def by_id(vectors):
    return {v["id"]: v for v in vectors}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Completeness and identity
# ══════════════════════════════════════════════════════════════════════════════

def test_fixture_file_exists_and_is_valid_json(golden):
    assert isinstance(golden, dict)
    assert golden["_spec"] == "ZT-SPEC-ECO-002"
    assert golden["_spec_version"] == "2.0"


def test_exactly_twenty_eight_normative_vectors(vectors, golden):
    assert len(vectors) == 28
    assert golden["_vector_count"] == 28
    assert golden["_normative"] is True


def test_vector_ids_are_gv_01_through_gv_28(by_id):
    assert tuple(sorted(by_id)) == EXPECTED_IDS


def test_no_duplicate_vector_ids(vectors):
    ids = [v["id"] for v in vectors]
    assert len(ids) == len(set(ids)), "duplicate golden vector id"


def test_vectors_are_in_specification_order(vectors):
    assert [v["id"] for v in vectors] == list(EXPECTED_IDS)


def test_no_vector_is_silently_skipped(by_id):
    """A vector cannot be removed, blanked, or commented out to make a build
    green. Every id must be present with substantive content."""
    for gid in EXPECTED_IDS:
        v = by_id[gid]
        assert v["scenario"].strip(), f"{gid} has no scenario"
        assert v["inputs_verbatim"].strip(), f"{gid} has no inputs"
        assert v["expected_verbatim"].strip(), f"{gid} has no expected output"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Schema
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("gid", EXPECTED_IDS)
def test_every_vector_has_the_required_fields(by_id, gid):
    v = by_id[gid]
    for field in REQUIRED_VECTOR_FIELDS:
        assert field in v, f"{gid} missing {field}"
    for field in REQUIRED_HARNESS_FIELDS:
        assert field in v["harness"], f"{gid} harness missing {field}"


@pytest.mark.parametrize("gid", EXPECTED_IDS)
def test_blocking_dependencies_are_known_codes(by_id, gid):
    """Every cited code must be in the vocabulary, and the list must match the
    execution state.

    The emptiness rule is CONDITIONAL, not relaxed. An executable vector has
    nothing left to block it, so its list must be empty — carrying a stale
    blocker would misreport why it runs. A blocked vector must cite at least
    one, so "blocked" always says what for.
    """
    harness = by_id[gid]["harness"]
    deps = harness["blocking_dependencies"]
    assert isinstance(deps, list), f"{gid} blocking_dependencies is not a list"

    unknown = set(deps) - KNOWN_DEPENDENCY_CODES
    assert not unknown, f"{gid} cites unrecognised dependency code(s): {sorted(unknown)}"
    assert len(deps) == len(set(deps)), f"{gid} cites a duplicate dependency"

    if harness["execution"] == EXECUTION_EXECUTABLE:
        assert deps == [], (
            f"{gid} is executable but still cites {deps} — an executable vector "
            f"has no remaining blocker")
    else:
        assert deps, f"{gid} is blocked and must cite at least one blocker"


def test_normative_and_harness_data_are_kept_separate(by_id):
    """Execution status is metadata. It must never appear inside a normative
    field, or a future reader could mistake "blocked" for an expected output."""
    for gid, v in by_id.items():
        for field in ("scenario", "inputs_verbatim", "expected_verbatim"):
            assert "blocked_pending_platform_primitive" not in v[field], gid


# ══════════════════════════════════════════════════════════════════════════════
# 3. Execution state — a closed vocabulary, and an exact set
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("gid", EXPECTED_IDS)
def test_execution_is_one_of_the_two_permitted_values(by_id, gid):
    """No synonyms. A third value would let a vector describe itself as runnable
    without an execution test standing behind it."""
    assert by_id[gid]["harness"]["execution"] in PERMITTED_EXECUTION_VALUES, (
        f"{gid} carries an unrecognised execution value: "
        f"{by_id[gid]['harness']['execution']!r}")


def test_exactly_two_execution_values_are_permitted():
    assert PERMITTED_EXECUTION_VALUES == {
        "blocked_pending_platform_primitive", "executable"}
    assert len(PERMITTED_EXECUTION_VALUES) == 2
    for synonym in ("ready", "passed", "implemented", "unblocked", "complete",
                    "runnable", "green"):
        assert synonym not in PERMITTED_EXECUTION_VALUES


# ── Dependency vocabulary ────────────────────────────────────────────────────

def test_exactly_twenty_one_dependency_codes_are_permitted():
    """Seventeen, then nineteen when the owner split ALERT, now twenty-one.

    The fourth group came from owner ruling B3: GV-17's §24.7 visibility is
    discharged by the WF-ECO-03 / WF-ECO-04 explanation surfaces rather than by
    a notification, so VISIBILITY-SURFACE and VIEW-AUTHORITY were named and
    ALERT was removed from that vector.

    The groups are counted separately so a future addition has to say which one
    it belongs to, and the total is pinned so an addition cannot be slipped in
    without amending this number."""
    assert len(KNOWN_DEPENDENCY_CODES) == 21
    assert len(_ORIGINAL_DEPENDENCY_CODES) == 10
    assert len(_REBASELINE_DEPENDENCY_CODES) == 7
    assert len(_ALERT_SPLIT_DEPENDENCY_CODES) == 2
    assert len(_VISIBILITY_SPLIT_DEPENDENCY_CODES) == 2
    # The groups are disjoint: a code belongs to exactly one.
    groups = (_ORIGINAL_DEPENDENCY_CODES, _REBASELINE_DEPENDENCY_CODES,
              _ALERT_SPLIT_DEPENDENCY_CODES, _VISIBILITY_SPLIT_DEPENDENCY_CODES)
    assert sum(len(g) for g in groups) == len(KNOWN_DEPENDENCY_CODES)
    assert len(_ORIGINAL_DEPENDENCY_CODES) == 10
    assert len(_REBASELINE_DEPENDENCY_CODES) == 7
    assert len(_ALERT_SPLIT_DEPENDENCY_CODES) == 2
    assert not _ORIGINAL_DEPENDENCY_CODES & _REBASELINE_DEPENDENCY_CODES
    assert not _ORIGINAL_DEPENDENCY_CODES & _ALERT_SPLIT_DEPENDENCY_CODES
    assert not _REBASELINE_DEPENDENCY_CODES & _ALERT_SPLIT_DEPENDENCY_CODES


def test_the_two_alert_split_codes_are_accepted():
    """ALERT itself remains in the vocabulary: it is narrowed, not retired."""
    for code in ("ALERT-DELIVERY", "RECIPIENT-AUTHORITY", "ALERT"):
        assert code in KNOWN_DEPENDENCY_CODES, code


def test_gv07_is_promoted_and_cites_nothing(by_id):
    """The owner ruling that split ALERT, asserted on the artifact.

    GV-07 cited EVAL-EVENT and ALERT, then RECIPIENT-AUTHORITY, then
    ALERT-DELIVERY alone — each dropped as it was satisfied, because this
    artifact's dependency lists are BLOCKER semantics, not "everything the
    vector uses" (the same rule that removed P-1/P-3/P-4/C-2).

    ALERT-DELIVERY was the last of them. Stage 5 made "all original
    recipients" ANSWERABLE from the frozen snapshot; what remained was that
    the withdrawal must REACH them. The notification platform (D-A), the
    persist-first processing step (D-C) and the operational command (D-F)
    together do that, and the end-to-end harness walks it: two recipients,
    two handoffs, two delivery records, no retransmission on replay, and the
    withdrawal reaching the same two after current authority is deleted.

    So GV-07 now cites NOTHING — which is the strongest form this assertion
    can take, because an empty list cannot hide a blocker behind a stale code.
    """
    harness = by_id["GV-07"]["harness"]
    assert harness["blocking_dependencies"] == []
    assert harness["execution"] == EXECUTION_EXECUTABLE
    # The split must not have leaked onto vectors blocked for unrelated
    # reasons. GV-06 and GV-22 have since been PROMOTED — they needed only the
    # record/render half, so they now carry no blocker at all, which is the
    # clearest possible evidence the split was drawn in the right place.
    # GV-18 has since been promoted on the day-scoped primitive (ruling 2: B),
    # its ALERT citation having been stale rather than satisfied, and GV-26 on
    # the platform incident (ruling D-9.1), its ALERT citation stale for the
    # same kind of reason. The split now speaks to NO remaining vector — which
    # is the outcome it predicted, not a reason to stop asserting it.
    assert by_id["GV-18"]["harness"]["blocking_dependencies"] == []
    for gid in ("GV-06", "GV-22"):
        harness = by_id[gid]["harness"]
        assert harness["execution"] == EXECUTION_EXECUTABLE, gid
        assert harness["blocking_dependencies"] == [], gid
    # GV-17 is the strongest evidence of all: owner ruling B3 took it off ALERT
    # because its §24.7 visibility is a read-surface requirement, not a
    # notification one — and it has since been PROMOTED on exactly that basis,
    # with ALERT still unbuilt. Had the split been drawn wrongly, GV-17 could
    # not have become executable without it.
    assert by_id["GV-17"]["harness"]["execution"] == EXECUTION_EXECUTABLE
    assert by_id["GV-17"]["harness"]["blocking_dependencies"] == []
    assert "ALERT" not in by_id["GV-17"]["harness"]["blocking_dependencies"]
    # GV-26 was the last vector the split still spoke to, and it has since been
    # PROMOTED on that basis: §24.15 asks for a platform INCIDENT — operational
    # evidence with no audience, no recipient, no channel and no delivery — so
    # ALERT's entry point could never have fired for it either. Asserted
    # positively rather than dropped: a vector re-acquiring an ALERT citation
    # must fail here, not merely stop being checked.
    assert by_id["GV-26"]["harness"]["execution"] == EXECUTION_EXECUTABLE
    assert by_id["GV-26"]["harness"]["blocking_dependencies"] == []


def test_the_seven_rebaseline_codes_are_accepted():
    for code in ("EVAL-EVENT", "PRODUCER", "MODEL-VARIANCE", "ALERT",
                 "STATE-CONTRACT", "POLICY-VERSION", "WORKER-CONTEXT-WIRING"):
        assert code in KNOWN_DEPENDENCY_CODES, code


def test_the_original_codes_remain_accepted_even_where_unused():
    """Backward compatibility. P-1/P-3/P-4/C-2 are no longer cited by any
    vector, but the vocabulary must still recognise them so a historical
    blocker can be re-cited without a schema change."""
    for code in ("P-1", "P-2", "P-3", "P-4", "P-5", "C-2",
                 "MODEL-NETTING", "MODEL-GRACE", "MODEL-HORIZON",
                 "MODEL-WORKWEEK"):
        assert code in KNOWN_DEPENDENCY_CODES, code


def test_an_unknown_dependency_code_is_rejected():
    """Guard the guard: the vocabulary check is a real filter."""
    for invented in ("P-9", "MODEL-UNICORN", "eval-event", "PRODUCERS", ""):
        assert invented not in KNOWN_DEPENDENCY_CODES, invented


def test_the_satisfied_primitives_are_no_longer_cited(vectors):
    """The re-baseline's whole point: a satisfied primitive is not a blocker.

    P-1, P-3, P-4 and C-2 were established as satisfied by the owner-gate
    audit. If one reappears in the artifact, the inventory has drifted back to
    declaring blockers that no longer block.
    """
    cited = {code for v in vectors
             for code in v["harness"]["blocking_dependencies"]}
    for satisfied in ("P-1", "P-3", "P-4", "C-2"):
        assert satisfied not in cited, (
            f"{satisfied} is cited as a blocker but the audit established it "
            f"is satisfied")


def test_no_vector_is_wired_to_a_calculation():
    """The fixture must not reference the platform's worked-time calculation.
    If it ever does, someone has started executing these against a second
    source of truth (INV-2, §7.2)."""
    raw = GOLDEN_PATH.read_text(encoding="utf-8")
    for forbidden in ("_worked", "session_worked_seconds",
                      "clock_out_time -", "total_seconds -"):
        assert forbidden not in raw, (
            f"golden vector fixture references {forbidden!r} — the vectors are "
            "declarative and MUST NOT be executed against the platform's "
            "session-aggregate worked time (§7.2, INV-2)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 4. Normative expected outputs are preserved (anti-drift)
# ══════════════════════════════════════════════════════════════════════════════
# Independently transcribed from the §42 table. If these disagree with the
# fixture, one transcription drifted — which is the event this file exists for.

def test_gv01_three_measurements_diverge(by_id):
    v = by_id["GV-01"]
    assert v["expected_verbatim"] == (
        "variance −60; qualifying 495; gross 30; excluded 30; "
        "attributable 0; no event"
    )
    f = v["expected_fields"]
    assert f["schedule_variance_minutes"] == -60
    assert f["qualifying_minutes"] == 495
    assert f["gross_shortfall_minutes"] == 30
    assert f["exclusion_minutes"] == 30
    assert f["attributable_minutes"] == 0
    assert f["threshold_event_emitted"] is False


def test_gv02_meal_rest_distinction(by_id):
    f = by_id["GV-02"]["expected_fields"]
    assert f["qualifying_minutes"] == 500
    assert f["attributable_minutes"] == 25
    assert f["rest_qualifying_cap_minutes"] == 30
    assert f["rest_cap_is_cumulative"] is True


def test_gv03_is_a_must_fail_guard_for_inv4(by_id):
    """§42: "MUST FAIL. Asserts that no code path collapses BRK_M and BRK_R".
    A guard that silently became a passing test would be worthless."""
    v = by_id["GV-03"]
    assert "MUST FAIL" in v["expected_verbatim"]
    assert v["expected_fields"]["must_fail"] is True
    assert v["expected_fields"]["asserts_invariant"] == "INV-4"


def test_gv04_threshold_triggers_at_exactly_thirty(by_id):
    """§21.1: "Exactly 30 minutes triggers. Strictly-greater-than is a defect."
    The single most likely off-by-one in the whole control."""
    f = by_id["GV-04"]["expected_fields"]
    assert f["weekly_total_minutes"] == 30
    assert f["crossed"] is True
    assert f["events_emitted"] == ["ECO_THRESHOLD_REACHED"]
    assert f["event_count"] == 1


def test_gv05_twenty_nine_does_not_trigger(by_id):
    f = by_id["GV-05"]["expected_fields"]
    assert f["weekly_total_minutes"] == 29
    assert f["crossed"] is False
    assert f["event_count"] == 0


def test_gv06_exactly_one_primary_event(by_id):
    f = by_id["GV-06"]["expected_fields"]
    assert f["primary_event_count"] == 1
    assert f["further_notifications"] == 0


def test_gv07_withdrawal_retains_the_original(by_id):
    f = by_id["GV-07"]["expected_fields"]
    assert f["weekly_total_minutes"] == 20
    assert f["events_emitted"] == ["ECO_THRESHOLD_WITHDRAWN"]
    assert f["original_event_retained"] is True


def test_gv08_recrossing_increments_sequence(by_id):
    f = by_id["GV-08"]["expected_fields"]
    assert f["weekly_total_minutes"] == 32
    assert f["sequence"] == 2
    assert f["references_prior_withdrawal"] is True


def test_gv09_cross_midnight_counted_once(by_id):
    f = by_id["GV-09"]["expected_fields"]
    assert f["work_date_weekday"] == "Sunday"
    assert f["shift_attribution_rule"] == "START_DATE"
    assert f["counted_in_workweeks"] == 1


def test_gv10_dst_duration_is_eight_hours_not_nine(by_id):
    f = by_id["GV-10"]["expected_fields"]
    assert f["duration_hours"] == 8
    assert f["required_minutes"] == 525


def test_gv11_autumn_week_is_169_hours(by_id):
    f = by_id["GV-11"]["expected_fields"]
    assert f["week_length_hours"] == 169
    assert f["days_double_counted"] == 0


def test_gv12_weekly_total_is_five_not_three(by_id):
    """R-16.5. The Worker must be able to add the daily figures up by hand
    (INV-10); rounding once at week end gives 3 and breaks that."""
    f = by_id["GV-12"]["expected_fields"]
    assert f["daily_shortfall_minutes"] == [1, 1, 1, 1, 1]
    assert f["weekly_total_minutes"] == 5
    assert f["incorrect_result_if_rounded_once_at_week_end"] == 3


def test_gv13_gv14_gv15_leave_arithmetic(by_id):
    thirteen = by_id["GV-13"]["expected_fields"]
    assert (thirteen["gap_minutes"], thirteen["leave_overlap_minutes"]) == (30, 15)
    assert thirteen["exclusion_minutes"] == 15
    assert thirteen["attributable_minutes"] == 15

    fourteen = by_id["GV-14"]["expected_fields"]
    assert fourteen["exclusion_minutes"] == 30
    assert fourteen["attributable_minutes"] == 0
    assert fourteen["never_negative"] is True

    fifteen = by_id["GV-15"]["expected_fields"]
    assert fifteen["attributable_minutes"] == 0
    assert fifteen["credit_created"] is False


def test_gv16_missing_clock_out_is_never_an_early_clock_out(by_id):
    """Owner-amended: the STATE changed, the BEHAVIOUR did not.

    §24.6's substance — "a missing clock-out is not an early clock-out", no
    shortfall computed, no advisory, contributes nothing — is unchanged and
    still asserted below. What the owner amended is only the NAME of the
    resulting evaluation state: R4 fixes ``EcoDayState`` at six values and
    forbids adding ``MISSING_CLOCK_OUT``, so S3's ``DEFERRED_PENDING_LEDGER``
    is the ECO-domain counterpart and the vector now expects it.

    ``expected_verbatim`` deliberately still reads "state MISSING_CLOCK_OUT":
    it is the faithful §42 transcription, and the specification did not change
    — the platform's mapping of it did. The divergence is the ruling, recorded
    rather than hidden.
    """
    f = by_id["GV-16"]["expected_fields"]
    assert f["state"] == "DEFERRED_PENDING_LEDGER"
    assert f["shortfall_computed"] is False
    assert f["advisory_shown"] is False
    assert f["contributes_to_accumulator_minutes"] == 0
    # The transcription is untouched, so the amendment stays visible.
    assert "MISSING_CLOCK_OUT" in by_id["GV-16"]["expected_verbatim"]


def test_gv17_deferred_day_is_neither_counted_nor_excused(by_id):
    f = by_id["GV-17"]["expected_fields"]
    assert f["state"] == "DEFERRED_PENDING_LEDGER"
    assert f["contributes_to_accumulator_minutes"] == 0
    assert f["visible_to_worker"] is True
    assert f["visible_to_administrator"] is True


def test_gv18_unresolved_requirement_shows_the_worker_nothing(by_id):
    f = by_id["GV-18"]["expected_fields"]
    assert f["state"] == "REQUIREMENT_UNRESOLVED"
    assert f["administrator_alert"] is True
    assert f["shown_to_worker"] is False
    assert f["shortfall_computed"] is False


def test_gv19_and_gv20_netting(by_id):
    assert by_id["GV-19"]["expected_fields"]["tuesday_attributable_minutes"] == 20
    twenty = by_id["GV-20"]["expected_fields"]
    assert twenty["tuesday_attributable_minutes"] == 0
    assert twenty["bank_consumed_minutes"] == 20


def test_gv21_historical_policy_resolves_to_510_in_perpetuity(by_id):
    v = by_id["GV-21"]
    assert v["expected_verbatim"] == "required 510, in perpetuity"
    f = v["expected_fields"]
    assert f["evaluated_date"] == "2026-08-28"
    assert f["required_minutes"] == 510
    assert f["stable_in_perpetuity"] is True


def test_gv22_and_gv23_retroactive_behaviour(by_id):
    twenty_two = by_id["GV-22"]["expected_fields"]
    assert twenty_two["weekly_total_minutes"] == 33
    assert twenty_two["is_retroactive"] is True

    twenty_three = by_id["GV-23"]["expected_fields"]
    assert twenty_three["retroactive_horizon_days"] == 90
    assert twenty_three["record_corrected"] is True
    assert twenty_three["notification_sent"] is False


def test_gv24_grace_zeroes_attributable_but_retains_gross(by_id):
    """§11: gross is still recorded. Zeroing both would erase the evidence that
    a short day happened at all."""
    f = by_id["GV-24"]["expected_fields"]
    assert f["grace_minutes"] == 5
    assert f["attributable_minutes"] == 0
    assert f["gross_shortfall_minutes_retained"] == 3


def test_gv25_replay_is_idempotent(by_id):
    f = by_id["GV-25"]["expected_fields"]
    assert f["final_state_identical"] is True
    assert f["duplicate_events"] == 0
    assert f["duplicate_records"] == 0
    assert f["duplicate_notifications"] == 0


def test_gv26_integrity_fault_refuses_to_evaluate_and_never_clamps(by_id):
    """§24.15 — the control MUST NOT repair, skip or clamp a corrupt interval.

    "Silently coping with corrupt evidence is how evidentiary systems lose
    their standing."
    """
    f = by_id["GV-26"]["expected_fields"]
    assert f["state"] == "LEDGER_INTEGRITY_FAULT"
    assert f["evaluation_performed"] is False
    assert f["platform_incident_raised"] is True
    assert f["clamping_applied"] is False


def test_gv27_per_day_requirements_are_not_retroactively_reclassified(by_id):
    """§24.12: a Worker moving Part-Time -> Full-Time on Wednesday has Monday and
    Tuesday evaluated at 240, not at today's classification."""
    f = by_id["GV-27"]["expected_fields"]
    assert f["per_day_required_minutes"] == [240, 240, 525, 525, 525]
    assert f["weekly_threshold_count"] == 1


def test_gv28_non_working_day_still_writes_a_record(by_id):
    """§24.2: the record is written "so that the week fold is complete and
    explainable" — a silent gap is not the same as a recorded non-working day."""
    f = by_id["GV-28"]["expected_fields"]
    assert f["required_minutes"] == 0
    assert f["gross_shortfall_minutes"] == 0
    assert f["advisory_shown"] is False
    assert f["state"] == "NOT_A_WORKING_DAY"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Blocker coverage matches the audit
# ══════════════════════════════════════════════════════════════════════════════

def test_the_blocker_concentration_matches_the_audit(by_id):
    """Where the remaining work is concentrated — and it no longer is.

    The sealed ledger (P-1) used to block 22 vectors; it is built, so it blocks
    none. PRODUCER and EVAL-EVENT replaced it, then EVAL-EVENT was built too,
    then ALERT led alone at three. Owner ruling B3 took GV-17 off ALERT — §24.7
    is discharged by the WF-ECO-03 / WF-ECO-04 explanation surfaces, not by a
    notification — and that was the last thing holding a lead together.

    THE CONCENTRATION HAS DISSOLVED. THIS GUARD NOW SAYS SO.
    --------------------------------------------------------
    There is no leader any more: the peak is TWO, reached by ALERT alone,
    and nothing exceeds it. The remaining FOUR blocked vectors are held by
    FIVE distinct codes, most of them citing one vector each. It was nine
    until PRODUCER and EVAL-EVENT were dropped as stale citations,
    MODEL-VARIANCE left with GV-01's promotion, and MODEL-NETTING left with
    GV-19's and GV-20's.

    That is a weaker-sounding claim than "X leads at three" but a stronger
    test, because it has no privileged code to be re-pointed at: ANY code
    reaching three fails it. The previous versions each named a leader and each
    had to be rewritten when a promotion removed one — this one only fails if
    the work RE-concentrates, which is the thing actually worth knowing.

    WHY THE OLD "COVER AT LEAST HALF" CLAIM WAS RETIRED, NOT WEAKENED
    ----------------------------------------------------------------
    The previous version asserted that PRODUCER and EVAL-EVENT together covered
    at least half the blocked set, and that EVAL-EVENT was the single largest
    group. Promoting GV-04, GV-05 and GV-08 made BOTH false, and made them false
    in the direction that means success: the vectors EVAL-EVENT blocked by
    itself left the blocked set entirely, taking the coverage with them. 20
    blocked became 17, and EVAL-EVENT fell from 7 citations to 2.

    Re-pointing the same inequality at whichever pair happens to lead would be
    a number chosen to pass. The honest successor claims are asserted instead,
    and each is falsifiable:

    The claims, each falsifiable:

    * PRODUCER now blocks NOTHING. GV-09's promotion took one vector, GV-17's
      took another, and GV-01 — the last citation — was a stale label on a
      satisfied primitive, dropped once the audit proved the producer seals
      intervals and D18-F2 freezes the departure GV-01 measures from;
    * NO code blocks more than two vectors. Not ALERT, not anything;
    * the peak of two is a genuine tie, not a near-lead — asserted as a set so
      a code breaking away to three fails here rather than quietly leading;
    * EVAL-EVENT is satisfied and now blocks NOTHING either. GV-23 was its
      last citation, and the audit proved the package records is_retroactive
      over a persisted correction lineage — all GV-23 asks of it — so the
      citation was stale rather than open;
    * VISIBILITY-SURFACE and VIEW-AUTHORITY are both satisfied and now block
      NOTHING. GV-17 was the only vector that cited either, and its promotion
      retired both from the blocked set entirely.
    """
    blocked = {gid for gid, v in by_id.items()
               if v["harness"]["execution"] == EXECUTION_BLOCKED}
    deps = {gid: set(by_id[gid]["harness"]["blocking_dependencies"])
            for gid in blocked}

    producer = {gid for gid, d in deps.items() if "PRODUCER" in d}
    events = {gid for gid, d in deps.items() if "EVAL-EVENT" in d}
    alert = {gid for gid, d in deps.items() if "ALERT" in d}

    # PRODUCER is satisfied and is now cited by NOTHING. Its last citation
    # (GV-01) was stale: GV-09 left when MODEL-WORKWEEK closed, GV-17 when
    # VISIBILITY-SURFACE and VIEW-AUTHORITY did, and GV-01's label was dropped
    # once the audit proved the primitive supplies what the vector needs.
    # EVAL-EVENT went the same way, GV-23 having been its last citation.
    assert producer == set(), sorted(producer)
    assert events == set(), sorted(events)

    # Satisfied primitives, each cited by nothing. Asserted rather than
    # assumed: a future vector citing any of them would be claiming a blocker
    # the audit established is not one.
    for satisfied in ("VISIBILITY-SURFACE", "VIEW-AUTHORITY",
                      "WORKER-CONTEXT-WIRING", "POLICY-VERSION",
                      "MODEL-GRACE", "P-2", "PRODUCER", "EVAL-EVENT",
                      "MODEL-VARIANCE", "MODEL-NETTING"):
        holders = {gid for gid, d in deps.items() if satisfied in d}
        assert holders == set(), f"{satisfied} is satisfied but blocks {holders}"

    by_code = {}
    for d in deps.values():
        for code in d:
            by_code[code] = by_code.get(code, 0) + 1

    # THE BLOCKED SET IS NOW EMPTY. The peak fell from nine to two to one, and
    # GV-26's promotion on §24.15's platform incident (ruling D-9.1) took the
    # last of it. Asserted POSITIVELY and in two independent ways, because an
    # empty set makes every loop below it vacuous and a vacuous guard is not a
    # guard: the blocked set is empty, and no code blocks anything.
    ranked = sorted(by_code.items(), key=lambda kv: (-kv[1], kv[0]))
    assert blocked == set(), sorted(blocked)
    assert by_code == {}, ranked
    assert EXECUTION_BLOCKED not in {
        v["harness"]["execution"] for v in by_id.values()}
    peak = max(by_code.values(), default=0)
    assert peak == 0, ranked

    # VIEW-AUTHORITY is built (owner ruling B2), so the same rule applies to
    # it: a vector blocked by it alone would be promotable or mis-labelled.
    for gid, d in deps.items():
        if "VIEW-AUTHORITY" in d:
            assert d - {"VIEW-AUTHORITY"}, (
                f"{gid} is blocked by VIEW-AUTHORITY alone, but the primitive "
                f"exists — it is either promotable or mis-labelled")

    # No exemptions, and the bound outlives the empty set: it is what catches a
    # future re-baseline re-concentrating the work rather than spreading it.
    for code, count in by_code.items():
        assert count <= 2, (
            f"{code} blocks {count} vectors — the remaining gaps have "
            f"re-concentrated, and this guard's premise needs revisiting")


def test_break_semantics_no_longer_block_the_meal_rest_vectors(by_id):
    """P-2 was the last ORIGINAL primitive still blocking anything.

    The claim inverts: a post-seal classification artifact, a versioned
    BreakTreatmentProfile and the cumulative rest cap now exist, so GV-02 and
    GV-03 execute. Every code still blocking a vector came from a later
    re-baseline, not from the original ten.
    """
    for gid in ("GV-02", "GV-03"):
        harness = by_id[gid]["harness"]
        assert harness["execution"] == EXECUTION_EXECUTABLE, gid
        assert harness["blocking_dependencies"] == [], gid

    # And P-2 is retired from the blocked set entirely — cited by nothing.
    holders = {gid for gid, v in by_id.items()
               if "P-2" in v["harness"]["blocking_dependencies"]}
    assert holders == set(), f"P-2 is satisfied but blocks {holders}"


def test_the_leave_vectors_are_no_longer_blocked_by_leave_granularity(by_id):
    """P-4 was their only blocker and it is satisfied, so they execute.

    GV-01 used to be the counter-example here: same leave window, but it also
    expected a schedule variance the evaluator refused to invent. Both halves
    have since closed — P-4 gave it the window, D-1.1 gave it the frozen
    scheduled end — so it is now asserted alongside them rather than against
    them, and its execution lives in `test_gv01_execution.py::TestGV01`.
    """
    for gid in ("GV-13", "GV-14", "GV-15"):
        harness = by_id[gid]["harness"]
        assert harness["execution"] == EXECUTION_EXECUTABLE, gid
        assert harness["blocking_dependencies"] == [], gid

    assert by_id["GV-01"]["harness"]["execution"] == EXECUTION_EXECUTABLE
    assert by_id["GV-01"]["harness"]["blocking_dependencies"] == []


def test_gv21_executes_on_historical_configuration_resolution(by_id):
    """P-5's blocker was "no historically resolvable policy versioning".

    For THIS vector's stated expectation — a past date still resolving the
    superseded requirement — ConfigVersion supplies exactly that, so the vector
    runs. The general policy-version question did not disappear: it moved to
    GV-25 under POLICY-VERSION, and has since been ANSWERED — a distinct
    PolicyVersion artifact, resolved historically and consumed by EVAL-DAY — so
    GV-25 now carries no blocker either. The distinction this guard draws is
    unchanged: ConfigVersion is not the policy, and never became it.
    """
    assert by_id["GV-21"]["harness"]["execution"] == EXECUTION_EXECUTABLE
    assert by_id["GV-21"]["harness"]["blocking_dependencies"] == []
    assert by_id["GV-25"]["harness"]["execution"] == EXECUTION_EXECUTABLE
    assert by_id["GV-25"]["harness"]["blocking_dependencies"] == []


def test_gv16_state_contract_was_resolved_not_abandoned(by_id):
    """STATE-CONTRACT blocked GV-16 until the owner resolved the contradiction.

    The conflict was real: GV-16 expected a day state of ``MISSING_CLOCK_OUT``,
    which R4's six-value evaluation vocabulary does not contain and R4 forbids
    adding. It was closed by ruling rather than by code — the owner kept the
    six values, kept MISSING_CLOCK_OUT as the LEDGER state, and authorised the
    one normative amendment that made the vector expect what the platform
    produces.

    So the vector is executable AND the vocabulary is intact. Both halves are
    asserted, because promoting it by widening ``EcoDayState`` would have been
    the wrong way to reach the same green.
    """
    from app.eval_day import EcoDayState
    from app.work_ledger import LedgerState

    harness = by_id["GV-16"]["harness"]
    assert harness["execution"] == EXECUTION_EXECUTABLE
    assert harness["blocking_dependencies"] == []
    assert by_id["GV-16"]["expected_fields"]["state"] == "DEFERRED_PENDING_LEDGER"
    assert len({s.value for s in EcoDayState}) == 7
    assert "MISSING_CLOCK_OUT" not in {s.value for s in EcoDayState}
    assert "MISSING_CLOCK_OUT" in {s.value for s in LedgerState}


def test_gv18_is_promoted_and_cites_nothing(by_id):
    """The one vector that genuinely never needed the ledger, now promoted.

    §19 returns at step 3 on an unresolved requirement, before any interval is
    read, so the day state and the absent minutes were producible long before
    anything else. What remained was ``administrator_alert: true``.

    ALERT was a STALE citation, not a satisfied one. A requirement-unresolved
    day emits no threshold event — EVAL-WEEK blocks the week and EVAL-EVENT
    refuses anything that is not COMPLETE — so ALERT's entry point, which takes
    an ``eco_threshold_event.id``, could never have fired for it. That is the
    same mis-drawn dependency owner ruling B3 corrected for GV-17.

    Ruling 2: B authorized a separate day-scoped primitive instead, and
    ``app.eco_day_alert`` raises the §24.3 alert from the authoritative
    ``EcoDayRecord`` to the designated PRIMARY ADMIN under the non-suppressible
    ECO_ADMIN_OPERATIONAL class. GV-18 now cites NOTHING, which is the
    strongest form this assertion can take.
    """
    harness = by_id["GV-18"]["harness"]
    assert harness["execution"] == EXECUTION_EXECUTABLE
    assert harness["blocking_dependencies"] == []


def test_no_executable_vector_retains_a_stale_blocker(by_id):
    """The re-baseline's other half: executable means nothing is left.

    GV-13/14/15 took the gap as a stated input, so they only ever needed the
    leave arithmetic; GV-21 only ever needed historical resolution; GV-10 and
    GV-11 only ever needed the producer. All six are now empty, and the guard
    is that they must be.
    """
    for gid in EXECUTABLE_IDS:
        assert by_id[gid]["harness"]["blocking_dependencies"] == [], gid


# ══════════════════════════════════════════════════════════════════════════════
# 5. EXECUTION — the vectors the current implementation can actually run
# ══════════════════════════════════════════════════════════════════════════════
# Everything above this line validates the FIXTURE. This section executes it,
# for the vectors whose declared blockers the owner-gate audit established as
# satisfied: GV-13/14/15 (P-4), GV-21 (P-5, for this vector's stated
# expectation only), and GV-10/GV-11 (PRODUCER, D18-F8). GV-12 is the
# seventh; it is executed with the week fold, not here.
#
# THE EXPECTED VALUES ARE READ FROM THE ARTIFACT, NEVER RESTATED HERE.
# Each test loads ``expected_fields`` from the canonical JSON and compares it to
# what the production services return. There is deliberately no second
# expected-value dictionary: a harness carrying its own copy of the answer
# proves only that the copy matches itself.
#
# EVERY FIELD IS COMPARED. ``_assert_exhaustive`` fails when a vector carries a
# field the harness does not handle, so a future field cannot be silently
# ignored into a green build.
#
# NOTHING IS RECOMPUTED HERE. The harness builds inputs and reads outputs; the
# exclusion arithmetic, the rounding and the historical version selection all
# happen inside the authoritative services. Search this section for a
# subtraction and you will not find one.

import sqlalchemy as sa

from app.config_versioning import (
    ConfigValue,
    ConfigVersion,
    DatabaseConfigVersionProvider,
)
from app.eval_day import (
    EcoDayRecord,
    LeaveInputContribution,
    LeaveInputSnapshot,
    approved_leave_overlap,
    evaluate_and_record_day,
    round_half_up_minutes,
)
from app.leave_intervals import (
    CandidateLeaveInterval,
    LeaveInterval,
    LeaveIntervalSet,
    record_leave_intervals,
)
from app.requirement_resolution import (
    RequirementCandidate,
    ResolutionOutcome,
    resolve_requirement_strict,
)
from app.work_ledger import (
    CandidateInterval,
    DayKey,
    LedgerState,
    Provenance,
    WorkDayLedger,
    WorkState,
    WorkStateInterval,
    seal_day,
)

# ── GV-10 / GV-11 only: the forward-only producer and the W4 workweek ────────
# The DST vectors are the only ones that need real session evidence, because
# their expectation is a MEASURED duration. Reading it off a hand-built
# interval would prove nothing: the point is that production code derives 8h
# from two UTC instants. Nothing here reaches for a scheduler or an endpoint —
# `produce_and_seal_work_day` is invoked directly, as D18-F8 permits.
from app import _tz
from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.eval_week import DAYS_IN_WORKWEEK, canonical_dates
from app.schedule_policy import keys as SCHEDULE_KEYS
from app.work_ledger.workweek import canonical_workweek_key
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc

#: Ids of this suite's own, chosen high so they cannot collide with the other
#: ECO suites sharing the session-scoped engine.
GV_ORG = 4200
GV_USER = 42000

#: §42's canonical full-time requirement, used by GV-01, GV-02 and GV-10.
GV_REQUIRED_MINUTES = 525

GV_ZONE = "Asia/Kolkata"

#: §42's DST zone. GV-10 and GV-11 name Europe/London explicitly, and the
#: transition dates below are that zone's, not a generic "has DST" stand-in.
GV_DST_ZONE = "Europe/London"

#: ALL TWENTY-EIGHT vectors are executable. Exact, not a floor. Six are
#: executed in this file; GV-12 in ``test_eval_week_fold.py::TestGV12``, GV-28
#: in ``test_working_day_classifier.py::TestGV28``, GV-04/GV-05/GV-08 in
#: ``test_eval_event.py``, GV-06/GV-22 in ``test_alert.py``, GV-16 in
#: ``test_departure_resolution.py``, GV-17 in ``test_gv17_execution.py`` and
#: GV-26 in ``test_eco_ledger_incident.py::TestGV26`` — each executed where
#: its behaviour actually lives.
EXECUTABLE_IDS = ("GV-01", "GV-02", "GV-03", "GV-04", "GV-05", "GV-06",
                  "GV-07", "GV-08", "GV-09", "GV-10", "GV-11", "GV-12",
                  "GV-13", "GV-14", "GV-15", "GV-16", "GV-17", "GV-18",
                  "GV-19", "GV-20", "GV-21", "GV-22", "GV-23", "GV-24",
                  "GV-25", "GV-26", "GV-27", "GV-28")

#: Vectors whose remaining dependency is not satisfied. Stated as the
#: complement so the two sets provably partition all 28 — and it is now EMPTY,
#: which is asserted positively below rather than left to a vacuous loop.
NON_EXECUTABLE_IDS = tuple(g for g in EXPECTED_IDS if g not in EXECUTABLE_IDS)


@pytest.fixture(scope="module", autouse=True)
def _execution_schema(engine):
    """Only the tables the six vectors touch. Additive, per-model."""
    from app.break_classification import (
        BreakClassificationAssertion, ClassificationSnapshot,
        EcoClassificationAuthority,
    )
    from app.break_treatment import BreakTreatmentProfile
    from app.clock_session_boundary.models import ClockSessionBoundaryEvent
    from app.policy_version.models import PolicyVersion
    from app.work_ledger_producer.models import (
        WorkLedgerProducerCutoverHistory,
    )
    from database.models import (
        AppSettings, BreakSession, ClockSession, EmployeeAvailability, Holiday,
        LeaveRequest, OfflineInterval, Organization, OrgSettings, Shift,
        ShiftAssignment, User,
    )

    for model in (Organization, User, LeaveRequest, WorkDayLedger,
                  PolicyVersion, BreakTreatmentProfile,
                  EcoClassificationAuthority,
                  BreakClassificationAssertion,
                  ClassificationSnapshot,
                  WorkStateInterval, ConfigVersion, ConfigValue, EcoDayRecord,
                  LeaveIntervalSet, LeaveInterval, LeaveInputSnapshot,
                  LeaveInputContribution,
                  # GV-10 / GV-11: the producer's source evidence and gates.
                  # Shift/ShiftAssignment are the schedule resolver's own
                  # tiers — the working-day question reads them, and an
                  # empty table is the "no shift configured" answer.
                  OrgSettings, AppSettings, ClockSession,
                  ClockSessionBoundaryEvent, Shift, ShiftAssignment,
                  EmployeeAvailability, Holiday,
                  # The producer's other source tables. Left EMPTY: this shift
                  # has no break and no offline window, and an empty table is
                  # how the producer is told so.
                  BreakSession, OfflineInterval,
                  WorkLedgerProducerCutoverHistory):
        model.__table__.create(engine, checkfirst=True)


def _ensure_week_start(db, organization_id, value):
    """Set the organization's week-start boundary, once.

    Upsert rather than insert: §12.1 requires every tenant to have a defined
    workweek before Early Clock-Out can be enabled, so more than one fixture
    needs to state it — and GV-11 reads the row back with ``.one()`` to prove
    the setting is honoured, which a duplicate would break.
    """
    from database.models import AppSettings

    row = (db.query(AppSettings)
             .filter(AppSettings.organization_id == organization_id,
                     AppSettings.key == SCHEDULE_KEYS.SETTING_WEEK_START_DAY,
                     AppSettings.team_key.is_(None),
                     AppSettings.user_id.is_(None))
             .one_or_none())
    if row is None:
        db.add(AppSettings(
            organization_id=organization_id, team_key=None, user_id=None,
            key=SCHEDULE_KEYS.SETTING_WEEK_START_DAY, value=value))
    else:
        row.value = value
    db.flush()


@pytest.fixture()
def tenant(db):
    """One organization and one Worker. Created through the canonical models."""
    from database.models import Organization, User

    if db.get(Organization, GV_ORG) is None:
        db.add(Organization(id=GV_ORG, name="Golden Vector Org"))
    if db.get(User, GV_USER) is None:
        db.add(User(id=GV_USER, name="GV Worker", email="gv@example.test",
                    organization_id=GV_ORG))
    db.flush()
    # MONDAY — what the lenient resolver defaulted to before the ECO path
    # began refusing an undefined boundary, so no vector's week grouping moves.
    _ensure_week_start(db, GV_ORG, "MONDAY")
    # An explicit historical PolicyVersion. ``evaluate_and_record_day`` refuses
    # with POLICY_UNRESOLVED without one; the vectors executed here evaluate
    # real days, so the Tenant must have a policy exactly as a real one would.
    # It carries no requirement — daily_minutes stays with Requirement
    # Resolution, which is what these leave vectors actually exercise.
    from tests._eco_policy import capture_test_policy
    from app.policy_version import PolicyVersion

    if db.query(PolicyVersion).filter_by(organization_id=GV_ORG).first() is None:
        capture_test_policy(db, organization_id=GV_ORG, commit=False)
        db.flush()
    return GV_ORG, GV_USER


def _assert_exhaustive(gid, expected, handled):
    """Every field in the vector must have been compared.

    The point of a golden harness is that it cannot pass by looking at a
    convenient subset. If the artifact grows a field, this fails until the
    harness either compares it or the vector is re-blocked.
    """
    unhandled = set(expected) - set(handled)
    assert not unhandled, (
        f"{gid}: expected_fields {sorted(unhandled)} were not compared. Either "
        f"compare them or leave the vector blocked — do not narrow the harness."
    )


# ── Requirement capture, through the public versioned-configuration writer ───
def _capture_requirement(db, *, organization_id, version_key, effective_from,
                         minutes):
    """One immutable ConfigVersion carrying one TENANT_FALLBACK requirement.

    Uses Step 6's own ``capture_version`` rather than inserting rows: the point
    is to exercise the real versioned-configuration path, including its content
    hash and its refusal to reuse a key or an effective date.
    """
    provider = DatabaseConfigVersionProvider(db)
    ref = provider.capture_version(
        organization_id=organization_id,
        config_version_id=version_key,
        effective_from=effective_from,
        candidates=[RequirementCandidate(
            tier="TENANT_FALLBACK", rank=6, minutes=minutes)],
    )
    db.flush()
    return ref


# ── Sealed work evidence, through the public ledger service ──────────────────
def _seal_worked_day(db, *, organization_id, user_id, work_date,
                     qualifying_seconds):
    """Seal one WRK interval of exactly ``qualifying_seconds``.

    Goes through ``seal_day``, so the interval set passes the ledger's own
    integrity validation and the workweek key is derived by the ledger rather
    than supplied by the test.
    """
    begin = datetime(work_date.year, work_date.month, work_date.day, 9, 0,
                     tzinfo=UTC)
    return seal_day(
        db,
        DayKey(organization_id=organization_id, user_id=user_id,
               work_date=work_date),
        intervals=[CandidateInterval(
            seq=1,
            state=WorkState.WRK.value,
            start_at_utc=begin,
            end_at_utc=begin + timedelta(seconds=qualifying_seconds),
            provenance=Provenance.WORKER.value,
        )],
        timezone_iana=GV_ZONE, is_working_day=True, working_day_source="org",
        created_by="GOLDEN_VECTOR",
    )


# ── Approved leave, through the public leave-interval service ────────────────
def _record_approved_leave(db, *, organization_id, user_id, work_date,
                           minutes, status="approved"):
    """One approved grant with one intraday interval of ``minutes``.

    Recorded through ``record_leave_intervals``, so the set version, the
    attributed work_date and the tenant binding are all produced by the leave
    authority rather than asserted by the test.
    """
    from zoneinfo import ZoneInfo

    from database.models import LeaveRequest

    request = LeaveRequest(
        user_id=user_id, organization_id=organization_id,
        leave_type="Casual Leave",
        from_date=datetime(work_date.year, work_date.month, work_date.day),
        to_date=datetime(work_date.year, work_date.month, work_date.day),
        description="golden vector fixture",
        status=status,
    )
    db.add(request)
    db.flush()

    begin = datetime(work_date.year, work_date.month, work_date.day, 18, 0,
                     tzinfo=UTC)
    recorded = record_leave_intervals(
        db,
        leave_request=request,
        intervals=[CandidateLeaveInterval(
            seq=1,
            start_at_utc=begin,
            end_at_utc=begin + timedelta(minutes=minutes),
        )],
        timezone_iana=GV_ZONE,
        zone=ZoneInfo(GV_ZONE),
        expected_version=None,
        recorded_by="GOLDEN_VECTOR",
    )
    db.flush()
    return request, recorded


# ══════════════════════════════════════════════════════════════════════════════
# GV-13 / GV-14 / GV-15 — MODEL-EXCLUSION, executed
# ══════════════════════════════════════════════════════════════════════════════
#: One distinct work_date per vector, so three fixtures cannot interfere.
_LEAVE_VECTOR_DATES = {
    "GV-13": date(2026, 8, 31),      # a Monday
    "GV-14": date(2026, 9, 1),
    "GV-15": date(2026, 9, 2),
}


@pytest.mark.parametrize("gid", ("GV-13", "GV-14", "GV-15"))
def test_leave_exclusion_vector_executes(by_id, db, tenant, gid):
    """Execute the vector against the real evaluation service.

    The scenario is built FROM the vector: ``gap_minutes`` fixes how far short
    the sealed work evidence falls, and ``leave_overlap_minutes`` fixes the
    approved leave recorded against the day. Everything compared afterwards is
    produced by ``evaluate_and_record_day`` and the leave authority.
    """
    organization_id, user_id = tenant
    vector = by_id[gid]
    expected = vector["expected_fields"]
    work_date = _LEAVE_VECTOR_DATES[gid]

    gap = expected["gap_minutes"]
    leave_minutes = expected["leave_overlap_minutes"]

    # Requirement, then work evidence that falls exactly ``gap`` short of it.
    _capture_requirement(
        db, organization_id=organization_id,
        version_key=f"{gid}-requirement", effective_from=work_date,
        minutes=GV_REQUIRED_MINUTES)
    _seal_worked_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date,
        qualifying_seconds=(GV_REQUIRED_MINUTES - gap) * 60)
    request, _ = _record_approved_leave(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date, minutes=leave_minutes)

    # The canonical leave authority resolves the overlap. Asserted through the
    # service, so a mis-scoped or superseded set would be caught here rather
    # than showing up as a wrong exclusion later.
    overlap = approved_leave_overlap(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date)
    assert overlap.is_resolved, gid
    assert request.id in overlap.leave_request_ids, (
        f"{gid}: the grant was not resolved as APPROVED leave")

    # The authoritative evaluation.
    result = evaluate_and_record_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date)
    assert result.is_recorded, f"{gid}: {result.reason} {result.detail}"
    record = result.record

    handled = []

    # gap: the gross shortfall the evaluator computed from requirement and
    # sealed evidence — not the number the fixture put in.
    assert record.gross_shortfall_minutes == expected["gap_minutes"], gid
    handled.append("gap_minutes")

    # leave overlap: converted from seconds by the evaluator's single rounding
    # site, the same function that rounds qualifying minutes.
    assert round_half_up_minutes(overlap.overlap_seconds) == \
        expected["leave_overlap_minutes"], gid
    handled.append("leave_overlap_minutes")

    if "exclusion_minutes" in expected:
        assert record.exclusion_minutes == expected["exclusion_minutes"], gid
        handled.append("exclusion_minutes")

    assert record.attributable_minutes == expected["attributable_minutes"], gid
    handled.append("attributable_minutes")

    if expected.get("never_negative"):
        # §14.2: exclusion is capped at the gap, so neither figure can go below
        # zero however much leave overlapped.
        assert record.exclusion_minutes >= 0, gid
        assert record.attributable_minutes >= 0, gid
        assert record.contributes_to_accumulator_minutes >= 0, gid
        handled.append("never_negative")

    if "credit_created" in expected:
        # Example D: leave does not create credit. With no gap, nothing is
        # excluded however much leave overlapped, and nothing is banked.
        assert expected["credit_created"] is False
        assert record.exclusion_minutes == 0, gid
        assert record.attributable_minutes == 0, gid
        assert record.contributes_to_accumulator_minutes == 0, gid
        handled.append("credit_created")

    _assert_exhaustive(gid, expected, handled)


def test_the_leave_vectors_went_through_the_immutable_snapshot(
    by_id, db, tenant
):
    """Guard the path: the exclusion must have been frozen, not just computed.

    Owner Ruling 2 makes the leave input snapshot the seventh component of the
    day record's identity. If a vector produced the right number without a
    snapshot, it did not go through MODEL-EXCLUSION's authoritative path.
    """
    organization_id, user_id = tenant
    expected = by_id["GV-13"]["expected_fields"]
    work_date = date(2026, 9, 3)

    _capture_requirement(
        db, organization_id=organization_id, version_key="GV-13-snapshot",
        effective_from=work_date, minutes=GV_REQUIRED_MINUTES)
    _seal_worked_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date,
        qualifying_seconds=(GV_REQUIRED_MINUTES - expected["gap_minutes"]) * 60)
    request, _ = _record_approved_leave(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date, minutes=expected["leave_overlap_minutes"])

    result = evaluate_and_record_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=work_date)
    assert result.is_recorded

    key = result.record.leave_input_snapshot_key
    assert key, "no leave input snapshot key was recorded"
    snapshot = db.execute(
        sa.select(LeaveInputSnapshot).where(
            LeaveInputSnapshot.organization_id == organization_id,
            LeaveInputSnapshot.snapshot_key == key)
    ).scalars().first()
    assert snapshot is not None, "the recorded snapshot key resolves to no row"
    assert snapshot.overlap_seconds == expected["leave_overlap_minutes"] * 60
    contributions = db.execute(
        sa.select(LeaveInputContribution).where(
            LeaveInputContribution.snapshot_id == snapshot.id)
    ).scalars().all()
    assert [c.leave_request_id for c in contributions] == [request.id]
    assert [c.status_at_evaluation for c in contributions] == ["approved"]


# ══════════════════════════════════════════════════════════════════════════════
# GV-21 — historical configuration resolution, executed
# ══════════════════════════════════════════════════════════════════════════════
def test_gv21_historical_requirement_resolution_executes(by_id, db, tenant):
    """Execute GV-21 against the real versioned-configuration resolver.

    The vector: evaluating 2026-08-28 AFTER a later version supersedes the one
    in force must still resolve the earlier value, in perpetuity.

    The test would pass trivially if the resolver simply returned the only
    version it could find, so the newer version is captured FIRST-order-
    independently and asserted to win for its own date. If ``version_as_of``
    ever selected the current version instead, ``510`` would become ``525``
    and this test fails.
    """
    organization_id, user_id = tenant
    expected = by_id["GV-21"]["expected_fields"]

    evaluated_date = date.fromisoformat(expected["evaluated_date"])
    superseded_minutes = expected["required_minutes"]        # v3 — 510
    later_minutes = GV_REQUIRED_MINUTES                      # v4 — 525

    provider = DatabaseConfigVersionProvider(db)
    handled = []

    # v3 governs the evaluated date; v4 takes effect afterwards.
    v3_from = date(2026, 8, 1)
    v4_from = date(2026, 9, 1)
    assert v3_from <= evaluated_date < v4_from, (
        "the fixture must place the evaluated date inside v3's window")

    _capture_requirement(
        db, organization_id=organization_id, version_key="GV-21-v3",
        effective_from=v3_from, minutes=superseded_minutes)

    # Before v4 exists the answer is already 510 — recorded so the comparison
    # after v4 is a statement about supersession, not about a lone version.
    before = resolve_requirement_strict(
        organization_id=organization_id, user_id=user_id,
        as_of=evaluated_date, provider=provider)
    assert before.outcome is ResolutionOutcome.RESOLVED
    assert before.required_minutes == superseded_minutes

    _capture_requirement(
        db, organization_id=organization_id, version_key="GV-21-v4",
        effective_from=v4_from, minutes=later_minutes)

    # THE VECTOR. The evaluated date still resolves to the superseded version.
    after = resolve_requirement_strict(
        organization_id=organization_id, user_id=user_id,
        as_of=evaluated_date, provider=provider)
    assert after.outcome is ResolutionOutcome.RESOLVED
    assert after.as_of == evaluated_date
    handled.append("evaluated_date")

    assert after.required_minutes == expected["required_minutes"], (
        f"resolving {evaluated_date} selected {after.required_minutes} — the "
        f"resolver reached past the superseded version")
    handled.append("required_minutes")

    if expected.get("stable_in_perpetuity"):
        # Stability has two halves: the answer did not move when a newer
        # version appeared, and it does not move on re-resolution.
        assert after.required_minutes == before.required_minutes
        again = resolve_requirement_strict(
            organization_id=organization_id, user_id=user_id,
            as_of=evaluated_date, provider=provider)
        assert again.required_minutes == expected["required_minutes"]
        assert again.config_version_id == after.config_version_id
        # And the newer version is real: it wins for its own date. Without
        # this, "510 in perpetuity" could be true merely because v4 was never
        # captured.
        later = resolve_requirement_strict(
            organization_id=organization_id, user_id=user_id,
            as_of=v4_from, provider=provider)
        assert later.required_minutes == later_minutes
        assert later.config_version_id != after.config_version_id
        handled.append("stable_in_perpetuity")

    _assert_exhaustive("GV-21", expected, handled)


# ══════════════════════════════════════════════════════════════════════════════
# GV-10 / GV-11 — the two DST vectors, executed (PRODUCER, D18-F8)
# ══════════════════════════════════════════════════════════════════════════════
# WHY THESE NEED THE PRODUCER AND THE OTHERS DO NOT
# -------------------------------------------------
# GV-13/14/15/21 state their gap as an INPUT, so a hand-sealed interval is a
# faithful fixture. GV-10's expectation is a MEASURED duration, so the measuring
# has to be done by production code: `produce_and_seal_work_day` derives the
# interval from two session boundary instants, and the test only reads the
# result. There is no subtraction in the GV-10 assertions.
#
# THE TRAP BOTH VECTORS EXIST TO CATCH
# ------------------------------------
# Subtracting two aware datetimes that share one `tzinfo` object ignores the
# offset entirely — Python documents the shortcut — so wall-clock arithmetic
# returns 9h for GV-10's shift and 168h for GV-11's week. Both tests therefore
# compute the WRONG answer explicitly and assert the platform does not produce
# it. Without that half, a regression to naive arithmetic could still pass.

#: Europe/London spring forward: 01:00 GMT becomes 02:00 BST. Always a Sunday,
#: which is why the GV-10 tenant treats every day as a working day.
GV10_TRANSITION_DAY = date(2026, 3, 29)

#: Europe/London autumn transition, and the Monday-start week containing it.
GV11_TRANSITION_DAY = date(2026, 10, 25)
GV11_WEEK_START = date(2026, 10, 19)

ALL_SEVEN_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
                  "SATURDAY", "SUNDAY"]


def _london(day, hour, minute=0):
    """A London wall-clock instant, as aware UTC.

    ``astimezone`` is what makes this an INSTANT rather than a wall-clock
    reading; the conversion is the whole point of both vectors.
    """
    from zoneinfo import ZoneInfo

    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=ZoneInfo(GV_DST_ZONE)).astimezone(UTC)


def _configure_dst_tenant(db, organization_id, *, working_days=None,
                          week_start=None, cutover=None):
    """Timezone, working week, week start and producer cutover.

    Written through the ordinary settings tables and the producer's own cutover
    service — no scheduler, no endpoint, no rollout row.
    """
    from database.models import AppSettings, OrgSettings

    _tz.invalidate_tz_cache()
    if db.get(OrgSettings, organization_id) is None:
        db.add(OrgSettings(organization_id=organization_id,
                           default_timezone=GV_DST_ZONE))
    if working_days is not None:
        db.add(AppSettings(organization_id=organization_id, team_key=None,
                           user_id=None, key=SCHEDULE_KEYS.SETTING_WORKING_DAYS,
                           value=json.dumps(working_days)))
    if week_start is not None:
        _ensure_week_start(db, organization_id, week_start)
    db.flush()
    _tz.invalidate_tz_cache()
    if cutover is not None:
        set_producer_cutover(db, organization_id=organization_id,
                            cutover_at=cutover, actor_label="GOLDEN_VECTOR")
        db.flush()


def _clock_session_with_provenance(db, *, organization_id, user_id,
                                   clock_in, clock_out):
    """One closed session carrying its D-15 boundary provenance.

    The session's own aggregate columns are given deliberately wrong values:
    if any of them ever reached the sealed interval, the duration assertion
    below would fail rather than quietly agreeing (§7.2, INV-2).
    """
    from database.models import ClockSession

    row = ClockSession(
        user_id=user_id, organization_id=organization_id,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=999999, break_seconds=888, offline_seconds=777,
        is_active=False,
    )
    db.add(row)
    db.flush()
    for boundary, at in ((BOUNDARY_CLOCK_IN, clock_in),
                         (BOUNDARY_CLOCK_OUT, clock_out)):
        append_boundary_event(
            db, session_id=row.id, user_id=user_id, boundary=boundary,
            boundary_at=at, provenance=Provenance.WORKER,
            actor_label="gv@example.test")
    db.flush()
    return row


def _aware(value):
    """pysqlite drops tzinfo on read; the stored value is UTC by contract."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def test_gv10_dst_spring_forward_measures_eight_hours(by_id, tenant, db):
    """GV-10, executed: "Duration 8h, not 9h; required minutes unchanged at 525".

    The shift is 01:00–10:00 London on the spring-forward day. 01:00 local is
    the transition instant itself (01:00 GMT), 10:00 local is 09:00 UTC, so the
    elapsed time is eight hours while the wall clock advanced nine.

    Both halves of the vector are exercised by production code: the producer
    derives the duration from the two boundary instants, and requirement
    resolution supplies 525 as a configured DURATION, which is why it is
    unaffected by the clock change (§16.3).
    """
    organization_id, user_id = tenant
    expected = by_id["GV-10"]["expected_fields"]
    handled = []

    # Every day is a working day: the transition is always a Sunday. A fixture
    # choice about the tenant, not an assertion about the vector.
    _configure_dst_tenant(
        db, organization_id, working_days=ALL_SEVEN_DAYS, week_start="MONDAY",
        cutover=datetime(2026, 3, 1, tzinfo=UTC))

    clock_in = _london(GV10_TRANSITION_DAY, 1)
    clock_out = _london(GV10_TRANSITION_DAY, 10)

    # The day really is a transition day — otherwise the vector would pass for
    # the wrong reason on any ordinary date.
    day_start, day_end = _tz.local_day_bounds(db, organization_id,
                                              GV10_TRANSITION_DAY)
    assert day_end - day_start == timedelta(hours=23), (
        f"{GV10_TRANSITION_DAY} is not a spring-forward day in "
        f"{GV_DST_ZONE}: its local day is {day_end - day_start}")

    _clock_session_with_provenance(
        db, organization_id=organization_id, user_id=user_id,
        clock_in=clock_in, clock_out=clock_out)

    result = produce_and_seal_work_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=GV10_TRANSITION_DAY)
    assert result.outcome is ProducerOutcome.SEALED, result.refusal
    assert result.timezone_iana == GV_DST_ZONE

    intervals = (db.query(WorkStateInterval)
                 .filter(WorkStateInterval.ledger_id == result.ledger_id)
                 .order_by(WorkStateInterval.seq).all())
    assert [i.state for i in intervals] == [WorkState.WRK.value]

    # THE VECTOR, first half. Read, never recomputed.
    assert intervals[0].duration_seconds == expected["duration_hours"] * 3600
    handled.append("duration_hours")

    # ... and it is NOT the wall-clock span. Subtracting the shift's two STATED
    # readings — 10:00 minus 01:00 — is the naive arithmetic §24.8 warns about,
    # and it returns the 9h the vector explicitly forbids.
    stated = (datetime(GV10_TRANSITION_DAY.year, GV10_TRANSITION_DAY.month,
                       GV10_TRANSITION_DAY.day, 10)
              - datetime(GV10_TRANSITION_DAY.year, GV10_TRANSITION_DAY.month,
                         GV10_TRANSITION_DAY.day, 1))
    assert stated == timedelta(hours=9)
    assert intervals[0].duration_seconds != int(stated.total_seconds())

    # And the zone really was applied: 01:00 local does not exist on this day,
    # so the clock-in INSTANT reads 02:00 BST. That is why the stated reading,
    # not a round-trip, is the right thing to compare against.
    from zoneinfo import ZoneInfo
    assert clock_in.astimezone(ZoneInfo(GV_DST_ZONE)).hour == 2
    assert clock_in.utcoffset() == timedelta(0)          # the instant is UTC

    # The duration came from the boundary instants, not from a clock read and
    # not from the session's aggregate columns.
    assert _aware(intervals[0].start_at_utc) == clock_in
    assert _aware(intervals[0].end_at_utc) == clock_out
    assert intervals[0].duration_seconds not in (999999, 888, 777)

    # No double counting: one current ledger row, one interval, and re-running
    # the producer writes nothing further.
    # `work_date` is a DateTime column, so the day is addressed at midnight.
    at_midnight = datetime(GV10_TRANSITION_DAY.year, GV10_TRANSITION_DAY.month,
                           GV10_TRANSITION_DAY.day)
    rows = (db.query(WorkDayLedger)
            .filter(WorkDayLedger.organization_id == organization_id,
                    WorkDayLedger.user_id == user_id,
                    WorkDayLedger.work_date == at_midnight).all())
    assert len(rows) == 1 and rows[0].superseded_by is None
    assert rows[0].id == result.ledger_id
    assert rows[0].state == LedgerState.SEALED.value
    assert sum(i.duration_seconds for i in intervals) == \
        expected["duration_hours"] * 3600
    again = produce_and_seal_work_day(
        db, organization_id=organization_id, user_id=user_id,
        work_date=GV10_TRANSITION_DAY)
    assert again.outcome is ProducerOutcome.ALREADY_SEALED
    assert again.ledger_id == result.ledger_id
    assert db.query(WorkDayLedger).filter(
        WorkDayLedger.work_date == at_midnight).count() == 1

    # THE VECTOR, second half. The requirement is a duration, so the clock
    # change cannot move it: the day before the transition and the transition
    # day itself resolve to the same value.
    provider = DatabaseConfigVersionProvider(db)
    _capture_requirement(
        db, organization_id=organization_id, version_key="GV-10-v1",
        effective_from=date(2026, 3, 1), minutes=GV_REQUIRED_MINUTES)

    on_day = resolve_requirement_strict(
        organization_id=organization_id, user_id=user_id,
        as_of=GV10_TRANSITION_DAY, provider=provider)
    assert on_day.outcome is ResolutionOutcome.RESOLVED
    assert on_day.required_minutes == expected["required_minutes"]
    handled.append("required_minutes")

    day_before = resolve_requirement_strict(
        organization_id=organization_id, user_id=user_id,
        as_of=GV10_TRANSITION_DAY - timedelta(days=1), provider=provider)
    assert day_before.required_minutes == on_day.required_minutes, (
        "the requirement moved across the transition — §16.3 requires a "
        "duration, not a wall-clock span")

    _assert_exhaustive("GV-10", expected, handled)
    _tz.invalidate_tz_cache()


def test_gv11_dst_fall_back_week_is_169_hours(by_id, tenant, db):
    """GV-11, executed: "Week boundary correct; week is 169 hours; no day
    double-counted".

    Three separate claims, and each is answered by an existing primitive:

    * the boundary comes from ``schedule.week_start_day`` through W4's
      ``canonical_workweek_key``, which is pure DATE arithmetic and therefore
      cannot be moved by a clock change;
    * the 169 hours are composed from ``_tz.local_day_bounds``, which resolves
      each local day through the IANA zone;
    * no day is double-counted because the seven dates are distinct, each maps
      to exactly one week key, and their UTC spans tile the week without gap
      or overlap.

    Nothing here attributes a shift, reads a threshold, or touches EVAL-EVENT,
    variance, netting, horizon, grace, policy versioning or worker context.
    """
    organization_id, _user_id = tenant
    expected = by_id["GV-11"]["expected_fields"]
    handled = []

    _configure_dst_tenant(db, organization_id, week_start="MONDAY")

    # ── the boundary is the CONFIGURED one, and it is date-based ─────────────
    key = canonical_workweek_key(db, organization_id=organization_id,
                                 work_date=GV11_TRANSITION_DAY)
    assert key == GV11_WEEK_START.isoformat(), (
        f"the configured MONDAY start put the transition day in week {key!r}")

    dates = canonical_dates(key)
    assert dates is not None, f"{key!r} is not a canonical workweek key"
    assert len(dates) == DAYS_IN_WORKWEEK == 7
    assert len(set(dates)) == 7                      # seven DISTINCT dates
    assert dates[0] == GV11_WEEK_START
    assert GV11_TRANSITION_DAY in dates

    # Not ISO-week behaviour: the key is the start DATE, and a Sunday-start
    # tenant gets a different week for the same physical day (W4-3).
    from database.models import AppSettings
    row = (db.query(AppSettings)
           .filter(AppSettings.organization_id == organization_id,
                   AppSettings.key == SCHEDULE_KEYS.SETTING_WEEK_START_DAY)
           .one())
    row.value = "SUNDAY"
    db.flush()
    sunday_key = canonical_workweek_key(db, organization_id=organization_id,
                                        work_date=GV11_TRANSITION_DAY)
    assert sunday_key != key, (
        "the week start setting is not being honoured — the key did not move")
    assert sunday_key == GV11_TRANSITION_DAY.isoformat()
    row.value = "MONDAY"
    db.flush()
    assert canonical_workweek_key(db, organization_id=organization_id,
                                  work_date=GV11_TRANSITION_DAY) == key

    # ── the week's length, composed from timezone-aware local-day bounds ─────
    bounds = [_tz.local_day_bounds(db, organization_id, d) for d in dates]
    total = sum((end - start for start, end in bounds), timedelta())

    # THE VECTOR, first half.
    assert total == timedelta(hours=expected["week_length_hours"])
    assert total.total_seconds() / 3600 == expected["week_length_hours"]
    handled.append("week_length_hours")

    # The transition day is the long one, and every other day is 24h — so the
    # extra hour is where the zone actually puts it, not an off-by-one.
    long_days = [d for d, (s, e) in zip(dates, bounds)
                 if e - s != timedelta(hours=24)]
    assert long_days == [GV11_TRANSITION_DAY]
    assert dict(zip(dates, bounds))[GV11_TRANSITION_DAY][1] \
        - dict(zip(dates, bounds))[GV11_TRANSITION_DAY][0] \
        == timedelta(hours=25)

    # And it is NOT 168 hours — the assumption §24.8 says drifts twice a year.
    assert total != timedelta(hours=168)
    assert bounds[-1][1] - bounds[0][0] == total      # start-to-end agrees

    # ── no day double-counted ───────────────────────────────────────────────
    keys = {d: canonical_workweek_key(db, organization_id=organization_id,
                                      work_date=d)
            for d in dates}
    assert set(keys.values()) == {key}, (
        "a day in the week resolved to a different week key")

    overlaps = 0
    for (start, end), (next_start, _next_end) in zip(bounds, bounds[1:]):
        assert end == next_start, "the local days do not tile the week"
        if next_start < end:
            overlaps += 1
    duplicates = len(dates) - len(set(dates))

    # THE VECTOR, second half.
    assert overlaps + duplicates == expected["days_double_counted"]
    handled.append("days_double_counted")

    _assert_exhaustive("GV-11", expected, handled)
    _tz.invalidate_tz_cache()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Execution status is exact, in both directions
# ══════════════════════════════════════════════════════════════════════════════
def test_the_executable_set_is_exactly_the_twenty_eight_authorised_vectors():
    """Not "at least twenty-eight". Exactly these, and they are all 28.

    GV-26 was the last one out. Its STATE-CONTRACT citation was satisfied by
    the existing D-8.1 contract — LedgerState owns ledger facts, EcoDayState
    stays the seven-member evaluation vocabulary — and its ALERT citation was
    STALE rather than open: §24.15 asks for a platform INCIDENT, which has no
    audience, no recipient and no delivery, so ALERT's entry point could never
    have fired for it. Owner ruling D-9.1 authorized a dedicated primitive.

    The set being complete is NOT a reason to soften this guard. It is stated
    as an exact tuple for the same reason it always was: a vector quietly
    leaving it must fail here.
    """
    assert EXECUTABLE_IDS == ("GV-01", "GV-02", "GV-03", "GV-04", "GV-05",
                              "GV-06", "GV-07", "GV-08", "GV-09", "GV-10",
                              "GV-11", "GV-12", "GV-13", "GV-14", "GV-15",
                              "GV-16", "GV-17", "GV-18", "GV-19", "GV-20",
                              "GV-21", "GV-22", "GV-23", "GV-24", "GV-25",
                              "GV-26", "GV-27", "GV-28")
    assert len(EXECUTABLE_IDS) == 28
    assert set(EXECUTABLE_IDS) | set(NON_EXECUTABLE_IDS) == set(EXPECTED_IDS)
    assert not set(EXECUTABLE_IDS) & set(NON_EXECUTABLE_IDS)
    assert set(EXECUTABLE_IDS) == set(EXPECTED_IDS)
    assert NON_EXECUTABLE_IDS == ()


def test_the_artifact_agrees_with_the_executable_set(by_id):
    """The metadata and the harness must not be able to disagree.

    Exact in both directions: every vector this file executes is marked
    ``executable`` with no blocker, and every other vector is marked blocked
    with at least one. A vector cannot be quietly promoted in the artifact
    without an execution test, or quietly demoted while its test still runs.
    """
    for gid in EXECUTABLE_IDS:
        harness = by_id[gid]["harness"]
        assert harness["execution"] == EXECUTION_EXECUTABLE, gid
        assert harness["blocking_dependencies"] == [], gid

    # NON_EXECUTABLE_IDS is now empty, so this loop would assert nothing on its
    # own. The claim it used to make is therefore made directly: no vector in
    # the artifact is blocked, and none carries a blocker. A vector demoted
    # without its execution test being removed still fails here.
    assert NON_EXECUTABLE_IDS == ()
    for gid in NON_EXECUTABLE_IDS:
        harness = by_id[gid]["harness"]
        assert harness["execution"] == EXECUTION_BLOCKED, gid
        assert harness["blocking_dependencies"], gid
    for gid in EXPECTED_IDS:
        harness = by_id[gid]["harness"]
        assert harness["execution"] != EXECUTION_BLOCKED, gid
        assert harness["blocking_dependencies"] == [], gid

    executable_in_artifact = tuple(
        gid for gid in EXPECTED_IDS
        if by_id[gid]["harness"]["execution"] == EXECUTION_EXECUTABLE)
    assert executable_in_artifact == EXECUTABLE_IDS


#: The re-baselined inventory, transcribed from the owner ruling rather than
#: read back out of the artifact. Pinning it here is what makes a silent
#: reversion — say GV-04 drifting back to ``P-1`` — a test failure rather than
#: a quiet loss of the audit's findings.
EXPECTED_METADATA = {
    # PROMOTED 2026-09-21 under owner rulings D-1.1 and D-2.1. PRODUCER
    # had already been dropped as a stale citation; MODEL-VARIANCE closed
    # when the scheduled end became frozen ledger evidence resolved once
    # at seal, giving §18 step 14 its second operand.
    "GV-01": (EXECUTION_EXECUTABLE, []),
    # Promoted once P-2 landed: a post-seal classification artifact,
    # a versioned BreakTreatmentProfile, and the cumulative rest cap
    # applied in seconds before the single rounding site (§13.3).
    "GV-02": (EXECUTION_EXECUTABLE, []),
    # Promoted with GV-02. The negative twin is only expressible once
    # both categories can produce DIFFERENT answers — 500/25
    # differentiated against 470/55 collapsed (INV-4).
    "GV-03": (EXECUTION_EXECUTABLE, []),
    # Promoted once EVAL-EVENT was built and independently audited as
    # producing every one of their expected fields. All three assert only
    # event-generation and lifecycle facts.
    "GV-04": (EXECUTION_EXECUTABLE, []),
    "GV-05": (EXECUTION_EXECUTABLE, []),
    # GV-06 was NOT promoted with them. `further_notifications = 0` is a
    # POSITIVE claim about notification behaviour, which EVAL-EVENT having no
    # notification path cannot satisfy — unlike GV-28's structural-absence
    # `advisory_shown = false`. EVAL-EVENT is built, so citing it would read
    # as unblocked; ALERT is its true and only remaining blocker.
    # Promoted once ALERT's record/render half shipped: GV-06's dedup is a
    # property of the durable notification identity, which now exists.
    "GV-06": (EXECUTION_EXECUTABLE, []),
    # The owner split ALERT after the record/render half shipped. GV-07 cited
    # BOTH halves until Stage 5 satisfied RECIPIENT-AUTHORITY (ruling D-3); it
    # now cites operational delivery (R6) alone, and cites neither EVAL-EVENT
    # nor ALERT, because those are satisfied too and this list is blockers,
    # not usage.
    "GV-07": (EXECUTION_EXECUTABLE, []),
    "GV-08": (EXECUTION_EXECUTABLE, []),
    "GV-09": (EXECUTION_EXECUTABLE, []),
    # Promoted by the D18-F8 producer ruling: PRODUCER was their sole
    # blocker and it is satisfied. GV-12 joined them once the owner ruled
    # the rounding locus (Option A) and amended its input to a concrete
    # 36s deficit. GV-28 keeps PRODUCER: its NOT_A_WORKING_DAY ledger
    # state has no writer, so PRODUCER was never its only blocker.
    "GV-10": (EXECUTION_EXECUTABLE, []),
    "GV-11": (EXECUTION_EXECUTABLE, []),
    "GV-12": (EXECUTION_EXECUTABLE, []),
    "GV-13": (EXECUTION_EXECUTABLE, []),
    "GV-14": (EXECUTION_EXECUTABLE, []),
    "GV-15": (EXECUTION_EXECUTABLE, []),
    "GV-16": (EXECUTION_EXECUTABLE, []),
    # Owner ruling B3 re-pointed GV-17's second blocker. §24.7 is discharged by
    # the WF-ECO-03 / WF-ECO-04 explanation surfaces (§28.1, §29), not by a
    # notification: §33.1's matrix has no deferral row and §34 separates the
    # surface from delivery. Promoted once all three closed — the deferred copy
    # was authorized and registered, ``app.eco_view_authority`` answered who may
    # read, and ``/api/eco/day`` made the day readable by both parties.
    "GV-17": (EXECUTION_EXECUTABLE, []),
    "GV-18": (EXECUTION_EXECUTABLE, []),
    # PROMOTED 2026-09-22 under owner rulings D-6.3 through D-6.12. The
    # pair is the point: same evidence, different netting mode, divergent
    # answers (20 against 0).
    "GV-19": (EXECUTION_EXECUTABLE, []),
    "GV-20": (EXECUTION_EXECUTABLE, []),
    "GV-21": (EXECUTION_EXECUTABLE, []),
    # Same correction as GV-06: `notification_names_workweek` and
    # `notification_names_cause` are positive claims about notification COPY,
    # so ALERT blocks it, not the now-satisfied EVAL-EVENT.
    # Promoted with GV-06. Its workweek and cause come from the immutable
    # event and are rendered at record creation, so it needs no horizon.
    "GV-22": (EXECUTION_EXECUTABLE, []),
    # EVAL-EVENT was dropped as a STALE citation: the package records
    # is_retroactive over a persisted correction lineage, which is all
    # GV-23 asks of it. MODEL-HORIZON alone remains; GV-23 stays BLOCKED.
    # PROMOTED 2026-09-22 under owner rulings D-7.1 and D-7.2. EVAL-EVENT
    # had already been dropped as a stale citation; MODEL-HORIZON closed
    # when §21.5 got a decision: local calendar days in the frozen zone,
    # strictly greater-than, against the week's own policy.
    "GV-23": (EXECUTION_EXECUTABLE, []),
    # Promoted once §11 grace was resolved from the versioned Policy and
    # applied after the exclusion arithmetic: the day record now carries
    # grace_minutes_applied, so a zeroed attribution says what forgave it
    # instead of leaving gross - exclusion != attributable unexplained.
    "GV-24": (EXECUTION_EXECUTABLE, []),
    # Promoted once PolicyVersion existed, resolved historically and was
    # consumed at the evaluation boundary: §22.1's day-evaluation natural key
    # names policy_version, and until the artifact existed that component was
    # permanently NULL.
    "GV-25": (EXECUTION_EXECUTABLE, []),
    # Promoted once §24.15's third obligation had an implementation. Detection,
    # the refusal and LedgerState.LEDGER_INTEGRITY_FAULT were already built;
    # "raise a platform incident" was not, so the vector's third field was a
    # claim no test could fail. STATE-CONTRACT was satisfied by the existing
    # D-8.1 contract and no evaluation state was added; ALERT was a STALE
    # citation, an incident being evidence rather than a notification. Owner
    # ruling D-9.1 authorized app.eco_ledger_incident.
    "GV-26": (EXECUTION_EXECUTABLE, []),
    # Promoted once the Worker Context timeline was wired into requirement
    # resolution: classification-scoped ConfigValues let one version carry both
    # per-worker-type answers, so a mid-week reclassification changes what the
    # Worker owes per day without splitting the week's single threshold.
    "GV-27": (EXECUTION_EXECUTABLE, []),
    # Promoted once the dedicated working-day classifier gave
    # LedgerState.NOT_A_WORKING_DAY a production writer and EVAL-DAY began
    # consuming the frozen classification. PRODUCER alone was never its
    # only blocker, which is why it stayed behind GV-10/11/12.
    "GV-28": (EXECUTION_EXECUTABLE, []),
}


@pytest.mark.parametrize("gid", EXPECTED_IDS)
def test_the_rebaselined_metadata_is_exactly_the_audited_inventory(by_id, gid):
    """Per-vector, order-sensitive. A reverted classification fails here."""
    execution, deps = EXPECTED_METADATA[gid]
    harness = by_id[gid]["harness"]
    assert harness["execution"] == execution, gid
    assert harness["blocking_dependencies"] == deps, gid


def test_the_metadata_expectation_covers_every_vector():
    assert tuple(sorted(EXPECTED_METADATA)) == EXPECTED_IDS


#: Vectors executed OUTSIDE this file, each mapped to the suite and class that
#: runs it. A vector lands here when the behaviour it pins lives in another
#: module — the week fold for GV-12, the working-day classifier for GV-28 —
#: and naming the class is what keeps the guard real: a bare `"GV-28"` string
#: appears in this file several times already, so a mention proves nothing.
EXTERNAL_EXECUTION_TESTS = {
    "GV-12": ("test_eval_week_fold.py", "class TestGV12"),
    "GV-28": ("test_working_day_classifier.py", "class TestGV28"),
    # §15's netting pair. Executed together in one suite because neither
    # proves anything alone: the vectors pass only when the SAME evidence
    # under different modes produces DIFFERENT answers.
    # §21.5's horizon. Executed in its own suite because the scenario is a
    # real correction to a real sealed week, 120 local days later.
    "GV-23": ("test_horizon.py", "class TestGV23"),
    # §21.3's de-crossing. Executed in the command suite because the vector is
    # about DELIVERY to "all original recipients", and the only way to prove
    # that is to walk the whole authorized chain — command, rollout gate,
    # processing pass, platform handoff, delivery record — for two recipients.
    "GV-07": ("test_dispatch_command.py", "class TestGV07EndToEnd"),
    # §24.3's requirement-unresolved administrator alert. Executed in the
    # day-alert suite because the vector is about a DAY that emits no threshold
    # event, which is precisely why it needed its own primitive.
    "GV-18": ("test_eco_day_alert.py", "class TestGV18"),
    "GV-19": ("test_netting.py", "class TestGV19"),
    "GV-20": ("test_netting.py", "class TestGV20"),
    # §6.2's three-measurement vector. Executed in its own suite because the
    # fixture it needs is a whole sealed day — two WRK segments, a meal gap, a
    # frozen scheduled end and an intraday leave window — and because the
    # divergence it proves is only visible with all three numbers in hand.
    "GV-01": ("test_gv01_execution.py", "class TestGV01"),
    # The three EVAL-EVENT lifecycle vectors. Executed in the suite that owns
    # the behaviour, for the same reason as GV-12 and GV-28: the fixtures that
    # build a real week and a real threshold already live there.
    "GV-04": ("test_eval_event.py", "class TestGV04"),
    "GV-05": ("test_eval_event.py", "class TestGV05"),
    "GV-08": ("test_eval_event.py", "class TestGV08"),
    # The two ALERT record/render vectors. Executed in the suite that owns the
    # behaviour: GV-06's deduplication and GV-22's retroactive rendering are
    # both properties of the durable notification, not of the event.
    "GV-06": ("test_alert.py", "class TestGV06"),
    "GV-22": ("test_alert.py", "class TestGV22"),
    # The cross-week vector. Executed where the behaviour lives: its scenario
    # runs producer -> ledger -> MODEL-WORKWEEK -> EVAL-DAY -> EVAL-WEEK, and
    # the fixtures that build a real cross-midnight session and a real week
    # already exist there.
    "GV-09": ("test_model_workweek.py", "class TestCrossWeekEndToEnd"),
    # The missing-clock-out vector. Executed beside the primitive that decides
    # the condition: departure evidence -> departure_resolution -> producer ->
    # ledger -> EVAL-DAY -> EVAL-WEEK. The suite that owns §24.6 also owns the
    # negative cases that must NOT become it.
    "GV-16": ("test_departure_resolution.py", "class TestGV16"),
    # The deferred-exit vector. Its own suite, because no existing one owns the
    # whole span: departure evidence -> producer -> ledger -> EVAL-DAY ->
    # EVAL-WEEK -> read_eco_day -> ViewAuthority -> WF-ECO-03 rendering, and
    # then the resolution transition that supersedes both the ledger and the
    # evaluation.
    "GV-17": ("test_gv17_execution.py", "class TestGV17"),
    # The mid-week reclassification vector. Its own suite, because the span is
    # Worker Context -> classification-scoped configuration -> requirement
    # resolution -> EVAL-DAY -> EVAL-WEEK -> EVAL-EVENT, and no existing suite
    # owns both ends of it.
    "GV-27": ("test_gv27_execution.py", "class TestGV27"),
    # The idempotent-replay vector. Its own suite: the span is PolicyVersion ->
    # requirement -> producer -> EVAL-DAY -> EVAL-WEEK -> EVAL-EVENT -> the
    # durable notification record, replayed at each of the three points the
    # vector names.
    "GV-25": ("test_gv25_execution.py", "class TestGV25"),
    # The grace vector. Its own suite: the scenario needs a policy whose grace
    # BITES, and every other ECO suite deliberately captures grace_minutes=0 so
    # its own arithmetic stays unaffected by the seam.
    "GV-24": ("test_gv24_execution.py", "class TestGV24"),
    # The meal/rest pair. Their own suite: the span is BreakTreatmentProfile ->
    # classification authority -> authoritative assertions -> sealed ledger ->
    # EVAL-DAY, and GV-03 needs BOTH treatments evaluated from one ledger to
    # show they diverge.
    "GV-02": ("test_gv02_gv03_execution.py", "class TestGV02"),
    "GV-03": ("test_gv02_gv03_execution.py", "class TestGV03"),
    # §24.15's ledger integrity fault. Executed beside the primitive that
    # discharges its fourth field: the span is clock evidence -> producer ->
    # work_ledger.integrity -> the fault version -> the platform incident ->
    # EVAL-DAY's refusal, and the incident store is what makes
    # platform_incident_raised answerable at all.
    "GV-26": ("test_eco_ledger_incident.py", "class TestGV26"),
}


def test_every_executable_vector_has_an_execution_test():
    """A vector cannot be declared executable without a test that runs it."""
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    here = pathlib.Path(__file__).parent
    for gid, (filename, class_decl) in EXTERNAL_EXECUTION_TESTS.items():
        suite = here / filename
        assert suite.exists(), f"{gid}'s execution suite {filename} is missing"
        assert class_decl in suite.read_text(encoding="utf-8"), (
            f"{gid} is declared executable but {filename} no longer defines "
            f"{class_decl}")
    for gid in EXECUTABLE_IDS:
        if gid in EXTERNAL_EXECUTION_TESTS:
            continue
        assert f'"{gid}"' in source, f"{gid} is declared executable untested"
    # Every externally executed vector must actually be declared executable.
    assert set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)
    # And the three leave vectors are parametrised into the exclusion test.
    assert '("GV-13", "GV-14", "GV-15")' in source
