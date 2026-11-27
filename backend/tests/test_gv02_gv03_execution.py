"""
GV-02 and GV-03 — the meal/rest distinction, executed end to end.
=================================================================

    GV-02  "Meal/rest distinction (§13.3)"
           Req 525; BRK_R 15 + 20 with cap 30; BRK_M 45 unpaid
           qualifying 500; attributable 25; rest capped at 30 cumulative

    GV-03  "Generic-break regression guard"
           Same ledger as GV-02 evaluated with a single break category
           MUST FAIL. Asserts that no code path collapses BRK_M and BRK_R (INV-4)

Both drive the REAL pipeline: profile capture -> classification authority ->
authoritative assertions -> sealed ledger -> ``evaluate_and_record_day`` -> a
persisted ``EcoDayRecord``. Nothing is stubbed and no arithmetic is restated —
the expected values are read from the locked artifact.

WHY GV-02's NUMBERS ARE WHAT THEY ARE
--------------------------------------
§13.3's worked example, transcribed:

    WRK                     470 min    qualifying 470
    BRK_R  15 min                      qualifying 15   (paid rest, under cap)
    BRK_M  45 min                      qualifying  0   (unpaid meal)
    BRK_R  20 min                      cumulative 35 -> CAPPED at 30, so +15
    ------------------------------------------------------------------
    qualifying total 500 · required 525 · gross 25 · no exclusion · attributable 25

The cap is DAILY and CUMULATIVE (§19.1): 15 + 20 = 35 against a cap of 30
admits 30, never 20 + 20 and never 35.

HOW GV-03 PROVES INV-4
----------------------
It runs the SAME sealed ledger twice. Once under the differentiated profile,
which yields 500/25; once under a single generic bucket, where every break is
subtracted equally and the answer is 470/55. The vector passes only because the
two differ — if a future change ever collapsed the categories the numbers would
converge and the test would fail, which is exactly the D-01 regression it
exists to catch.

REGISTERED IN ``EXTERNAL_EXECUTION_TESTS``
-------------------------------------------
Registration is promotion-coupled, so naming either vector in the registry and
marking it ``executable`` are one act. Both classes were written first and left
unregistered — the TestGV16/17/25/27 precedent — so promotion had something
already proven to promote. GV-02 and GV-03 were promoted on 2026-09-21, closing
P-2 and retiring the last of the ten ORIGINAL dependency codes.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

import database.models as dbm
from database.database import Base

from app.break_classification import (
    ClassificationResult,
    assert_break_classification,
    grant_classification_authority,
)
from app.break_treatment import capture_break_treatment_profile
from app.config_versioning import ConfigValue, ConfigVersion, compute_content_hash
from app.eval_day import EcoDayRecord, EcoDayState, evaluate_and_record_day
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval

UTC = dt.timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

ORG, USER = 9530, 9531
DAY = dt.date(2026, 8, 31)
MONDAY_WEEK = "2026-08-31"
NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
FLOOR = dt.date(2020, 1, 1)

# ── Independently transcribed from §13.3, never imported ────────────────────
REQUIRED_MINUTES = 525
WRK_MINUTES = 470
REST_A_MINUTES = 15
REST_B_MINUTES = 20
MEAL_MINUTES = 45
CAP_MINUTES = 30

#: What a GENERIC single-bucket model returns: WRK only, every break equally
#: subtracted. §13.3 — "a generic-break model … would have returned qualifying
#: 470 and a shortfall of 55 minutes."
GENERIC_QUALIFYING = 470
GENERIC_SHORTFALL = 55


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
        db.add(dbm.Organization(id=ORG, name="GV02 Org"))
        db.flush()
        db.add(dbm.User(id=USER, name="W", email="w@gv02.test",
                        organization_id=ORG, role="employee"))
        db.flush()
    from tests._eco_policy import ensure_test_policy
    ensure_test_policy(db, organization_id=ORG, commit=False)
    db.flush()
    return ORG


def _vector(vid) -> dict:
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == vid:
            return v
    raise AssertionError(vid)


def _config(db):
    values = [{"scope_user_id": None, "tier": "TENANT_FALLBACK", "rank": 6,
               "key": "required_minutes", "value_minutes": REQUIRED_MINUTES,
               "source_ref": None}]
    key = "gv02-cfg-v1"
    version = ConfigVersion(
        organization_id=ORG, version_key=key, effective_from=FLOOR,
        captured_at_utc=dt.datetime(2020, 1, 1, tzinfo=UTC),
        captured_by_label="SYSTEM",
        content_hash=compute_content_hash(
            organization_id=ORG, version_key=key, effective_from=FLOOR,
            values=values))
    db.add(version)
    db.flush()
    for v in values:
        db.add(ConfigValue(config_version_id=version.id,
                           organization_id=ORG, **v))
    db.flush()


def _profile(db, *, ref="gv02-bt-v1", meal=False, rest=True, cap=CAP_MINUTES,
             effective_from=FLOOR):
    capture_break_treatment_profile(
        db, organization_id=ORG, profile_ref=ref,
        effective_from=effective_from, meal_qualifies=meal,
        rest_qualifies=rest, rest_qualifying_cap_minutes=cap,
        captured_by="owner@gv02.test")
    db.flush()


def _sealed_day(db):
    """§13.3's ledger: 470 WRK minutes, with three breaks as GAPS (D-18)."""
    led = WorkDayLedger(
        organization_id=ORG, user_id=USER,
        work_date=dt.datetime(DAY.year, DAY.month, DAY.day),
        workweek_key=MONDAY_WEEK, ledger_version=1,
        state=LedgerState.SEALED.value, timezone_iana="Asia/Kolkata",
        is_working_day=True, working_day_source="org", utc_offset_seconds=0,
        created_at=NOW, created_by="TEST")
    db.add(led)
    db.flush()

    begin = dt.datetime(DAY.year, DAY.month, DAY.day, 9, 0, tzinfo=UTC)
    db.add(WorkStateInterval(
        ledger_id=led.id, organization_id=ORG, seq=1, state="WRK",
        start_at_utc=begin,
        end_at_utc=begin + dt.timedelta(minutes=WRK_MINUTES),
        duration_seconds=WRK_MINUTES * 60, provenance="WORKER",
        created_at=NOW))

    breaks = []
    at = dt.datetime(DAY.year, DAY.month, DAY.day, 11, 0)
    for minutes in (REST_A_MINUTES, MEAL_MINUTES, REST_B_MINUTES):
        row = dbm.BreakSession(
            user_id=USER, organization_id=ORG, break_start=at,
            break_end=at + dt.timedelta(minutes=minutes),
            break_seconds=minutes * 60, is_active=False,
            # OBSERVATIONAL ONLY (P-1). Deliberately set to a value that
            # DISAGREES with the authoritative classification below, so a code
            # path that read it would produce a visibly different answer.
            break_type="break")
        db.add(row)
        db.flush()
        breaks.append((row.id, minutes))
        at = at + dt.timedelta(minutes=minutes + 60)
    return led, breaks


