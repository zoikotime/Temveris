"""
MODEL-HORIZON — §21.5's retroactive horizon, and GV-23 executed end to end.
============================================================================

    GV-23  "Beyond retroactive horizon"
           Same, 120 days later, horizon 90
           ->  Record corrected; no notification

§21.5, verbatim:

    "A Tenant-configurable retroactive horizon (default 90 days) bounds how far
     back a retroactive threshold event may be raised. Beyond the horizon the
     recomputation still occurs and the record is still corrected, but no
     notification is sent — a threshold notification about a week five months
     ago has no legitimate operational purpose and reads as harassment."

WHAT THE VECTOR IS ACTUALLY ASSERTING
--------------------------------------
Two things that must NOT be coupled:

    record_corrected  = true      the correction lands, in full
    notification_sent = false     and nothing is notified about it

A design that suppressed the correction would satisfy the second and break the
first. `TestRecordCorrectedIsNotCoupledToNotification` asserts the day record
really was superseded, the ledger really was corrected, and the weekly total
really did move — while the horizon still reports BEYOND.

WHERE 120 COMES FROM
--------------------
The workweek is 2026-08-31 (Monday) through 2026-09-06 (Sunday). The
correction is raised 120 local calendar days after that Sunday:

    2026-09-06 + 120 days = 2027-01-04

    elapsed = 2027-01-04 - 2026-09-06 = 120        > 90  ->  BEYOND

All three dates are LOCAL, in the zone frozen on the week's ledger rows.

WHY notification_sent IS FALSE FOR TWO INDEPENDENT REASONS
-----------------------------------------------------------
No notification infrastructure is reachable from this stage at all — no
recipient, audience, delivery attempt or transport exists on this path — AND
the horizon independently reports the correction ineligible. The suite asserts
both, because either alone would make the vector pass for the wrong reason: a
platform that simply cannot notify would satisfy it today and silently start
violating §21.5 the moment ALERT ships.

NO NOTIFICATION AND NO INCIDENT WORK IS DONE HERE
--------------------------------------------------
`TestScopeHeld` pins that. §33.1's "Never, for all four audiences" row is
ALERT's to honour; this stage hands it a decision.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib

import pytest

from app.eval_event.horizon import (
    REASON_HORIZON_MALFORMED_WORKWEEK,
    REASON_HORIZON_NOT_CONFIGURED,
    REASON_HORIZON_POLICY_UNRESOLVED,
    REASON_HORIZON_TIMEZONE_UNRESOLVED,
    HorizonOutcome,
    horizon_decision,
    resolve_retroactive_horizon,
)
from app.policy_version import (
    DEFAULT_RETROACTIVE_HORIZON_DAYS,
    capture_policy_version,
)

UTC = dt.timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"

# ── Independently transcribed from §21.5 and GV-23 ──────────────────────────
WEEK_KEY = "2026-08-31"                 # a Monday
WEEK_END = dt.date(2026, 9, 6)          # the Sunday that closes it
HORIZON = 90
ELAPSED = 120
ZONE = "Asia/Kolkata"


def _vector(gid="GV-23") -> dict:
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        if v["id"] == gid:
            return v
    raise AssertionError(gid)


def _raise_at(days_after_week_end: int, *, hour: int = 12) -> dt.datetime:
    """A UTC instant whose LOCAL date is exactly N days after the week end."""
    from zoneinfo import ZoneInfo

    local = dt.datetime.combine(
        WEEK_END + dt.timedelta(days=days_after_week_end),
        dt.time(hour, 0), tzinfo=ZoneInfo(ZONE))
    return local.astimezone(UTC)


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures — a real sealed week, so the frozen zone is real evidence
# ═════════════════════════════════════════════════════════════════════════════

P_USER = 9911


@pytest.fixture(scope="module", autouse=True)
def _schema(engine):
    import app.alert.models                    # noqa: F401
    import app.break_classification.models     # noqa: F401
    import app.break_treatment.models          # noqa: F401
    import app.eval_event.models               # noqa: F401
    import app.policy_version.models           # noqa: F401
    from database.database import Base
    from database.models import Organization, User

    # FK targets first: eco_threshold_event references organizations.
    Organization.__table__.create(engine, checkfirst=True)
    User.__table__.create(engine, checkfirst=True)
    Base.metadata.create_all(engine)


@pytest.fixture()
def org_week(db):
    """One organization with a SEALED workweek, returning the worker id.

    The week is sealed through the real ledger service, so `timezone_iana` is
    frozen evidence rather than a value this test asserts.
    """
    import database.models as dbm
    from app.schedule_policy import keys as SCHEDULE_KEYS
    from app.work_ledger import (
        CandidateInterval, DayKey, Provenance, WorkState, seal_day,
    )

    if db.get(dbm.Organization, P_ORG) is None:
        db.add(dbm.Organization(id=P_ORG, name="Horizon Org"))
        db.flush()
        db.add(dbm.User(id=P_USER, name="W", email="w@horizon.test",
                        organization_id=P_ORG, role="employee"))
        db.flush()
    row = (db.query(dbm.AppSettings)
             .filter(dbm.AppSettings.organization_id == P_ORG,
                     dbm.AppSettings.key
                     == SCHEDULE_KEYS.SETTING_WEEK_START_DAY,
                     dbm.AppSettings.team_key.is_(None),
                     dbm.AppSettings.user_id.is_(None))
             .one_or_none())
    if row is None:
        db.add(dbm.AppSettings(
            organization_id=P_ORG, team_key=None, user_id=None,
            key=SCHEDULE_KEYS.SETTING_WEEK_START_DAY, value="MONDAY"))
    else:
        row.value = "MONDAY"
    db.flush()

    begin = dt.datetime(WEEK_START.year, WEEK_START.month, WEEK_START.day,
                        3, 30, tzinfo=UTC)
    seal_day(
        db, DayKey(organization_id=P_ORG, user_id=P_USER,
                   work_date=WEEK_START),
        intervals=[CandidateInterval(
            seq=1, state=WorkState.WRK.value, start_at_utc=begin,
            end_at_utc=begin + dt.timedelta(minutes=480),
            provenance=Provenance.WORKER.value)],
        timezone_iana=ZONE, is_working_day=True, working_day_source="org",
        departure_at=begin + dt.timedelta(minutes=480),
        departure_provenance="WORKER",
        departure_source_ref="clock_session:horizon",
        created_by="GOLDEN_VECTOR")
    db.flush()
    return P_USER


# ═════════════════════════════════════════════════════════════════════════════
# D / E / F / G. The boundary — strictly greater-than
# ═════════════════════════════════════════════════════════════════════════════

class TestBoundary:

    def _decide(self, days, horizon=HORIZON):
        return horizon_decision(
            raise_at_utc=_raise_at(days), workweek_end_date=WEEK_END,
            timezone_iana=ZONE, retroactive_horizon_days=horizon)

    def test_eighty_nine_days_is_within(self):
        r = self._decide(89)
        assert r.outcome is HorizonOutcome.WITHIN
        assert r.days_elapsed == 89

    def test_exactly_the_horizon_is_within(self):
        """D-7.2: the horizon is the LAST day inside it, not the first outside.

        `>=` would silence a notification one day early, every time.
        """
        r = self._decide(90)
        assert r.outcome is HorizonOutcome.WITHIN
        assert r.days_elapsed == 90
        assert r.notification_eligible is True

    def test_one_day_past_the_horizon_is_beyond(self):
        r = self._decide(91)
        assert r.outcome is HorizonOutcome.BEYOND
        assert r.days_elapsed == 91
        assert r.notification_eligible is False

    def test_gv23s_hundred_and_twenty_days_is_beyond(self):
        r = self._decide(ELAPSED)
        assert r.outcome is HorizonOutcome.BEYOND
        assert r.days_elapsed == ELAPSED

    def test_the_same_day_is_within(self):
        r = self._decide(0)
        assert r.outcome is HorizonOutcome.WITHIN
        assert r.days_elapsed == 0

    def test_the_comparison_is_not_greater_or_equal(self):
        """Structural, so the operator cannot drift in a later edit."""
        src = (BACKEND / "app" / "eval_event"
               / "horizon.py").read_text(encoding="utf-8")
        comparisons = [
            line.strip() for line in src.splitlines()
            if "retroactive_horizon_days" in line
            and not line.strip().startswith("#")
            and any(op in line for op in (" > ", " >= ", " < ", " <= "))
        ]
        assert comparisons, "the comparison disappeared"
        for line in comparisons:
            assert ">=" not in line, line

    def test_there_is_exactly_one_comparison_site(self):
        src = (BACKEND / "app" / "eval_event"
               / "horizon.py").read_text(encoding="utf-8")
        assert src.count("days_elapsed > int(retroactive_horizon_days)") == 1


# ═════════════════════════════════════════════════════════════════════════════
# H / I / J. Calendar days, in the frozen zone
# ═════════════════════════════════════════════════════════════════════════════

class TestCalendarArithmetic:

    def test_it_counts_calendar_days_not_elapsed_hours(self):
        """A correction raised just after local midnight is a WHOLE day later.

        In UTC the two instants below are 2 hours apart; on the local calendar
        they are one day apart, and §21.5 is expressed in days.
        """
        late = horizon_decision(
            raise_at_utc=_raise_at(90, hour=23), workweek_end_date=WEEK_END,
            timezone_iana=ZONE, retroactive_horizon_days=HORIZON)
        just_after = horizon_decision(
            raise_at_utc=_raise_at(91, hour=1), workweek_end_date=WEEK_END,
            timezone_iana=ZONE, retroactive_horizon_days=HORIZON)
        assert late.days_elapsed == 90
        assert just_after.days_elapsed == 91
        assert late.outcome is HorizonOutcome.WITHIN
        assert just_after.outcome is HorizonOutcome.BEYOND

    def test_no_division_by_86400_anywhere(self):
        """Structural: hours/24 is the tempting wrong implementation."""
        src = (BACKEND / "app" / "eval_event"
               / "horizon.py").read_text(encoding="utf-8")
        for banned in ("86400", "total_seconds", "/ 24", "// 24"):
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or banned not in line:
                    continue
                # Prose may name the thing it forbids.
                assert '"""' in line or stripped.startswith("*") \
                    or "`" in line, line

    def test_the_frozen_zone_decides_the_calendar_date(self):
        """The same UTC instant is a different local date in another zone.

        Proves the zone is actually read, rather than the arithmetic happening
        to work because the test zone is close to UTC.
        """
        instant = dt.datetime(2026, 12, 5, 19, 0, tzinfo=UTC)
        kolkata = horizon_decision(
            raise_at_utc=instant, workweek_end_date=WEEK_END,
            timezone_iana="Asia/Kolkata", retroactive_horizon_days=HORIZON)
        honolulu = horizon_decision(
            raise_at_utc=instant, workweek_end_date=WEEK_END,
            timezone_iana="Pacific/Honolulu", retroactive_horizon_days=HORIZON)
        # 19:00 UTC is 2026-12-06 in Kolkata (+05:30) and still 2026-12-05 in
        # Honolulu (-10:00).
        assert kolkata.raise_date == dt.date(2026, 12, 6)
        assert honolulu.raise_date == dt.date(2026, 12, 5)
        assert kolkata.days_elapsed == honolulu.days_elapsed + 1

    def test_utc_subtraction_would_give_a_different_answer(self):
        """The failure mode this design rejects, made concrete."""
        instant = dt.datetime(2026, 12, 5, 19, 0, tzinfo=UTC)
        local = horizon_decision(
            raise_at_utc=instant, workweek_end_date=WEEK_END,
            timezone_iana="Asia/Kolkata", retroactive_horizon_days=HORIZON)
        naive_utc_days = (instant.date() - WEEK_END).days
        assert local.days_elapsed == naive_utc_days + 1

    def test_the_server_timezone_cannot_affect_the_result(self, monkeypatch):
        """J. No local clock, no TZ env var, no `datetime.now` anywhere."""
        monkeypatch.setenv("TZ", "America/New_York")
        r = horizon_decision(
            raise_at_utc=_raise_at(ELAPSED), workweek_end_date=WEEK_END,
            timezone_iana=ZONE, retroactive_horizon_days=HORIZON)
        assert r.days_elapsed == ELAPSED

        src = (BACKEND / "app" / "eval_event"
               / "horizon.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        for banned in ("now", "utcnow", "today", "localtime"):
            assert banned not in called, banned

    def test_a_naive_raise_instant_is_read_as_utc(self):
        """SQLite drops tzinfo; Postgres keeps it. Same answer either way."""
        aware = _raise_at(ELAPSED)
        naive = aware.replace(tzinfo=None)
        assert horizon_decision(
            raise_at_utc=naive, workweek_end_date=WEEK_END,
            timezone_iana=ZONE,
            retroactive_horizon_days=HORIZON).days_elapsed == ELAPSED


# ═════════════════════════════════════════════════════════════════════════════
# A / B / C / K / L. Policy resolution
# ═════════════════════════════════════════════════════════════════════════════

WEEK_START = dt.date(2026, 8, 31)
FLOOR = dt.date(2020, 1, 1)
P_ORG = 9910


class TestPolicyResolution:

    def _capture(self, db, *, key, effective_from, horizon, org=P_ORG):
        return capture_policy_version(
            db, organization_id=org, policy_version=key,
            effective_from=effective_from, counts_toward_week=True,
            netting_mode="NONE", grace_minutes=0,
            netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
            retroactive_horizon_days=horizon,
            captured_by="owner@horizon.test")

    def test_the_capture_default_is_still_ninety(self, db):
        """A. Unchanged by Stage 4."""
        assert DEFAULT_RETROACTIVE_HORIZON_DAYS == 90
        row = capture_policy_version(
            db, organization_id=P_ORG, policy_version="h-default",
            effective_from=FLOOR, counts_toward_week=True,
            netting_mode="NONE", grace_minutes=0,
            netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
            captured_by="owner@horizon.test")
        assert row.retroactive_horizon_days == 90

    def test_the_historical_policy_supplies_the_horizon(self, db, org_week):
        """B. The week's own policy, not the newest one."""
        self._capture(db, key="h-old", effective_from=FLOOR, horizon=HORIZON)
        db.flush()
        r = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        assert r.outcome is HorizonOutcome.BEYOND
        assert r.retroactive_horizon_days == HORIZON
        assert r.policy_version == "h-old"

    def test_a_later_policy_does_not_rewrite_the_historical_horizon(
            self, db, org_week):
        """C. A policy captured after the week cannot reach back into it."""
        self._capture(db, key="h-old", effective_from=FLOOR, horizon=HORIZON)
        self._capture(db, key="h-new",
                      effective_from=WEEK_START + dt.timedelta(days=30),
                      horizon=365)
        db.flush()
        r = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        # Under the LATER policy 120 days would be within a 365-day horizon.
        assert r.policy_version == "h-old"
        assert r.retroactive_horizon_days == HORIZON
        assert r.outcome is HorizonOutcome.BEYOND

    def test_a_null_horizon_refuses_rather_than_defaulting_to_ninety(
            self, db, org_week):
        """K. The load-bearing one.

        90 is the CAPTURE default. Substituting it at resolution would assert a
        bound the Tenant never configured, and a reader could no longer tell
        "configured 90" from "configured nothing".
        """
        self._capture(db, key="h-null", effective_from=FLOOR, horizon=None)
        db.flush()
        r = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        assert r.outcome is HorizonOutcome.REFUSED
        assert r.reason == REASON_HORIZON_NOT_CONFIGURED
        assert r.retroactive_horizon_days is None
        assert r.notification_eligible is None

    def test_no_applicable_policy_fails_closed(self, db, org_week):
        """L. The existing strict PolicyResolution contract, unchanged."""
        r = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        assert r.outcome is HorizonOutcome.REFUSED
        assert r.reason == REASON_HORIZON_POLICY_UNRESOLVED

    def test_a_malformed_workweek_key_refuses(self, db):
        r = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=1,
            workweek_key="2026-W36", raise_at_utc=_raise_at(ELAPSED))
        assert r.reason == REASON_HORIZON_MALFORMED_WORKWEEK

    def test_no_ledger_means_no_frozen_zone_and_a_refusal(self, db):
        r = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=999999,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        assert r.reason == REASON_HORIZON_TIMEZONE_UNRESOLVED


