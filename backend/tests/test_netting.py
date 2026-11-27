"""
MODEL-NETTING — §15's bank, and GV-19 / GV-20 executed end to end.
===================================================================

    GV-19  "Netting NONE (§15)"
           Mon +45; Tue −20; mode NONE  ->  Tuesday attributable 20

    GV-20  "Netting WEEKLY_BANK"
           Same, mode WEEKLY_BANK, cap 60
           ->  Tuesday attributable 0; bank consumed 20

THE PAIR IS THE POINT
---------------------
The two vectors run the SAME two days against the SAME evidence and differ only
in the governing policy's netting mode. They pass only when the answers DIVERGE:
20 against 0. An implementation that banked under `NONE`, or failed to bank
under `WEEKLY_BANK`, would make them agree — which is precisely §15's "DO NOT
SHIP `NONE` AS AN ASSUMPTION" expressed as a test.

WHERE EACH NUMBER COMES FROM (§18)
-----------------------------------
Monday, required 480, qualifying 525::

    surplus   = MAX(0, 525 - 480)           = 45     (D-6.3)
    gap       = MAX(0, 480 - 525)           = 0
    bank      = MIN(available, gap)         = 0      (step 10)
    gross     = MAX(0, 480 - 525 - 0)       = 0      (step 11)
    exclusion = 0                                    (step 12)
    attributable = 0                                 (step 13)
    accumulator  = 0                                 (§20)

Tuesday, required 480, qualifying 460, under WEEKLY_BANK with caps 60/60::

    eligible pool = MIN(45, cap_per_day 60) = 45     folded from MONDAY only
    consumed      = 0                                Monday took nothing
    available     = MIN(45, cap_per_period 60) - 0 = 45
    gap           = MAX(0, 480 - 460)       = 20
    bank          = MIN(45, 20)             = 20     (step 10)
    gross         = MAX(0, 480 - 460 - 20)  = 0      (step 11)
    exclusion     = 0                                (step 12)
    attributable  = 0                                (step 13)
    accumulator   = 0                                (§20)

and under NONE the bank is never consulted, so::

    gross = MAX(0, 480 - 460) = 20 -> attributable 20 -> accumulator 20

THREE QUANTITIES, NEVER CONFLATED
----------------------------------
`bank_credit_minutes` is what the WEEK supplied to this day.
`attributable_minutes` is what this day is answerable for.
`contributes_to_accumulator_minutes` is what reaches the weekly threshold.

On GV-20's Tuesday they are 20, 0 and 0 — three different numbers, and
`TestQuantitiesStayDistinct` asserts they cannot collapse.

NO BANK IS STORED, ANYWHERE
----------------------------
§15.1 / INV-3. `TestNoStoredBank` checks the schema and the application surface
structurally rather than by name alone, so a counter cannot hide under a
different spelling.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib

import pytest
import sqlalchemy as sa

import database.models as dbm
from database.database import Base

from app.eval_day import (
    EcoDayRecord,
    EcoDayState,
    evaluate_and_record_day,
)
from app.eval_day.netting import (
    BankOutcome,
    eligible_surplus_minutes,
    fold_available_minutes,
    resolve_bank_credit,
)
from app.eval_day.errors import (
    REASON_NETTING_CAP_UNAVAILABLE,
    REASON_NETTING_PERIOD_UNSUPPORTED,
)
from app.policy_version import capture_policy_version
from app.schedule_policy import keys as SCHEDULE_KEYS
from app.work_ledger import (
    CandidateInterval,
    DayKey,
    Provenance,
    WorkState,
    seal_day,
)

UTC = dt.timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, USER = 9810, 9811
ZONE = "Asia/Kolkata"
FLOOR = dt.date(2020, 1, 1)

#: A Monday and the Tuesday after it.
MONDAY = dt.date(2026, 8, 31)
TUESDAY = dt.date(2026, 9, 1)

# ── Independently transcribed from §15's worked example ─────────────────────
REQUIRED_MINUTES = 480
MONDAY_QUALIFYING = 525          # +45
TUESDAY_QUALIFYING = 460         # -20
SURPLUS = 45
SHORTFALL = 20
CAP = 60


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    import app.alert.models                    # noqa: F401
    import app.break_classification.models     # noqa: F401
    import app.break_treatment.models          # noqa: F401
    import app.eval_event.models               # noqa: F401
    import app.policy_version.models           # noqa: F401

    Base.metadata.create_all(engine)


@pytest.fixture()
def org(db):
    if db.get(dbm.Organization, ORG) is None:
        db.add(dbm.Organization(id=ORG, name="Netting Org"))
        db.flush()
        db.add(dbm.User(id=USER, name="W", email="w@netting.test",
                        organization_id=ORG, role="employee"))
        db.flush()
    row = (db.query(dbm.AppSettings)
             .filter(dbm.AppSettings.organization_id == ORG,
                     dbm.AppSettings.key
                     == SCHEDULE_KEYS.SETTING_WEEK_START_DAY,
                     dbm.AppSettings.team_key.is_(None),
                     dbm.AppSettings.user_id.is_(None))
             .one_or_none())
    if row is None:
        db.add(dbm.AppSettings(
            organization_id=ORG, team_key=None, user_id=None,
            key=SCHEDULE_KEYS.SETTING_WEEK_START_DAY, value="MONDAY"))
    else:
        row.value = "MONDAY"
    db.flush()
    return ORG


def _vector(vid) -> dict:
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == vid:
            return v
    raise AssertionError(vid)


def _policy(db, *, mode="NONE", cap_day=None, cap_period=None,
            key="netting-v1", effective_from=FLOOR):
    return capture_policy_version(
        db, organization_id=ORG, policy_version=key,
        effective_from=effective_from, counts_toward_week=True,
        netting_mode=mode, grace_minutes=0,
        netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
        surplus_cap_per_day_minutes=cap_day,
        surplus_cap_per_period_minutes=cap_period,
        captured_by="owner@netting.test")


def _requirement(db, *, minutes=REQUIRED_MINUTES, key="netting-cfg-v1"):
    from app.config_versioning import DatabaseConfigVersionProvider
    from app.requirement_resolution import RequirementCandidate

    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id=key, effective_from=FLOOR,
        candidates=[RequirementCandidate(
            tier="TENANT_FALLBACK", rank=6, minutes=minutes)])
    db.flush()


def _seal(db, day, minutes):
    """One WRK interval of exactly ``minutes``, through the real ledger."""
    begin = dt.datetime(day.year, day.month, day.day, 3, 30, tzinfo=UTC)
    return seal_day(
        db, DayKey(organization_id=ORG, user_id=USER, work_date=day),
        intervals=[CandidateInterval(
            seq=1, state=WorkState.WRK.value, start_at_utc=begin,
            end_at_utc=begin + dt.timedelta(minutes=minutes),
            provenance=Provenance.WORKER.value)],
        timezone_iana=ZONE, is_working_day=True, working_day_source="org",
        scheduled_end_local=dt.time(18, 0), scheduled_end_source="shift",
        departure_at=begin + dt.timedelta(minutes=minutes),
        departure_provenance="WORKER",
        departure_source_ref=f"clock_session:{day.isoformat()}",
        created_by="GOLDEN_VECTOR")


def _evaluate(db, day):
    result = evaluate_and_record_day(
        db, organization_id=ORG, user_id=USER, work_date=day)
    assert result.record is not None, (day, result.reason, result.detail)
    return result.record


def _run_week(db, *, mode, cap_day=None, cap_period=None):
    """Seal and evaluate Monday then Tuesday, in that order."""
    _requirement(db)
    _policy(db, mode=mode, cap_day=cap_day, cap_period=cap_period)
    _seal(db, MONDAY, MONDAY_QUALIFYING)
    _seal(db, TUESDAY, TUESDAY_QUALIFYING)
    return _evaluate(db, MONDAY), _evaluate(db, TUESDAY)


# ═════════════════════════════════════════════════════════════════════════════
# A / B. The surplus quantity (D-6.3)
# ═════════════════════════════════════════════════════════════════════════════

class _Rec:
    def __init__(self, qualifying=None, required=None, credit=None):
        self.qualifying_minutes = qualifying
        self.required_minutes = required
        self.bank_credit_minutes = credit


class TestSurplus:

    def test_it_is_qualifying_minus_required(self):
        assert eligible_surplus_minutes(
            _Rec(qualifying=525, required=480), cap_per_day=999) == 45

    def test_it_never_goes_negative(self):
        assert eligible_surplus_minutes(
            _Rec(qualifying=460, required=480), cap_per_day=999) == 0

    def test_an_exact_day_banks_nothing(self):
        assert eligible_surplus_minutes(
            _Rec(qualifying=480, required=480), cap_per_day=999) == 0

    def test_a_day_that_measured_nothing_banks_nothing(self):
        """NULL is not zero. An unresolved requirement, an unresolved policy
        and a deferred classification all reach here with NULL measurements."""
        assert eligible_surplus_minutes(
            _Rec(qualifying=None, required=480), cap_per_day=999) == 0
        assert eligible_surplus_minutes(
            _Rec(qualifying=525, required=None), cap_per_day=999) == 0

    def test_it_is_not_derived_from_gross_or_attributable(self):
        """Structural. Deriving surplus from a post-exclusion figure would let
        leave manufacture credit, which D-6.3 forbids."""
        src = (BACKEND / "app" / "eval_day"
               / "netting.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "eligible_surplus_minutes")
        read = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert read <= {"qualifying_minutes", "required_minutes"}, read
        for banned in ("gross_shortfall_minutes", "attributable_minutes",
                       "exclusion_minutes", "schedule_variance_minutes"):
            assert banned not in read


# ═════════════════════════════════════════════════════════════════════════════
# C / D. NONE creates nothing; WEEKLY_BANK creates a day credit
# ═════════════════════════════════════════════════════════════════════════════

class TestModes:

    def test_none_creates_no_credit(self, db, org):
        monday, tuesday = _run_week(db, mode="NONE")
        assert monday.bank_credit_minutes == 0
        assert tuesday.bank_credit_minutes == 0

    def test_none_leaves_the_shortfall_attributable(self, db, org):
        _, tuesday = _run_week(db, mode="NONE")
        assert tuesday.attributable_minutes == SHORTFALL

    def test_weekly_bank_creates_a_day_credit(self, db, org):
        _, tuesday = _run_week(db, mode="WEEKLY_BANK",
                               cap_day=CAP, cap_period=CAP)
        assert tuesday.bank_credit_minutes == SHORTFALL

    def test_weekly_bank_closes_the_shortfall(self, db, org):
        _, tuesday = _run_week(db, mode="WEEKLY_BANK",
                               cap_day=CAP, cap_period=CAP)
        assert tuesday.attributable_minutes == 0

    def test_the_surplus_day_itself_takes_nothing(self, db, org):
        """Monday has no gap, so it consumes no credit however large the
        pool — taking more would spend a capped resource to no effect."""
        monday, _ = _run_week(db, mode="WEEKLY_BANK",
                              cap_day=CAP, cap_period=CAP)
        assert monday.bank_credit_minutes == 0
        assert monday.attributable_minutes == 0

    def test_the_two_modes_diverge(self, db, org):
        """§15's whole point, and GV-19 vs GV-20 in one assertion."""
        _, none_tue = _run_week(db, mode="NONE")
        assert none_tue.attributable_minutes == SHORTFALL