def _authority(db):
    row = grant_classification_authority(
        db, organization_id=ORG, user_id=USER,
        authority_label="hr.compliance@gv02.test", granted_by="owner")
    db.flush()
    return row


def _classify(db, authority, breaks, *, results):
    """One authoritative assertion per break — every break (P2-D5)."""
    for (break_id, _minutes), result in zip(breaks, results):
        assert_break_classification(
            db, organization_id=ORG, user_id=USER, work_date=DAY,
            source_ref=f"break_session:{break_id}", classification=result,
            evidence_ref="ruling:2026-collective-agreement-clause-7",
            authority_id=authority.id)
    db.flush()


#: §13.3's order: rest, meal, rest.
GV02_RESULTS = (ClassificationResult.BRK_R.value,
                ClassificationResult.BRK_M.value,
                ClassificationResult.BRK_R.value)


def _scenario(db, *, meal=False, rest=True, cap=CAP_MINUTES, ref="gv02-bt-v1"):
    _config(db)
    _profile(db, ref=ref, meal=meal, rest=rest, cap=cap)
    _, breaks = _sealed_day(db)
    _classify(db, _authority(db), breaks, results=GV02_RESULTS)
    return breaks


def _record(db):
    out = evaluate_and_record_day(db, organization_id=ORG, user_id=USER,
                                  work_date=DAY)
    assert out.is_recorded, f"{out.reason} {out.detail}"
    return out.record