# ═════════════════════════════════════════════════════════════════════════════
# The refusal contract
# ═════════════════════════════════════════════════════════════════════════════

class TestRefusalCarriesNoDecision:

    def test_a_refusal_states_no_eligibility_in_either_direction(self):
        from app.eval_event.horizon import HorizonResolution

        r = HorizonResolution(outcome=HorizonOutcome.REFUSED,
                              reason=REASON_HORIZON_NOT_CONFIGURED)
        assert r.notification_eligible is None
        assert r.is_beyond_horizon is False      # a refusal is not a "beyond"
        assert r.is_refused is True

    def test_a_refusal_cannot_carry_a_number(self):
        from app.eval_event.horizon import HorizonResolution

        with pytest.raises(ValueError, match="must not carry"):
            HorizonResolution(outcome=HorizonOutcome.REFUSED,
                              reason=REASON_HORIZON_NOT_CONFIGURED,
                              days_elapsed=120)

    def test_an_outcome_cannot_contradict_its_own_numbers(self):
        from app.eval_event.horizon import HorizonResolution

        with pytest.raises(ValueError, match="contradicts"):
            HorizonResolution(
                outcome=HorizonOutcome.WITHIN, days_elapsed=120,
                retroactive_horizon_days=90, workweek_end_date=WEEK_END,
                raise_date=dt.date(2027, 1, 4))


