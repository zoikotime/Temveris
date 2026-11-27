"""
STEP 2 — interval provenance integrated with the canonical ledger.
==================================================================

Step 1 gave intervals a provenance column and a vocabulary but enforced only
that the value was truthy: a typo, a case variant or an invented value would
have been accepted and persisted, and nothing stopped a persisted provenance
being rewritten in place. Step 2 closes both, and makes provenance readable at
the ledger level.

INDEPENDENT ASSERTIONS
----------------------
The eight approved values are transcribed here from the owner ruling, NOT
imported from the implementation. Parametrising over ``Provenance`` alone would
only prove the module is self-consistent: deleting a value would delete its own
test with it. Comparing two separate readings is what makes a drop fail.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone

import pytest

from app.work_ledger import (
    CandidateInterval,
    DayKey,
    DERIVED_BOUNDARY_PROVENANCE,
    IntegrityFault,
    LedgerEvidenceConflict,
    LedgerIntegrityFault,
    Provenance,
    ProvenanceClass,
    ProvenanceImmutable,
    SYNTHESIZED_CLOSURE_PROVENANCE,
    VERIFIED_DEPARTURE_PROVENANCE,
    WorkDayLedger,
    WorkState,
    WorkStateInterval,
    assert_provenance_vocabulary_closed,
    classify_provenance,
    is_valid_provenance,
    parse_provenance,
    read_intervals,
    record_integrity_fault,
    seal_day,
    summarise_ledger_provenance,
    summarise_provenance,
    supersede_day,
    validate_intervals,
    verified_departure_intervals,
)

UTC = timezone.utc
TZ = "Europe/London"
DAY = date(2026, 9, 4)

# ── The owner ruling, transcribed independently of the implementation ────────
OWNER_APPROVED_PROVENANCE = (
    "WORKER",
    "ADMIN_OVERRIDE",
    "STALE_AUTOCLOSE",
    "ABANDONED_AUTOCLOSE",
    "OFFBOARDING",
    "CLASSIFIER_DERIVED",
    "BREAK_REPAIR",
    "BREAK_DERIVED",
    # Decision D-18A: the forward-only producer authors boundaries at points
    # that are not session departures — a complete break split, and the edges
    # of an observed connectivity gap — and none of the original eight
    # described them truthfully.
    "PRODUCER_DERIVED",
)

OWNER_VERIFIED = ("WORKER", "ADMIN_OVERRIDE")
OWNER_SYNTHESIZED = ("STALE_AUTOCLOSE", "ABANDONED_AUTOCLOSE", "OFFBOARDING")
OWNER_DERIVED = ("CLASSIFIER_DERIVED", "BREAK_REPAIR", "BREAK_DERIVED",
                 "PRODUCER_DERIVED")


@pytest.fixture(scope="module", autouse=True)
def _ledger_schema(engine):
    from database.models import AppSettings, Organization, User

    from app.eco_ledger_incident import EcoLedgerIncident

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    AppSettings.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    # §24.15's third obligation — see test_work_ledger_step1.py.
    EcoLedgerIncident.__table__.create(engine, checkfirst=True)


@pytest.fixture(autouse=True)
def _workweek_boundary(db):
    """Every organization here must have a defined workweek (§12.1).

    MONDAY throughout — what the resolver used to assume — so no provenance
    expectation in this file changes; only the fact that the boundary is now
    stated rather than guessed.
    """
    from app.schedule_policy import keys as schedule_keys
    from database.models import AppSettings

    for org in range(201, 217):
        db.add(AppSettings(
            organization_id=org, team_key=None, user_id=None,
            key=schedule_keys.SETTING_WEEK_START_DAY, value="MONDAY"))
    db.flush()


def _at(hh, mm=0):
    return datetime(2026, 9, 4, hh, mm, tzinfo=UTC)


def _iv(seq, start, end, prov, state=WorkState.WRK.value):
    return CandidateInterval(seq=seq, state=state, start_at_utc=start,
                             end_at_utc=end, provenance=prov)


def _day(org, user=900):
    return DayKey(organization_id=org, user_id=user, work_date=DAY)


# ══════════════════════════════════════════════════════════════════════════════
# 1. The vocabulary is exactly the eight approved values
# ══════════════════════════════════════════════════════════════════════════════

def test_vocabulary_matches_the_owner_ruling_exactly():
    """Compared against an independent transcription, not against itself."""
    implemented = {p.value for p in Provenance}
    assert implemented == set(OWNER_APPROVED_PROVENANCE)
    assert len(implemented) == 9


@pytest.mark.parametrize("value", OWNER_APPROVED_PROVENANCE)
def test_every_approved_value_is_accepted(value):
    assert is_valid_provenance(value) is True
    assert parse_provenance(value).value == value


@pytest.mark.parametrize("bad", [
    "WORKR",            # typo
    "worker",           # case variant
    "Worker",
    "IMPORTED",         # invented
    "MANUAL",
    "",
    None,
    123,
    "WORKER ",          # trailing space
])
def test_unapproved_values_are_rejected(bad):
    """A ninth provenance value is an owner ruling, not a code change."""
    assert is_valid_provenance(bad) is False
    with pytest.raises(ValueError):
        parse_provenance(bad)


def test_rejection_message_names_the_approved_set():
    """An operator hitting this needs to know what IS allowed."""
    with pytest.raises(ValueError) as exc:
        parse_provenance("IMPORTED")
    msg = str(exc.value)
    assert "IMPORTED" in msg
    for value in OWNER_APPROVED_PROVENANCE:
        assert value in msg


# ══════════════════════════════════════════════════════════════════════════════
# 2. Verified / synthesized / derived classification
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("value", OWNER_VERIFIED)
def test_verified_values_classify_as_verified(value):
    assert classify_provenance(value) is ProvenanceClass.VERIFIED


@pytest.mark.parametrize("value", OWNER_SYNTHESIZED)
def test_synthesized_values_classify_as_synthesized(value):
    """A closure the Worker never performed is never verified evidence."""
    assert classify_provenance(value) is ProvenanceClass.SYNTHESIZED


@pytest.mark.parametrize("value", OWNER_DERIVED)
def test_derived_values_classify_as_derived(value):
    assert classify_provenance(value) is ProvenanceClass.DERIVED


def test_the_nine_values_partition_exactly_into_three_classes():
    """No value is in two classes, and none is unclassified — otherwise a
    provenance could be neither trusted nor distrusted."""
    v, s, d = (set(VERIFIED_DEPARTURE_PROVENANCE),
               set(SYNTHESIZED_CLOSURE_PROVENANCE),
               set(DERIVED_BOUNDARY_PROVENANCE))
    assert v & s == set() and v & d == set() and s & d == set()
    assert v | s | d == set(Provenance)
    assert (len(v), len(s), len(d)) == (2, 3, 4)


def test_break_repair_and_break_derived_are_not_collapsed_into_worker():
    """Owner ruling: do not collapse either into ordinary worker provenance."""
    for value in ("BREAK_REPAIR", "BREAK_DERIVED"):
        assert classify_provenance(value) is not ProvenanceClass.VERIFIED
        assert parse_provenance(value) is not Provenance.WORKER


def test_classification_refuses_an_unapproved_value_rather_than_defaulting():
    """Guessing the class of an unknown provenance is how synthesized evidence
    would acquire verified status by accident."""
    with pytest.raises(ValueError):
        classify_provenance("IMPORTED")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Integrity refuses invalid provenance (the Step 1 gap)
# ══════════════════════════════════════════════════════════════════════════════

def test_invalid_provenance_is_refused_by_validation():
    bad = [_iv(1, _at(9), _at(17), "IMPORTED")]
    r = validate_intervals(bad)
    assert r.admissible is False
    assert any(v.fault is IntegrityFault.INVALID_PROVENANCE for v in r.violations)


def test_case_variant_provenance_is_refused():
    r = validate_intervals([_iv(1, _at(9), _at(17), "worker")])
    assert r.admissible is False
    assert any(v.fault is IntegrityFault.INVALID_PROVENANCE for v in r.violations)


def test_missing_and_invalid_are_distinct_faults():
    """An operator fixing 'absent' does something different from 'wrong'."""
    missing = validate_intervals([_iv(1, _at(9), _at(17), None)])
    invalid = validate_intervals([_iv(1, _at(9), _at(17), "NOPE")])
    assert any(v.fault is IntegrityFault.MISSING_PROVENANCE for v in missing.violations)
    assert any(v.fault is IntegrityFault.INVALID_PROVENANCE for v in invalid.violations)


def test_invalid_provenance_is_caught_even_when_provenance_is_not_required():
    """An unapproved value is wrong whether or not one was demanded."""
    r = validate_intervals([_iv(1, _at(9), _at(17), "NOPE")],
                           require_provenance=False)
    assert r.admissible is False


def test_sealing_refuses_invalid_provenance_and_writes_nothing(db):
    key = _day(org=201)
    with pytest.raises(LedgerIntegrityFault):
        seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "IMPORTED")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    db.rollback()
    from app.work_ledger import get_current_ledger
    assert get_current_ledger(db, key) is None


# ══════════════════════════════════════════════════════════════════════════════
# 4. Immutability
# ══════════════════════════════════════════════════════════════════════════════

def test_provenance_cannot_be_rewritten_in_place(db):
    key = _day(org=202)
    row = seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "WORKER")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    iv = read_intervals(db, row)[0]
    with pytest.raises(ProvenanceImmutable):
        iv.provenance = Provenance.ADMIN_OVERRIDE.value
    db.rollback()


def test_reassigning_the_same_provenance_is_not_a_mutation(db):
    """Idempotent writes must not trip the guard."""
    key = _day(org=203)
    row = seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "WORKER")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    iv = read_intervals(db, row)[0]
    iv.provenance = "WORKER"  # same value — allowed
    assert iv.provenance == "WORKER"


def test_initial_assignment_is_allowed():
    """Construction is not mutation."""
    fresh = WorkStateInterval(
        ledger_id=1, organization_id=1, seq=1, state=WorkState.WRK.value,
        start_at_utc=_at(9), end_at_utc=_at(17), duration_seconds=28800,
        provenance=Provenance.WORKER.value,
    )
    assert fresh.provenance == "WORKER"


def test_immutability_survives_a_reload(db):
    """The guard must key on the persisted value, not on a session cache."""
    key = _day(org=204)
    row = seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "STALE_AUTOCLOSE")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    db.flush()
    db.expire_all()
    iv = read_intervals(db, row)[0]
    with pytest.raises(ProvenanceImmutable):
        iv.provenance = "WORKER"
    db.rollback()


def test_synthesized_provenance_cannot_be_upgraded_to_verified(db):
    """The specific attack the immutability rule exists to stop: relabelling a
    fabricated departure as a real one."""
    key = _day(org=205)
    row = seal_day(db, key,
                   intervals=[_iv(1, _at(9), _at(17), "ABANDONED_AUTOCLOSE")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    iv = read_intervals(db, row)[0]
    with pytest.raises(ProvenanceImmutable):
        iv.provenance = "WORKER"
    # The guard raises BEFORE the attribute is set, so the original value must
    # still stand. Asserted without rolling back — a rollback would discard the
    # seal itself and prove nothing about the guard.
    assert iv.provenance == "ABANDONED_AUTOCLOSE"
    assert read_intervals(db, row)[0].provenance == "ABANDONED_AUTOCLOSE"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Provenance through sealing, supersession and replay
# ══════════════════════════════════════════════════════════════════════════════

def test_provenance_is_preserved_through_sealing(db):
    key = _day(org=206)
    ivs = [
        _iv(1, _at(9), _at(13), "WORKER"),
        _iv(2, _at(13), _at(13, 45), "BREAK_DERIVED", WorkState.BRK_M.value),
        _iv(3, _at(13, 45), _at(17), "CLASSIFIER_DERIVED"),
    ]
    row = seal_day(db, key, intervals=ivs,
                   timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert [i.provenance for i in read_intervals(db, row)] == [
        "WORKER", "BREAK_DERIVED", "CLASSIFIER_DERIVED",
    ]


def test_provenance_is_preserved_through_supersession(db):
    """The superseded version keeps its own provenance; the new version keeps
    its own. Neither is rewritten (INV-3, §23)."""
    key = _day(org=207)
    v1 = seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "ABANDONED_AUTOCLOSE")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    v2 = supersede_day(db, key, expected_version=1,
                       intervals=[_iv(1, _at(9), _at(17, 30), "ADMIN_OVERRIDE")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")

    from app.work_ledger import get_ledger_version
    old = get_ledger_version(db, key, 1)
    assert read_intervals(db, old)[0].provenance == "ABANDONED_AUTOCLOSE"
    assert read_intervals(db, v2)[0].provenance == "ADMIN_OVERRIDE"


def test_idempotent_replay_preserves_provenance(db):
    key = _day(org=208)
    ivs = [_iv(1, _at(9), _at(17), "OFFBOARDING")]
    first = seal_day(db, key, intervals=ivs,
                     timezone_iana=TZ, is_working_day=True, working_day_source="org")
    again = seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "OFFBOARDING")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert first.id == again.id
    assert read_intervals(db, again)[0].provenance == "OFFBOARDING"


def test_replay_with_changed_provenance_is_a_conflict_not_a_rewrite(db):
    """Same boundaries, different provenance, is different evidence."""
    key = _day(org=209)
    seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "STALE_AUTOCLOSE")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    with pytest.raises(LedgerEvidenceConflict):
        seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "WORKER")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")


# ══════════════════════════════════════════════════════════════════════════════
# 6. Ledger-level provenance integration
# ══════════════════════════════════════════════════════════════════════════════

def test_summary_counts_by_value_and_class(db):
    key = _day(org=210)
    row = seal_day(db, key, intervals=[
        _iv(1, _at(9), _at(12), "WORKER"),
        _iv(2, _at(12), _at(12, 30), "BREAK_DERIVED", WorkState.BRK_R.value),
        _iv(3, _at(12, 30), _at(17), "CLASSIFIER_DERIVED"),
    ], timezone_iana=TZ, is_working_day=True, working_day_source="org")

    s = summarise_ledger_provenance(db, row)
    assert s.interval_count == 3
    assert s.by_value["WORKER"] == 1
    assert s.by_class[ProvenanceClass.VERIFIED.value] == 1
    assert s.by_class[ProvenanceClass.DERIVED.value] == 2


def test_day_with_only_synthesized_evidence_has_no_verified_departure(db):
    """The input to a §24.6 MISSING_CLOCK_OUT determination — which the
    classifier makes at Step 3, not here."""
    key = _day(org=211)
    row = seal_day(db, key, intervals=[
        _iv(1, _at(9), _at(17), "ABANDONED_AUTOCLOSE"),
    ], timezone_iana=TZ, is_working_day=True, working_day_source="org")

    s = summarise_ledger_provenance(db, row)
    assert s.has_verified_departure_evidence is False
    assert s.has_synthesized_closure is True


def test_day_with_a_real_clock_out_has_verified_departure(db):
    key = _day(org=212)
    row = seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "WORKER")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert summarise_ledger_provenance(db, row).has_verified_departure_evidence


def test_wholly_derived_day_is_reported_as_such(db):
    key = _day(org=213)
    row = seal_day(db, key, intervals=[
        _iv(1, _at(9), _at(12), "CLASSIFIER_DERIVED"),
        _iv(2, _at(12), _at(17), "BREAK_REPAIR", WorkState.BRK_M.value),
    ], timezone_iana=TZ, is_working_day=True, working_day_source="org")
    s = summarise_ledger_provenance(db, row)
    assert s.is_wholly_derived is True
    assert s.has_verified_departure_evidence is False


def test_verified_departure_intervals_filters_correctly(db):
    key = _day(org=214)
    row = seal_day(db, key, intervals=[
        _iv(1, _at(9), _at(12), "WORKER"),
        _iv(2, _at(12), _at(17), "STALE_AUTOCLOSE"),
    ], timezone_iana=TZ, is_working_day=True, working_day_source="org")
    got = verified_departure_intervals(read_intervals(db, row))
    assert [i.seq for i in got] == [1]


def test_summary_of_an_empty_ledger_is_not_verified(db):
    """An integrity-fault day carries no intervals; it must not read as
    verified evidence."""
    key = _day(org=215)
    row = record_integrity_fault(db, key, reason="overlapping sealed intervals",
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    s = summarise_ledger_provenance(db, row)
    assert s.interval_count == 0
    assert s.has_verified_departure_evidence is False
    assert s.is_wholly_derived is False


def test_summary_refuses_an_unapproved_persisted_value():
    """A vocabulary breach that bypassed validation must surface, not be
    bucketed as 'other'."""
    class _Fake:
        provenance = "IMPORTED"
    with pytest.raises(ValueError):
        summarise_provenance([_Fake()])


def test_vocabulary_probe_detects_a_breach():
    class _Fake:
        id = 7
        provenance = "IMPORTED"
    with pytest.raises(ValueError):
        assert_provenance_vocabulary_closed([_Fake()])


def test_vocabulary_probe_passes_on_approved_values(db):
    key = _day(org=216)
    row = seal_day(db, key, intervals=[_iv(1, _at(9), _at(17), "WORKER")],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert_provenance_vocabulary_closed(read_intervals(db, row))  # no raise


# ══════════════════════════════════════════════════════════════════════════════
# 7. Architectural guards still hold after Step 2
# ══════════════════════════════════════════════════════════════════════════════

def _code_identifiers(mod) -> set:
    import ast
    tree = ast.parse(inspect.getsource(mod))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.update(a.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_provenance_module_computes_no_worked_time():
    """Counts, not durations. A duration sum here would be the start of a
    second worked-time authority."""
    from app.work_ledger import provenance as mod
    ids = _code_identifiers(mod)
    for forbidden in ("session_worked_seconds", "_worked", "worked_seconds",
                      "duration_seconds", "total_seconds"):
        assert forbidden not in ids, f"provenance.py references {forbidden!r}"


def test_provenance_module_has_no_activity_or_screenshot_dependency():
    from app.work_ledger import provenance as mod
    ids = _code_identifiers(mod)
    for forbidden in ("active_time", "Activity", "ActivityWindow",
                      "BrowserActivity", "AppSession", "Screenshot"):
        assert forbidden not in ids


def test_provenance_module_does_not_read_legacy_session_tables():
    from app.work_ledger import provenance as mod
    ids = _code_identifiers(mod)
    for forbidden in ("ClockSession", "BreakSession", "OfflineInterval"):
        assert forbidden not in ids


def test_step_2_did_not_wire_worked_py_to_the_ledger():
    """Step 9 has not happened. `_worked.py` must still be untouched."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parent.parent
    worked = (backend / "app" / "_worked.py").read_text(encoding="utf-8")
    assert "work_ledger" not in worked
    assert "def session_worked_seconds" in worked


def test_step_2_did_not_re_enable_eco_v1():
    import pathlib
    backend = pathlib.Path(__file__).resolve().parent.parent
    router = (backend / "app" / "employees" / "router.py").read_text(
        encoding="utf-8", errors="replace")
    assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