# ══════════════════════════════════════════════════════════════════════════════
# GV-02
# ══════════════════════════════════════════════════════════════════════════════
class TestGV02:
    """§13.3 — BRK_R qualifies, BRK_M does not, and the cap is cumulative."""

    def test_the_vector_is_promoted(self):
        """Promoted 2026-09-21, once P-2 was satisfied end to end.

        This guard asserted the opposite while the harness existed but no
        promotion had been authorised — the risk then was promoting quietly.
        That risk is gone; the claim inverts, exactly as GV-16's, GV-17's,
        GV-24's, GV-25's and GV-27's did at their own promotions.
        """
        h = _vector("GV-02")["harness"]
        assert h["execution"] == "executable"
        assert h["blocking_dependencies"] == []

    def test_this_harness_is_registered_and_points_here(self):
        src = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-02": ("test_gv02_gv03_execution.py", "class TestGV02")' in src
        assert "assert set(EXTERNAL_EXECUTION_TESTS) <= set(EXECUTABLE_IDS)" in src

    def test_gv02_executes(self, db, org):
        expected = _vector("GV-02")["expected_fields"]
        # The vector's own transcription agrees with §13.3's example.
        assert expected["rest_qualifying_cap_minutes"] == CAP_MINUTES
        assert expected["rest_cap_is_cumulative"] is True

        _scenario(db)
        r = _record(db)

        assert r.required_minutes == REQUIRED_MINUTES
        assert r.qualifying_minutes == expected["qualifying_minutes"]   # 500
        assert r.attributable_minutes == expected["attributable_minutes"]  # 25
        assert r.gross_shortfall_minutes == 25
        assert r.exclusion_minutes == 0
        assert r.state == EcoDayState.SHORTFALL.value

    def test_the_answer_is_computed_not_asserted(self, db, org):
        """Prove the calculation, not the constant.

        Qualifying is derived: WRK + capped rest. Recomputed here from the
        fixture's own inputs rather than from the vector, so a production
        change that happened to match the literal would still fail.
        """
        _scenario(db)
        r = _record(db)

        capped_rest = min(REST_A_MINUTES + REST_B_MINUTES, CAP_MINUTES)
        assert capped_rest == CAP_MINUTES                     # 35 -> 30
        assert r.qualifying_minutes == WRK_MINUTES + capped_rest
        assert r.gross_shortfall_minutes == (
            REQUIRED_MINUTES - (WRK_MINUTES + capped_rest))
        assert r.attributable_minutes == r.gross_shortfall_minutes

    def test_the_meal_contributed_nothing(self, db, org):
        """BRK_M never enters the pool while ``meal_qualifies`` is false."""
        _scenario(db)
        r = _record(db)
        # Had the 45-minute meal qualified, qualifying would be 545.
        assert r.qualifying_minutes == 500
        assert r.qualifying_minutes != WRK_MINUTES + CAP_MINUTES + MEAL_MINUTES

    def test_the_cap_is_cumulative_not_per_interval(self, db, org):
        """§19.1, the note's own example restated as an assertion."""
        _scenario(db)
        r = _record(db)
        per_interval = (min(REST_A_MINUTES, CAP_MINUTES)
                        + min(REST_B_MINUTES, CAP_MINUTES))     # 15 + 20 = 35
        assert per_interval == 35
        assert r.qualifying_minutes == WRK_MINUTES + CAP_MINUTES   # 500
        assert r.qualifying_minutes != WRK_MINUTES + per_interval  # not 505

    def test_the_governing_profile_is_recorded(self, db, org):
        _scenario(db)
        r = _record(db)
        assert r.break_treatment_profile_ref == "gv02-bt-v1"
        assert r.classification_snapshot_key is not None

    def test_the_worker_label_disagreed_and_was_ignored(self, db, org):
        """Every fixture break carries ``break_type='break'``.

        If any path read it as authority, the 45-minute meal would have been
        treated as rest and the answer would differ. It does not.
        """
        _scenario(db)
        rows = db.query(dbm.BreakSession).filter_by(
            organization_id=ORG, user_id=USER).all()
        assert {b.break_type for b in rows} == {"break"}
        assert _record(db).qualifying_minutes == 500


