"""
STEP 1 — canonical sealed work-state interval ledger.
=====================================================

Covers the Step 1 safety requirements from the owner ruling: additive schema,
integer seconds, optimistic concurrency, idempotency, sealing, supersession,
tenant isolation, provenance on every interval, no activity/screenshot
dependency, no ECO-local worked-time calculation.

The ledger is NOT wired to clock-out in Step 1, so there is nothing here that
exercises a request path. These tests exercise the primitive directly.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.work_ledger import (
    BREAK_STATES,
    CandidateInterval,
    DayKey,
    IntegrityFault,
    LedgerEvidenceConflict,
    LedgerIntegrityFault,
    LedgerState,
    Provenance,
    RESERVED_UNSOURCED_STATES,
    StaleLedgerVersion,
    SYNTHESIZED_CLOSURE_PROVENANCE,
    VERIFIED_DEPARTURE_PROVENANCE,
    WorkDayLedger,
    WorkState,
    WorkStateInterval,
    duration_seconds,
    get_current_ledger,
    get_ledger_version,
    is_verified_departure,
    order_intervals,
    read_intervals,
    record_integrity_fault,
    seal_day,
    supersede_day,
    validate_intervals,
)

UTC = timezone.utc
DAY = date(2026, 9, 4)
TZ = "Europe/London"


@pytest.fixture(scope="module", autouse=True)
def _ledger_schema(engine):
    """Create the two ledger tables.

    conftest builds the test schema per-model rather than via
    ``Base.metadata.create_all``, so new tables opt in explicitly.
    """
    from database.models import Organization, User

    from database.models import AppSettings

    from app.eco_ledger_incident import EcoLedgerIncident

    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    AppSettings.__table__.create(engine, checkfirst=True)
    WorkDayLedger.__table__.create(engine, checkfirst=True)
    WorkStateInterval.__table__.create(engine, checkfirst=True)
    # §24.15's third obligation: establishing LEDGER_INTEGRITY_FAULT now
    # raises a platform incident, so a schema that supports the ledger writer
    # has to carry the incident store. Omitting it would not make the ledger
    # writer simpler — it would make it fail.
    EcoLedgerIncident.__table__.create(engine, checkfirst=True)


#: Every organization this module seals a day for. §12.1 requires a tenant to
#: have defined its workweek before Early Clock-Out can be enabled, and the ECO
#: boundary resolver now refuses an organization that has not — so a fixture
#: that seals without one is modelling a tenant the specification rejects.
_ORGS = (1, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113,
         120, 121, 122, 123, 999)


@pytest.fixture(autouse=True)
def _workweek_boundary(db):
    """Give every organization in this module a defined workweek.

    MONDAY throughout, which is what the lenient resolver used to default to,
    so every grouping expectation here is unchanged — only the fact that the
    boundary is now stated rather than assumed.
    """
    from app.schedule_policy import keys as schedule_keys
    from database.models import AppSettings

    for org in _ORGS:
        db.add(AppSettings(
            organization_id=org, team_key=None, user_id=None,
            key=schedule_keys.SETTING_WEEK_START_DAY, value="MONDAY"))
    db.flush()


def _at(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 9, 4, hh, mm, tzinfo=UTC)


def _iv(seq, state, start, end, prov=Provenance.WORKER.value, src=None):
    return CandidateInterval(
        seq=seq, state=state, start_at_utc=start, end_at_utc=end,
        provenance=prov, source_ref=src,
    )


def _day(org=1, user=1, on=DAY) -> DayKey:
    return DayKey(organization_id=org, user_id=user, work_date=on)


def _valid_set():
    """A clean day: work, unpaid meal, work."""
    return [
        _iv(1, WorkState.WRK.value, _at(9), _at(13)),
        _iv(2, WorkState.BRK_M.value, _at(13), _at(13, 45),
            prov=Provenance.CLASSIFIER_DERIVED.value),
        _iv(3, WorkState.WRK.value, _at(13, 45), _at(17, 30)),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# 1. Vocabularies
# ══════════════════════════════════════════════════════════════════════════════

def test_all_eight_canonical_work_states_exist():
    assert {s.value for s in WorkState} == {
        "WRK", "TRV", "ONC", "BRK_M", "BRK_R", "OFF", "LVE", "UNK"
    }


def test_meal_and_rest_are_distinct_states():
    """INV-4: a single generic 'break' category MUST NOT exist in this control."""
    assert WorkState.BRK_M != WorkState.BRK_R
    assert BREAK_STATES == {WorkState.BRK_M, WorkState.BRK_R}


def test_travel_and_oncall_are_reserved_because_no_source_exists():
    """Recording the gap is honest; inventing a source is not."""
    assert RESERVED_UNSOURCED_STATES == {WorkState.TRV, WorkState.ONC}


def test_all_nine_owner_approved_provenance_values_exist():
    """Eight approved at Gate 6, plus PRODUCER_DERIVED by decision D-18A.

    D-18A was reached through the route ``parse_provenance`` reserves: the
    producer audit stopped, reported that no existing value could truthfully
    describe a producer-authored non-departure boundary, and the owner ruled.
    """
    assert {p.value for p in Provenance} == {
        "WORKER", "ADMIN_OVERRIDE", "STALE_AUTOCLOSE", "ABANDONED_AUTOCLOSE",
        "OFFBOARDING", "CLASSIFIER_DERIVED", "BREAK_REPAIR", "BREAK_DERIVED",
        "PRODUCER_DERIVED",
    }


def test_break_repair_and_break_derived_are_separate_values():
    """Owner explicitly approved them as separate: one records that a source
    record was written, the other that it was not."""
    assert Provenance.BREAK_REPAIR != Provenance.BREAK_DERIVED


def test_only_worker_and_admin_override_are_verified_departure_evidence():
    assert VERIFIED_DEPARTURE_PROVENANCE == {
        Provenance.WORKER, Provenance.ADMIN_OVERRIDE,
    }
    for p in SYNTHESIZED_CLOSURE_PROVENANCE:
        assert not is_verified_departure(p), p


@pytest.mark.parametrize("prov", [
    Provenance.STALE_AUTOCLOSE, Provenance.ABANDONED_AUTOCLOSE,
    Provenance.OFFBOARDING, Provenance.CLASSIFIER_DERIVED,
    Provenance.BREAK_REPAIR, Provenance.BREAK_DERIVED,
    Provenance.PRODUCER_DERIVED,
])
def test_synthesized_and_derived_boundaries_are_never_verified(prov):
    """§7.2 — a synthesized departure must never silently acquire the
    evidentiary status of a genuine Worker departure."""
    assert is_verified_departure(prov) is False


def test_unknown_provenance_is_not_verified():
    """Fail safe: the cost of wrongly trusting a fabricated departure is a
    shortfall computed against a time the Worker never chose."""
    assert is_verified_departure("SOMETHING_ELSE") is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. Integrity — refusal, never repair (§24.15)
# ══════════════════════════════════════════════════════════════════════════════

def test_clean_set_is_admissible():
    assert validate_intervals(_valid_set()).admissible is True


def test_non_positive_duration_is_refused():
    bad = [_iv(1, WorkState.WRK.value, _at(9), _at(9))]
    r = validate_intervals(bad)
    assert r.admissible is False
    assert r.violations[0].fault is IntegrityFault.NON_POSITIVE_DURATION


def test_reversed_interval_is_refused():
    bad = [_iv(1, WorkState.WRK.value, _at(13), _at(9))]
    r = validate_intervals(bad)
    assert r.admissible is False
    assert any(v.fault is IntegrityFault.NON_POSITIVE_DURATION for v in r.violations)


def test_overlapping_intervals_are_refused_not_merged():
    """The platform's existing break helper UNIONS overlaps by design
    (employees/router.py:2175, :2853). The ledger must do the opposite."""
    bad = [
        _iv(1, WorkState.WRK.value, _at(9), _at(13)),
        _iv(2, WorkState.WRK.value, _at(12), _at(14)),
    ]
    r = validate_intervals(bad)
    assert r.admissible is False
    assert any(v.fault is IntegrityFault.OVERLAPPING_INTERVALS for v in r.violations)


def test_touching_intervals_are_legal():
    """Half-open [start, end): one ending exactly as the next begins is fine."""
    ok = [
        _iv(1, WorkState.WRK.value, _at(9), _at(13)),
        _iv(2, WorkState.BRK_R.value, _at(13), _at(13, 15)),
    ]
    assert validate_intervals(ok).admissible is True


def test_naive_timestamp_is_refused():
    """§16.4 prohibits duration arithmetic on naive datetimes."""
    bad = [CandidateInterval(
        seq=1, state=WorkState.WRK.value,
        start_at_utc=datetime(2026, 9, 4, 9),   # naive
        end_at_utc=datetime(2026, 9, 4, 17),    # naive
        provenance=Provenance.WORKER.value,
    )]
    r = validate_intervals(bad)
    assert r.admissible is False
    assert any(v.fault is IntegrityFault.NAIVE_TIMESTAMP for v in r.violations)


def test_missing_provenance_is_refused():
    """No interval may exist without provenance (Gate 6 / C-02)."""
    bad = [_iv(1, WorkState.WRK.value, _at(9), _at(17), prov=None)]
    r = validate_intervals(bad)
    assert r.admissible is False
    assert any(v.fault is IntegrityFault.MISSING_PROVENANCE for v in r.violations)


def test_validation_reports_every_violation_not_just_the_first():
    bad = [
        _iv(1, WorkState.WRK.value, _at(9), _at(9)),
        _iv(2, WorkState.WRK.value, _at(10), _at(12), prov=None),
    ]
    r = validate_intervals(bad)
    faults = {v.fault for v in r.violations}
    assert IntegrityFault.NON_POSITIVE_DURATION in faults
    assert IntegrityFault.MISSING_PROVENANCE in faults


def test_empty_set_is_admissible():
    """A day with no intervals is not corrupt; it is a day with no recorded
    work. Which state it takes is a separate determination."""
    assert validate_intervals([]).admissible is True


def test_validation_never_mutates_its_input():
    """'MUST NOT repair it, skip it, or clamp it.'"""
    original = _valid_set()
    snapshot = list(original)
    validate_intervals(original)
    assert original == snapshot


# ══════════════════════════════════════════════════════════════════════════════
# 3. Integer seconds and deterministic ordering
# ══════════════════════════════════════════════════════════════════════════════

def test_duration_is_integer_seconds():
    d = duration_seconds(_at(9), _at(17, 30))
    assert isinstance(d, int)
    assert d == 8 * 3600 + 1800


def test_duration_rejects_naive_instants():
    with pytest.raises(ValueError):
        duration_seconds(datetime(2026, 9, 4, 9), _at(17))


def test_duration_uses_true_utc_instants_across_a_dst_boundary():
    """§16.4: real elapsed time, never a wall-clock subtraction. On the
    Europe/London spring-forward, 01:00 to 03:00 local is one hour."""
    start = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
    end = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert duration_seconds(start, end) == 3600


def test_ordering_is_deterministic_regardless_of_input_order():
    ivs = _valid_set()
    assert [i.seq for i in order_intervals(list(reversed(ivs)))] == [1, 2, 3]
    assert [i.seq for i in order_intervals(ivs)] == [1, 2, 3]


def test_ordering_uses_seq_as_tiebreaker_on_identical_start():
    """INV-8: order must never depend on iteration order."""
    a = _iv(2, WorkState.WRK.value, _at(9), _at(10))
    b = _iv(1, WorkState.BRK_R.value, _at(9), _at(9, 15))
    assert [i.seq for i in order_intervals([a, b])] == [1, 2]


# ══════════════════════════════════════════════════════════════════════════════
# 4. Sealing
# ══════════════════════════════════════════════════════════════════════════════

def test_seal_writes_version_one_with_ordered_intervals(db):
    key = _day(org=101, user=201)
    row = seal_day(db, key, intervals=_valid_set(),
                   timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert row.ledger_version == 1
    assert row.state == LedgerState.SEALED.value
    assert row.sealed_at is not None
    assert row.timezone_iana == TZ
    assert [i.seq for i in read_intervals(db, row)] == [1, 2, 3]


def test_sealed_intervals_store_integer_duration(db):
    key = _day(org=102, user=202)
    row = seal_day(db, key, intervals=_valid_set(),
                   timezone_iana=TZ, is_working_day=True, working_day_source="org")
    ivs = read_intervals(db, row)
    assert ivs[0].duration_seconds == 4 * 3600
    assert ivs[1].duration_seconds == 45 * 60
    assert all(isinstance(i.duration_seconds, int) for i in ivs)


def test_every_persisted_interval_carries_provenance(db):
    key = _day(org=103, user=203)
    row = seal_day(db, key, intervals=_valid_set(),
                   timezone_iana=TZ, is_working_day=True, working_day_source="org")
    for iv in read_intervals(db, row):
        assert iv.provenance, "no interval may exist without provenance"


def test_sealing_inadmissible_evidence_raises_and_writes_nothing(db):
    key = _day(org=104, user=204)
    bad = [
        _iv(1, WorkState.WRK.value, _at(9), _at(13)),
        _iv(2, WorkState.WRK.value, _at(12), _at(14)),
    ]
    with pytest.raises(LedgerIntegrityFault):
        seal_day(db, key, intervals=bad, timezone_iana=TZ, is_working_day=True, working_day_source="org")
    db.rollback()
    assert get_current_ledger(db, key) is None


def test_integrity_fault_day_is_recorded_and_not_evaluated(db):
    """§24.15: refuse to evaluate, record the fault, raise an incident."""
    key = _day(org=105, user=205)
    row = record_integrity_fault(db, key, reason="overlapping sealed intervals",
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert row.state == LedgerState.LEDGER_INTEGRITY_FAULT.value
    assert "overlapping" in row.integrity_fault_reason
    assert read_intervals(db, row) == []


# ══════════════════════════════════════════════════════════════════════════════
# 5. Idempotency (§22.1)
# ══════════════════════════════════════════════════════════════════════════════

def test_replaying_an_identical_seal_is_a_no_op(db):
    key = _day(org=106, user=206)
    first = seal_day(db, key, intervals=_valid_set(),
                     timezone_iana=TZ, is_working_day=True, working_day_source="org")
    second = seal_day(db, key, intervals=_valid_set(),
                      timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert first.id == second.id
    assert db.query(WorkDayLedger).filter_by(
        organization_id=106, user_id=206).count() == 1


def test_replay_is_order_independent(db):
    key = _day(org=107, user=207)
    first = seal_day(db, key, intervals=_valid_set(),
                     timezone_iana=TZ, is_working_day=True, working_day_source="org")
    again = seal_day(db, key, intervals=list(reversed(_valid_set())),
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert first.id == again.id


def test_reseal_with_different_evidence_is_refused_not_overwritten(db):
    """An evidentiary store that silently accepts a rewrite is not evidence."""
    key = _day(org=108, user=208)
    seal_day(db, key, intervals=_valid_set(),
             timezone_iana=TZ, is_working_day=True, working_day_source="org")
    changed = _valid_set()[:2]
    with pytest.raises(LedgerEvidenceConflict):
        seal_day(db, key, intervals=changed,
                 timezone_iana=TZ, is_working_day=True, working_day_source="org")


def test_natural_key_uniqueness_is_enforced_by_the_database(db):
    key = _day(org=109, user=209)
    seal_day(db, key, intervals=_valid_set(),
             timezone_iana=TZ, is_working_day=True, working_day_source="org")
    db.add(WorkDayLedger(
        organization_id=109, user_id=209,
        work_date=datetime(2026, 9, 4), workweek_key="2026-W36",
        ledger_version=1, state=LedgerState.SEALED.value, timezone_iana=TZ, is_working_day=True, working_day_source="org",
    ))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Supersession and optimistic concurrency (INV-3, §22.2, §23)
# ══════════════════════════════════════════════════════════════════════════════

def test_correction_supersedes_and_never_mutates(db):
    key = _day(org=110, user=210)
    v1 = seal_day(db, key, intervals=_valid_set(),
                  timezone_iana=TZ, is_working_day=True, working_day_source="org")
    v1_id = v1.id

    corrected = _valid_set()
    corrected[1] = _iv(2, WorkState.BRK_M.value, _at(13), _at(13, 35),
                       prov=Provenance.ADMIN_OVERRIDE.value)
    v2 = supersede_day(db, key, expected_version=1, intervals=corrected,
 timezone_iana=TZ, is_working_day=True, working_day_source="org")

    assert v2.ledger_version == 2
    assert v2.supersedes == v1_id
    assert get_current_ledger(db, key).id == v2.id

    # The prior version stays permanently readable, with its original evidence.
    old = get_ledger_version(db, key, 1)
    assert old is not None and old.superseded_by == v2.id
    assert read_intervals(db, old)[1].duration_seconds == 45 * 60
    assert read_intervals(db, v2)[1].duration_seconds == 35 * 60


def test_stale_version_write_is_rejected_and_nothing_is_merged(db):
    key = _day(org=111, user=211)
    seal_day(db, key, intervals=_valid_set(),
             timezone_iana=TZ, is_working_day=True, working_day_source="org")
    supersede_day(db, key, expected_version=1, intervals=_valid_set()[:2],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    with pytest.raises(StaleLedgerVersion):
        supersede_day(db, key, expected_version=1, intervals=_valid_set(),
 timezone_iana=TZ, is_working_day=True, working_day_source="org")


def test_superseding_an_unsealed_day_is_rejected(db):
    with pytest.raises(StaleLedgerVersion):
        supersede_day(db, _day(org=112, user=212), expected_version=1,
                      intervals=_valid_set(),
                      timezone_iana=TZ, is_working_day=True, working_day_source="org")


def test_versions_increment_monotonically(db):
    key = _day(org=113, user=213)
    seal_day(db, key, intervals=_valid_set(),
             timezone_iana=TZ, is_working_day=True, working_day_source="org")
    for expected in (1, 2, 3):
        row = supersede_day(db, key, expected_version=expected,
                            intervals=_valid_set()[:2],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
        assert row.ledger_version == expected + 1


# ══════════════════════════════════════════════════════════════════════════════
# 7. Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════

def test_ledger_lookup_is_organization_scoped(db):
    seal_day(db, _day(org=120, user=300), intervals=_valid_set(),
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    seal_day(db, _day(org=121, user=300), intervals=_valid_set()[:2],
 timezone_iana=TZ, is_working_day=True, working_day_source="org")

    a = get_current_ledger(db, _day(org=120, user=300))
    b = get_current_ledger(db, _day(org=121, user=300))
    assert a is not None and b is not None and a.id != b.id
    assert len(read_intervals(db, a)) == 3
    assert len(read_intervals(db, b)) == 2


def test_another_organizations_day_is_never_returned(db):
    seal_day(db, _day(org=122, user=301), intervals=_valid_set(),
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert get_current_ledger(db, _day(org=999, user=301)) is None


def test_every_interval_carries_the_organization_id(db):
    row = seal_day(db, _day(org=123, user=302), intervals=_valid_set(),
 timezone_iana=TZ, is_working_day=True, working_day_source="org")
    assert all(i.organization_id == 123 for i in read_intervals(db, row))


# ══════════════════════════════════════════════════════════════════════════════
# 8. Architectural guards — the constraints that make this Step 1 and not Step 9
# ══════════════════════════════════════════════════════════════════════════════

def _code_identifiers(mod) -> set:
    """Every identifier the module's CODE actually references.

    AST-based rather than a text scan: these modules document what they must
    never import, so a raw ``in source`` check flags the prohibition notice
    itself. Docstrings and comments are prose about the rule; imports, names
    and attributes are the rule being kept or broken.
    """
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


def test_ledger_package_contains_no_worked_time_calculation():
    """Step 1 delivers the primitive, not the fold. A worked-time calculation
    here would be a second source of truth (INV-2, §7)."""
    from app.work_ledger import service, integrity, models, states
    for mod in (service, integrity, models, states):
        ids = _code_identifiers(mod)
        for forbidden in ("session_worked_seconds", "_worked",
                          "worked_seconds", "qualifying_minutes"):
            assert forbidden not in ids, f"{mod.__name__} references {forbidden!r}"


def test_ledger_package_has_no_activity_or_screenshot_dependency():
    """INV-6 / INV-7 enforced by construction, not by review."""
    from app.work_ledger import service, integrity, models, states
    for mod in (service, integrity, models, states):
        ids = _code_identifiers(mod)
        for forbidden in ("active_time", "Activity", "ActivityWindow",
                          "BrowserActivity", "AppSession", "Screenshot"):
            assert forbidden not in ids, f"{mod.__name__} references {forbidden!r}"


def test_ledger_does_not_read_existing_session_tables():
    """Wiring is Step 9/10. Step 1 must not touch clock-out behaviour."""
    from app.work_ledger import service
    ids = _code_identifiers(service)
    for forbidden in ("ClockSession", "BreakSession", "OfflineInterval"):
        assert forbidden not in ids


def test_the_import_guard_actually_detects_a_violation():
    """Guards the guard: an AST check that silently matched nothing would pass
    forever. Pin that it flags real references in modules that have them.

    `payroll/router.py:48` imports `session_worked_seconds` directly;
    `employees/router.py` references `ClockSession` throughout. (Note the
    employees router reaches worked time via the `_session_worked_secs`
    wrapper, not the canonical name — which is itself part of why the
    consumer inventory took five sweeps to complete.)
    """
    from app.payroll import router as payroll_router
    from app.employees import router as employees_router

    assert "session_worked_seconds" in _code_identifiers(payroll_router)
    assert "ClockSession" in _code_identifiers(employees_router)


def test_service_never_repairs_evidence():
    """Guards the §24.15 contract against a future 'helpful' edit."""
    from app.work_ledger import integrity
    src = inspect.getsource(integrity)
    for forbidden in ("def merge", "def clamp", "def repair", ".union("):
        assert forbidden not in src


def test_existing_clock_out_behaviour_is_untouched_by_step_1():
    """The v1.0 gate stays disabled and `_worked` keeps its current definition
    until Step 9 re-expresses it as a fold."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parent.parent
    router = (backend / "app" / "employees" / "router.py").read_text(
        encoding="utf-8", errors="replace")
    assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router
    worked = (backend / "app" / "_worked.py").read_text(encoding="utf-8")
    assert "def session_worked_seconds" in worked
    assert "work_ledger" not in worked, "Step 9 has not happened yet"