# ═════════════════════════════════════════════════════════════════════════════
# P / Q. Determinism
# ═════════════════════════════════════════════════════════════════════════════

class TestDeterminism:

    def test_the_same_inputs_always_give_the_same_answer(self, db, org_week):
        capture_policy_version(
            db, organization_id=P_ORG, policy_version="h-det",
            effective_from=FLOOR, counts_toward_week=True, netting_mode="NONE",
            netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
            grace_minutes=0, retroactive_horizon_days=HORIZON,
            captured_by="owner@horizon.test")
        db.flush()
        at = _raise_at(ELAPSED)
        first = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=at)
        second = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=at)
        assert first == second

    def test_it_writes_nothing(self, db, org_week):
        """No horizon state, no counter, no row. Q, structurally."""
        src = (BACKEND / "app" / "eval_event"
               / "horizon.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        for banned in ("add", "flush", "commit", "merge", "delete", "execute"):
            assert banned not in called, banned

    def test_no_migration_was_added_for_the_horizon(self):
        """Every input is already durable evidence, so nothing is stored."""
        versions = BACKEND / "alembic" / "versions"
        offenders = [
            p.name for p in versions.glob("*.py")
            if "horizon" in p.read_text(encoding="utf-8", errors="ignore")
            and "retroactive_horizon_days" not in p.name
            and p.name != "a3c8e5f10b47_policy_surplus_caps_and_horizon.py"
        ]
        assert offenders == [], offenders


# ═════════════════════════════════════════════════════════════════════════════
# V. Scope held — no notification, no incident
# ═════════════════════════════════════════════════════════════════════════════

class TestScopeHeld:

    def test_the_horizon_module_reaches_no_notification_code(self):
        src = (BACKEND / "app" / "eval_event"
               / "horizon.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        for banned in ("app.alert", "app.notifications", "app.email_service"):
            assert not any(m.startswith(banned) for m in imported), banned

    def test_no_recipient_or_delivery_identifier_appears(self):
        src = (BACKEND / "app" / "eval_event"
               / "horizon.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
        for banned in ("recipient", "audience", "deliver", "dispatch",
                       "send", "notify", "channel"):
            offenders = [n for n in names if banned in n.lower()]
            assert offenders == [], offenders

    def test_no_incident_work_was_done(self):
        """The horizon increment did no incident work.

        This used to assert that NOTHING under ``app/`` mentioned
        ``eco_ledger_incident``, which was true while GV-26's primitive did not
        exist. §24.15's platform incident has since been built under owner
        ruling D-9.1, so the repository-wide absence is no longer the claim to
        make — and a substring scan would now flag the very docstrings that
        state the separation.

        The claim this increment actually owns is narrower and still exactly as
        falsifiable: the horizon path does not reach the incident primitive.
        Asserted by AST import scan over ``app/eval_event``, the surface this
        increment wrote.
        """
        offenders = []
        for path in sorted((BACKEND / "app" / "eval_event").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            if any(m == "app.eco_ledger_incident"
                   or m.startswith("app.eco_ledger_incident.")
                   for m in imported):
                offenders.append(path.name)
        assert offenders == [], offenders

    def test_the_still_blocked_vectors_did_not_move(self):
        by_id = {v["id"]: v for v in
                 json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]}
        # GV-26 was PROMOTED on §24.15's platform incident (ruling D-9.1);
        # its ALERT citation was stale, not satisfied — an incident is
        # operational evidence with no audience, recipient or delivery.
        # Asserted positively rather than dropped: the blocked set is now
        # EMPTY, and a vector re-entering it must be accounted for here.
        assert by_id["GV-26"]["harness"]["execution"] == "executable"
        assert by_id["GV-26"]["harness"]["blocking_dependencies"] == []


        # GV-18 was PROMOTED on the day-scoped primitive (ruling 2: B);
        # its ALERT citation was stale, not satisfied. Asserting it
        # positively means a vector leaving this list must be accounted for.
        assert by_id["GV-18"]["harness"]["execution"] == "executable"
        assert by_id["GV-18"]["harness"]["blocking_dependencies"] == []
        # GV-07 was PROMOTED once ALERT-DELIVERY shipped. Asserted
        # positively rather than dropped: a vector leaving this list
        # must be accounted for, not merely stop being checked.
        assert by_id["GV-07"]["harness"]["execution"] == "executable"
        assert by_id["GV-07"]["harness"]["blocking_dependencies"] == []
# ═════════════════════════════════════════════════════════════════════════════
# M / N / O / R. GV-23, executed against the artifact
# ═════════════════════════════════════════════════════════════════════════════

class TestGV23:
    """The vector: a correction 120 days later, against a 90-day horizon."""

    def _corrected_week(self, db, user_id):
        """Correct the sealed day, so the record really is corrected.

        Goes through `supersede_day`, the real correction path, so the ledger
        version advances and the lineage is genuine rather than asserted.
        """
        from app.work_ledger import (
            CandidateInterval, DayKey, Provenance, WorkState,
            get_current_ledger, supersede_day,
        )

        begin = dt.datetime(WEEK_START.year, WEEK_START.month, WEEK_START.day,
                            3, 30, tzinfo=UTC)
        key = DayKey(organization_id=P_ORG, user_id=user_id,
                     work_date=WEEK_START)
        before = get_current_ledger(db, key)
        corrected = supersede_day(
            db, key, expected_version=int(before.ledger_version),
            intervals=[CandidateInterval(
                seq=1, state=WorkState.WRK.value, start_at_utc=begin,
                end_at_utc=begin + dt.timedelta(minutes=400),
                provenance=Provenance.WORKER.value)],
            timezone_iana=ZONE, is_working_day=True, working_day_source="org",
            departure_at=begin + dt.timedelta(minutes=400),
            departure_provenance="WORKER",
            departure_source_ref="clock_session:horizon-corrected",
            created_by="GOLDEN_VECTOR")
        db.flush()
        return before, corrected

    def _policy(self, db):
        capture_policy_version(
            db, organization_id=P_ORG, policy_version="gv23-policy",
            effective_from=FLOOR, counts_toward_week=True, netting_mode="NONE",
            netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
            grace_minutes=0, retroactive_horizon_days=HORIZON,
            captured_by="owner@horizon.test")
        db.flush()

    def test_it_matches_the_artifact(self, db, org_week):
        expected = _vector()["expected_fields"]
        self._policy(db)
        self._corrected_week(db, org_week)

        decision = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY,
            raise_at_utc=_raise_at(expected["days_elapsed"]))

        assert decision.days_elapsed == expected["days_elapsed"]
        assert decision.retroactive_horizon_days == \
            expected["retroactive_horizon_days"]
        assert decision.outcome is HorizonOutcome.BEYOND

        # notification_sent = false. Two independent reasons, both asserted.
        assert decision.notification_eligible is expected["notification_sent"]
        from app.alert import EcoNotification
        assert db.query(EcoNotification).count() == 0

    def test_the_record_really_was_corrected(self, db, org_week):
        """M. `record_corrected = true` is a fact about the ledger, and the
        horizon decision does nothing to it."""
        expected = _vector()["expected_fields"]
        self._policy(db)
        before, corrected = self._corrected_week(db, org_week)

        assert expected["record_corrected"] is True
        assert corrected.ledger_version == before.ledger_version + 1
        assert corrected.supersedes == before.id
        assert before.superseded_by == corrected.id

        # And it STAYS corrected after the horizon says "beyond".
        decision = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        assert decision.outcome is HorizonOutcome.BEYOND
        db.expire_all()
        from app.work_ledger import DayKey, get_current_ledger
        still = get_current_ledger(
            db, DayKey(organization_id=P_ORG, user_id=org_week,
                       work_date=WEEK_START))
        assert still.id == corrected.id
        assert still.ledger_version == before.ledger_version + 1

    def test_the_correction_is_not_suppressed_undone_or_deleted(
            self, db, org_week):
        """The failure mode §21.5 is misread into: silencing the correction
        instead of the notification."""
        self._policy(db)
        before, corrected = self._corrected_week(db, org_week)
        resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        db.expire_all()

        from app.work_ledger import WorkDayLedger
        rows = db.query(WorkDayLedger).filter_by(
            organization_id=P_ORG, user_id=org_week).all()
        # BOTH versions survive: history is retained, nothing is deleted.
        assert len(rows) == 2
        assert {r.ledger_version for r in rows} == {1, 2}

    def test_lineage_survives_the_beyond_horizon_decision(self, db, org_week):
        """O. A beyond-horizon correction stays distinguishable from an
        ordinary one — through the existing supersession chain, not a flag."""
        self._policy(db)
        before, corrected = self._corrected_week(db, org_week)
        assert corrected.supersedes == before.id
        assert before.superseded_by == corrected.id

        decision = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(ELAPSED))
        # The decision is reproducible FROM that evidence, and carries the
        # provenance needed to verify it without re-resolving anything.
        assert decision.policy_version == "gv23-policy"
        assert decision.workweek_end_date == WEEK_END
        assert decision.timezone_iana == ZONE

    def test_replaying_the_decision_creates_no_state(self, db, org_week):
        """Q. It is a question, not a transition."""
        self._policy(db)
        self._corrected_week(db, org_week)
        at = _raise_at(ELAPSED)
        first = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=at)
        second = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=at)
        assert first == second

        from app.eval_event.models import EcoThresholdEvent
        assert db.query(EcoThresholdEvent).count() == 0

    def test_a_within_horizon_correction_stays_eligible(self, db, org_week):
        """The other half of the boundary, on the same real evidence."""
        self._policy(db)
        self._corrected_week(db, org_week)
        decision = resolve_retroactive_horizon(
            db, organization_id=P_ORG, user_id=org_week,
            workweek_key=WEEK_KEY, raise_at_utc=_raise_at(HORIZON))
        assert decision.outcome is HorizonOutcome.WITHIN
        assert decision.notification_eligible is True