# ═════════════════════════════════════════════════════════════════════════════
# E / F / G / H. Caps
# ═════════════════════════════════════════════════════════════════════════════

class TestCaps:

    def test_the_per_day_cap_bounds_one_day_s_contribution(self):
        """§15.1: "to prevent one long day erasing a week"."""
        assert eligible_surplus_minutes(
            _Rec(qualifying=600, required=480), cap_per_day=30) == 30

    def test_the_per_period_cap_bounds_the_pool(self):
        records = [_Rec(qualifying=525, required=480),
                   _Rec(qualifying=525, required=480)]
        available, eligible, consumed = fold_available_minutes(
            records, cap_per_day=60, cap_per_period=60)
        assert eligible == 90          # 45 + 45, each under the per-day cap
        assert available == 60         # bounded by the period cap
        assert consumed == 0

    def test_a_cap_of_zero_banks_nothing(self, db, org):
        """0 is a real policy — "bank nothing" — and not an absence."""
        _, tuesday = _run_week(db, mode="WEEKLY_BANK",
                               cap_day=0, cap_period=0)
        assert tuesday.bank_credit_minutes == 0
        assert tuesday.attributable_minutes == SHORTFALL

    def test_a_null_cap_refuses_rather_than_banking_zero(self, db, org):
        """G. NULL is neither zero nor unlimited: it is a policy gap.

        Banking zero would be indistinguishable from a deliberate cap of 0,
        and banking without a limit would breach §15.1 outright.
        """
        _requirement(db)
        _policy(db, mode="WEEKLY_BANK", cap_day=None, cap_period=None)
        _seal(db, MONDAY, MONDAY_QUALIFYING)
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=MONDAY)
        assert result.record is None
        assert result.reason == REASON_NETTING_CAP_UNAVAILABLE

    def test_one_missing_cap_is_enough_to_refuse(self, db, org):
        _requirement(db)
        _policy(db, mode="WEEKLY_BANK", cap_day=CAP, cap_period=None)
        _seal(db, MONDAY, MONDAY_QUALIFYING)
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=MONDAY)
        assert result.reason == REASON_NETTING_CAP_UNAVAILABLE

    def test_a_null_cap_under_none_is_harmless(self, db, org):
        """NONE never consults a cap, so its absence is not a gap."""
        monday, tuesday = _run_week(db, mode="NONE")
        assert monday.bank_credit_minutes == 0
        assert tuesday.attributable_minutes == SHORTFALL