# ══════════════════════════════════════════════════════════════════════════════
# GV-03
# ══════════════════════════════════════════════════════════════════════════════
class TestGV03:
    """INV-4 — collapsing BRK_M and BRK_R must produce a different answer."""

    def test_the_vector_is_promoted(self):
        """Promoted with GV-02: the negative twin needs its positive."""
        h = _vector("GV-03")["harness"]
        assert h["execution"] == "executable"
        assert h["blocking_dependencies"] == []

    def test_this_harness_is_registered_and_points_here(self):
        src = (BACKEND / "tests" / "test_eco_golden_vectors.py").read_text(
            encoding="utf-8")
        assert '"GV-03": ("test_gv02_gv03_execution.py", "class TestGV03")' in src

    def test_the_vector_asserts_inv4(self):
        f = _vector("GV-03")["expected_fields"]
        assert f["must_fail"] is True
        assert f["asserts_invariant"] == "INV-4"

    def test_a_generic_single_bucket_produces_a_different_answer(self, db, org):
        """THE INVARIANT.

        The same sealed ledger, evaluated under a profile that qualifies
        NOTHING — the single generic bucket, where every break is subtracted
        equally. §13.3: that model returns 470 and a shortfall of 55.

        The assertion is the DIFFERENCE. If a future change collapsed the two
        categories the two runs would converge and this fails, which is the
        D-01 regression the vector exists to catch.
        """
        # Collapsed: one bucket, nothing qualifies.
        _config(db)
        _profile(db, ref="gv03-generic", meal=False, rest=False, cap=None)
        _, breaks = _sealed_day(db)
        _classify(db, _authority(db), breaks, results=GV02_RESULTS)
        collapsed = _record(db)

        assert collapsed.qualifying_minutes == GENERIC_QUALIFYING      # 470
        assert collapsed.gross_shortfall_minutes == GENERIC_SHORTFALL  # 55

        # The differentiated answer, from the SAME ledger and the SAME
        # assertions — only the treatment profile differs.
        differentiated_qualifying = _vector("GV-02")["expected_fields"][
            "qualifying_minutes"]
        assert collapsed.qualifying_minutes != differentiated_qualifying
        assert differentiated_qualifying - collapsed.qualifying_minutes == 30
        # §13.3: "a 30-minute error against a 30-minute threshold."
        assert (collapsed.gross_shortfall_minutes
                - _vector("GV-02")["expected_fields"]["attributable_minutes"]
                == 30)

    def test_the_two_categories_are_never_the_same_value(self):
        """INV-4 at the vocabulary level: distinct, and not aliases."""
        assert (ClassificationResult.BRK_M.value
                != ClassificationResult.BRK_R.value)
        assert ClassificationResult.BRK_M.value == "BRK_M"
        assert ClassificationResult.BRK_R.value == "BRK_R"

    def test_meal_and_rest_are_treated_by_separate_profile_fields(self, db, org):
        """A single flag could not express §13.3; two independent ones can.

        Qualifying meal with non-qualifying rest is the inverse of GV-02 and
        must produce its own distinct answer — impossible if one field
        governed both.
        """
        _config(db)
        _profile(db, ref="gv03-inverse", meal=True, rest=False, cap=None)
        _, breaks = _sealed_day(db)
        _classify(db, _authority(db), breaks, results=GV02_RESULTS)
        r = _record(db)

        # Meal qualifies (45, uncapped), rest does not.
        assert r.qualifying_minutes == WRK_MINUTES + MEAL_MINUTES     # 515
        assert r.qualifying_minutes != 500                            # not GV-02
        assert r.qualifying_minutes != GENERIC_QUALIFYING             # not generic