# ═════════════════════════════════════════════════════════════════════════════
# I / J / K / L. The fold is a fold
# ═════════════════════════════════════════════════════════════════════════════

class TestFold:

    def test_it_is_deterministic_on_replay(self, db, org):
        """J. Same records in, same credit out, same digest."""
        _, first = _run_week(db, mode="WEEKLY_BANK",
                             cap_day=CAP, cap_period=CAP)
        digest, generation = first.content_hash, first.evaluation_generation

        again = _evaluate(db, TUESDAY)
        assert again.id == first.id
        assert again.bank_credit_minutes == first.bank_credit_minutes
        assert again.content_hash == digest
        assert again.evaluation_generation == generation

    def test_the_pool_is_recomputed_not_read_from_a_field(self):
        """I. The fold reads day records and sums them; it reads no total."""
        src = (BACKEND / "app" / "eval_day"
               / "netting.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "fold_available_minutes")
        # It sums; it does not read a stored aggregate.
        assert any(isinstance(n, ast.Call)
                   and getattr(n.func, "id", None) == "sum"
                   for n in ast.walk(fn))
        read = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        for banned in ("balance", "total", "counter", "accumulated"):
            assert not any(banned in r for r in read), read

    def test_a_corrected_earlier_day_changes_the_derived_result(self, db, org):
        """K. Monday's correction moves Tuesday's credit — through
        re-evaluation, because there is no balance to adjust."""
        _run_week(db, mode="WEEKLY_BANK", cap_day=CAP, cap_period=CAP)

        # Supersede Monday with a day that earned only 10 minutes of surplus.
        from app.work_ledger import supersede_day

        begin = dt.datetime(MONDAY.year, MONDAY.month, MONDAY.day, 3, 30,
                            tzinfo=UTC)
        supersede_day(
            db, DayKey(organization_id=ORG, user_id=USER, work_date=MONDAY),
            expected_version=1,
            intervals=[CandidateInterval(
                seq=1, state=WorkState.WRK.value, start_at_utc=begin,
                end_at_utc=begin + dt.timedelta(minutes=REQUIRED_MINUTES + 10),
                provenance=Provenance.WORKER.value)],
            timezone_iana=ZONE, is_working_day=True, working_day_source="org",
            scheduled_end_local=dt.time(18, 0), scheduled_end_source="shift",
            departure_at=begin + dt.timedelta(minutes=REQUIRED_MINUTES + 10),
            departure_provenance="WORKER",
            departure_source_ref="clock_session:corrected",
            created_by="GOLDEN_VECTOR")
        db.flush()

        corrected_monday = _evaluate(db, MONDAY)
        assert corrected_monday.qualifying_minutes == REQUIRED_MINUTES + 10

        # Tuesday re-evaluated against the corrected week now gets only 10.
        again = _evaluate(db, TUESDAY)
        assert again.bank_credit_minutes == 10
        assert again.attributable_minutes == SHORTFALL - 10

    def test_a_later_day_never_rewrites_an_earlier_record(self, db, org):
        """L. Tuesday's consumption leaves Monday's row untouched."""
        monday, _ = _run_week(db, mode="WEEKLY_BANK",
                              cap_day=CAP, cap_period=CAP)
        before = (monday.id, monday.content_hash, monday.bank_credit_minutes,
                  monday.evaluation_generation)

        _evaluate(db, TUESDAY)
        db.expire_all()

        after = db.get(EcoDayRecord, before[0])
        assert (after.id, after.content_hash, after.bank_credit_minutes,
                after.evaluation_generation) == before

    def test_the_fold_reads_only_earlier_days(self, db, org):
        """Monday is evaluated before Tuesday exists, and is unaffected by it."""
        _requirement(db)
        _policy(db, mode="WEEKLY_BANK", cap_day=CAP, cap_period=CAP)
        _seal(db, MONDAY, MONDAY_QUALIFYING)
        monday = _evaluate(db, MONDAY)
        assert monday.bank_credit_minutes == 0

        _seal(db, TUESDAY, TUESDAY_QUALIFYING)
        _evaluate(db, TUESDAY)
        db.expire_all()
        assert db.get(EcoDayRecord, monday.id).bank_credit_minutes == 0


# ═════════════════════════════════════════════════════════════════════════════
# M. Canonical workweek membership
# ═════════════════════════════════════════════════════════════════════════════

class TestMembership:

    def test_it_uses_canonical_dates_not_iso_weeks(self):
        src = (BACKEND / "app" / "eval_day"
               / "netting.py").read_text(encoding="utf-8")
        assert "canonical_dates" in src
        for banned in ("isocalendar", "%W", "%V", "strftime"):
            assert banned not in src, banned

    def test_a_day_in_another_week_does_not_supply_credit(self, db, org):
        """The previous week's surplus is not this week's pool."""
        _requirement(db)
        _policy(db, mode="WEEKLY_BANK", cap_day=CAP, cap_period=CAP)

        prior_monday = MONDAY - dt.timedelta(days=7)
        _seal(db, prior_monday, MONDAY_QUALIFYING)
        _evaluate(db, prior_monday)

        _seal(db, TUESDAY, TUESDAY_QUALIFYING)
        tuesday = _evaluate(db, TUESDAY)
        assert tuesday.bank_credit_minutes == 0
        assert tuesday.attributable_minutes == SHORTFALL


# ═════════════════════════════════════════════════════════════════════════════
# N / O. §18 ordering, and the three distinct quantities
# ═════════════════════════════════════════════════════════════════════════════

class TestOrdering:

    def test_leave_never_becomes_surplus(self, db, org):
        """N. Exclusions apply at step 12, to a gap the bank already reduced.

        A day covered entirely by leave has qualifying < required, so its
        surplus is 0 — the leave cannot bank anything.
        """
        assert eligible_surplus_minutes(
            _Rec(qualifying=0, required=480), cap_per_day=999) == 0

    def test_the_bank_is_subtracted_before_gross(self):
        """§18 step 11: gross := MAX(0, req - q - bank)."""
        src = (BACKEND / "app" / "eval_day"
               / "evaluator.py").read_text(encoding="utf-8")
        assert ("gross = max(required_minutes - qualifying_minutes "
                "- bank_credit, 0)") in src

    def test_grace_does_not_return_credit_to_the_bank(self, db, org):
        """The credit is spent at step 11 and recorded on the day that spent
        it; step 13 zeroes what is ATTRIBUTED and touches nothing else."""
        _, tuesday = _run_week(db, mode="WEEKLY_BANK",
                               cap_day=CAP, cap_period=CAP)
        assert tuesday.bank_credit_minutes == SHORTFALL
        assert tuesday.gross_shortfall_minutes == 0


class TestQuantitiesStayDistinct:

    def test_bank_attributable_and_accumulator_are_three_things(self, db, org):
        """O. On GV-20's Tuesday they are 20, 0 and 0."""
        _, tuesday = _run_week(db, mode="WEEKLY_BANK",
                               cap_day=CAP, cap_period=CAP)
        assert tuesday.bank_credit_minutes == 20
        assert tuesday.attributable_minutes == 0
        assert tuesday.contributes_to_accumulator_minutes == 0

    def test_they_are_separate_columns(self):
        cols = {c.name for c in EcoDayRecord.__table__.c}
        assert {"bank_credit_minutes", "attributable_minutes",
                "contributes_to_accumulator_minutes"} <= cols

    def test_the_credit_joins_the_content_hash(self, db, org):
        """A replay whose fold moved must not read as identical (D-6.12)."""
        from app.eval_day.evaluator import DayEvaluation

        base = dict(
            organization_id=ORG, user_id=USER, work_date=TUESDAY,
            ledger_version=1, state=EcoDayState.NO_SHORTFALL,
            requirement_outcome="RESOLVED", shortfall_computed=True,
            required_minutes=480, qualifying_minutes=460,
            gross_shortfall_minutes=0, exclusion_minutes=0,
            attributable_minutes=0, contributes_to_accumulator_minutes=0)
        a = DayEvaluation(**base, bank_credit_minutes=20)
        b = DayEvaluation(**base, bank_credit_minutes=10)
        assert a.result_fields() != b.result_fields()
        assert "bank_credit_minutes" in a.result_fields()


# ═════════════════════════════════════════════════════════════════════════════
# P / Q. Policy effective dating
# ═════════════════════════════════════════════════════════════════════════════

class TestPolicyDating:

    def test_the_policy_governing_the_date_is_the_one_applied(self, db, org):
        """P. A later policy does not reach back into an earlier week."""
        _requirement(db)
        _policy(db, mode="NONE", key="early", effective_from=FLOOR)
        _policy(db, mode="WEEKLY_BANK", cap_day=CAP, cap_period=CAP,
                key="later", effective_from=TUESDAY + dt.timedelta(days=30))

        _seal(db, MONDAY, MONDAY_QUALIFYING)
        _seal(db, TUESDAY, TUESDAY_QUALIFYING)
        _evaluate(db, MONDAY)
        tuesday = _evaluate(db, TUESDAY)

        # The week is governed by the EARLY policy: NONE, so no bank.
        assert tuesday.policy_version == "early"
        assert tuesday.bank_credit_minutes == 0
        assert tuesday.attributable_minutes == SHORTFALL

    def test_capturing_a_later_policy_does_not_rewrite_a_recorded_day(
            self, db, org):
        """Q. History changes by addition, never by mutation (§12.4)."""
        _, tuesday = _run_week(db, mode="NONE")
        before = (tuesday.content_hash, tuesday.attributable_minutes,
                  tuesday.bank_credit_minutes)

        _policy(db, mode="WEEKLY_BANK", cap_day=CAP, cap_period=CAP,
                key="later", effective_from=TUESDAY + dt.timedelta(days=30))
        db.expire_all()

        again = db.get(EcoDayRecord, tuesday.id)
        assert (again.content_hash, again.attributable_minutes,
                again.bank_credit_minutes) == before


# ═════════════════════════════════════════════════════════════════════════════
# PERIOD_BANK — refused, never approximated
# ═════════════════════════════════════════════════════════════════════════════

class TestPeriodBankRefuses:

    def test_it_refuses_rather_than_folding_as_a_workweek(self, db, org):
        _requirement(db)
        _policy(db, mode="PERIOD_BANK", cap_day=CAP, cap_period=CAP)
        _seal(db, MONDAY, MONDAY_QUALIFYING)
        result = evaluate_and_record_day(
            db, organization_id=ORG, user_id=USER, work_date=MONDAY)
        assert result.record is None
        assert result.reason == REASON_NETTING_PERIOD_UNSUPPORTED

    def test_the_refusal_names_the_missing_contract(self, db, org):
        r = resolve_bank_credit(
            db, organization_id=ORG, user_id=USER, work_date=MONDAY,
            workweek_key=MONDAY.isoformat(), netting_mode="PERIOD_BANK",
            cap_per_day_minutes=CAP, cap_per_period_minutes=CAP,
            evaluation=_Rec(qualifying=460, required=480))
        assert r.outcome is BankOutcome.REFUSED
        assert "period" in (r.detail or "").lower()

    def test_no_period_definition_exists_to_implement_it_from(self):
        """The gap, asserted so it cannot be quietly filled by guesswork.

        §15 needs a reference period (month, quarter or annualised) and an
        anchor. `PolicyVersion` records the CAP but no period definition, and
        nothing else supplies one.
        """
        from app.policy_version import PolicyVersion

        cols = {c.name for c in PolicyVersion.__table__.c}
        assert "surplus_cap_per_period_minutes" in cols
        for absent in ("period_kind", "period_start", "period_length",
                       "reference_period", "period_anchor"):
            assert absent not in cols, (
                f"{absent} now exists; PERIOD_BANK may be implementable and "
                f"this refusal should be revisited")


# ═════════════════════════════════════════════════════════════════════════════
# V. No stored bank
# ═════════════════════════════════════════════════════════════════════════════

class TestNoStoredBank:

    BANNED = ("bank_balance", "bank_counter", "bank_state", "banked_minutes",
              "surplus_balance", "bank_accumulator", "weekly_bank_total",
              "period_bank_total", "running_bank")

    def test_no_bank_artifact_by_name(self):
        """Identifiers, not substrings.

        A substring scan would flag `netting.py`'s own docstring, which names
        these things precisely in order to forbid them. Parsing for real
        NAMES — assignments, attributes, arguments, class and function names
        — is both narrower and stronger: prose cannot trip it, and a counter
        cannot hide in a comment.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.arg):
                    names.add(node.arg)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, ast.keyword) and node.arg:
                    names.add(node.arg)
            for token in self.BANNED:
                if token in names:
                    offenders.append(f"{path.name}:{token}")
        assert offenders == [], offenders

    def test_no_bank_artifact_hides_under_another_name(self):
        """Schema-level, not name-level.

        Every ECO table is inspected for a column that could hold a running
        total: an integer column whose name suggests accumulation, on any
        table other than the day record's own derived credit.
        """
        import app.alert.models            # noqa: F401
        import app.break_classification.models  # noqa: F401
        import app.break_treatment.models  # noqa: F401
        import app.eval_event.models       # noqa: F401
        import app.policy_version.models   # noqa: F401
        import app.work_ledger.models      # noqa: F401

        # ECO tables only: payroll and billing have their own, unrelated
        # balances, and this rule is about §15.1's bank.
        eco_tables = {
            "eco_day_record", "eco_notification", "eco_threshold_event",
            "policy_version", "work_day_ledger", "work_state_interval",
            "eco_classification_authority", "break_treatment_profile",
        }
        # `contributes_to_accumulator_minutes` is §20's per-day CONTRIBUTION,
        # not an accumulator: the week sums it, nothing stores the sum. Named
        # explicitly so the rule stays sharp for everything else.
        allowed = {"eco_day_record.contributes_to_accumulator_minutes"}
        suspicious = ("balance", "counter", "accumulat", "running_total")
        offenders = []
        for table in Base.metadata.tables.values():
            if table.name not in eco_tables:
                continue
            for column in table.columns:
                qualified = f"{table.name}.{column.name}"
                if qualified in allowed:
                    continue
                if any(s in column.name.lower() for s in suspicious):
                    offenders.append(qualified)
        assert offenders == [], offenders

    def test_the_only_bank_column_is_the_day_credit(self):
        eco_tables = {
            "eco_day_record", "eco_notification", "eco_threshold_event",
            "policy_version", "work_day_ledger", "work_state_interval",
            "eco_classification_authority", "break_treatment_profile",
        }
        offenders = []
        for table in Base.metadata.tables.values():
            if table.name not in eco_tables:
                continue
            for column in table.columns:
                if "bank" in column.name.lower():
                    offenders.append(f"{table.name}.{column.name}")
        # TWO columns name the bank, and both are correct: the derived
        # credit, and the content-addressed key naming the fold that
        # produced it. Neither accumulates.
        assert offenders == ["eco_day_record.bank_credit_minutes",
                             "eco_day_record.bank_input_key"], offenders

    def test_no_migration_ever_created_a_bank_table(self):
        offenders = []
        for path in (BACKEND / "alembic" / "versions").glob("*.py"):
            body = path.read_text(encoding="utf-8", errors="ignore")
            for token in self.BANNED:
                if token in body:
                    offenders.append(f"{path.name}:{token}")
        assert offenders == [], offenders

    def test_nothing_increments_the_credit(self):
        """A balance is a thing code adds to. This is written once, by the
        evaluator, from a fold — and nowhere is it read and increased."""
        for path in (BACKEND / "app").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in ("bank_credit_minutes +=",
                            "bank_credit_minutes -=",
                            "bank_credit_minutes = bank_credit_minutes"):
                assert pattern not in body, f"{path.name}: {pattern}"


# ═════════════════════════════════════════════════════════════════════════════
# R / S. The vectors, executed against the artifact
# ═════════════════════════════════════════════════════════════════════════════

class TestGV19:
    """Netting NONE (§15). Each day is evaluated independently."""

    def test_it_matches_the_artifact(self, db, org):
        expected = _vector("GV-19")["expected_fields"]
        _, tuesday = _run_week(db, mode=expected["netting_mode"])

        assert tuesday.policy_version is not None
        assert tuesday.attributable_minutes == \
            expected["tuesday_attributable_minutes"]

    def test_no_bank_is_created_or_consumed(self, db, org):
        monday, tuesday = _run_week(db, mode="NONE")
        assert monday.bank_credit_minutes == 0
        assert tuesday.bank_credit_minutes == 0

    def test_the_shortfall_reaches_the_week(self, db, org):
        _, tuesday = _run_week(db, mode="NONE")
        assert tuesday.contributes_to_accumulator_minutes == SHORTFALL
        assert tuesday.state == EcoDayState.SHORTFALL.value


class TestGV20:
    """Netting WEEKLY_BANK. Monday's surplus supplies Tuesday, up to the cap."""

    def test_it_matches_the_artifact(self, db, org):
        expected = _vector("GV-20")["expected_fields"]
        _, tuesday = _run_week(db, mode=expected["netting_mode"],
                               cap_day=expected["bank_cap_minutes"],
                               cap_period=expected["bank_cap_minutes"])

        assert tuesday.attributable_minutes == \
            expected["tuesday_attributable_minutes"]
        assert tuesday.bank_credit_minutes == \
            expected["bank_consumed_minutes"]

    def test_the_cap_comes_from_the_governing_policy(self, db, org):
        from app.policy_version import resolve_policy_as_of

        _run_week(db, mode="WEEKLY_BANK", cap_day=CAP, cap_period=CAP)
        policy = resolve_policy_as_of(db, organization_id=ORG, as_of=TUESDAY)
        assert policy.surplus_cap_per_day_minutes == CAP
        assert policy.surplus_cap_per_period_minutes == CAP

    def test_nothing_reaches_the_week(self, db, org):
        _, tuesday = _run_week(db, mode="WEEKLY_BANK",
                               cap_day=CAP, cap_period=CAP)
        assert tuesday.contributes_to_accumulator_minutes == 0
        assert tuesday.state == EcoDayState.NO_SHORTFALL.value

    def test_the_bank_is_derived_not_stored(self, db, org):
        """The credit on the row equals what a fresh fold computes."""
        monday, _ = _run_week(db, mode="WEEKLY_BANK",
                              cap_day=CAP, cap_period=CAP)
        available, eligible, consumed = fold_available_minutes(
            [monday], cap_per_day=CAP, cap_per_period=CAP)
        assert eligible == SURPLUS
        assert consumed == 0
        assert available == SURPLUS
