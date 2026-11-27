"""
PolicyVersion consumed by EVAL-DAY — the seam, the refusal, the provenance.
===========================================================================

Three properties under test:

1. **A day cannot be evaluated under a policy nobody configured.** With no
   applicable PolicyVersion the day records ``POLICY_UNRESOLVED`` and carries no
   measurement at all — §15 forbids assuming ``NONE`` netting and the owner
   forbade a neutral default, so there is nothing to fall back to.
2. **The policy that governed a past day is the one in force on that date**, and
   its identity is persisted beside — never merged into — the configuration,
   ledger and evaluation-generation identities.
3. **The refusal propagates.** EVAL-WEEK cannot complete a week containing one,
   and the visibility renderer shows nothing rather than guessing a label.

WHAT THIS DELIBERATELY DOES NOT TEST
------------------------------------
Any BEHAVIOUR of the five policy attributes. Netting, grace, break treatment and
rounding profiles are each their own increment; this seam resolves a policy,
records which one, and applies none of it. ``test_policy_version.py`` pins that
separately against the consumer's source.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession

import database.models as dbm
from database.database import Base

from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.config_versioning import DatabaseConfigVersionProvider
from app.eval_day import EcoDayState, evaluate_and_record_day
from app.eval_day.models import EcoDayRecord
from app.eval_week import evaluate_workweek
from app.eval_week.outcomes import WeeklyState
from app.policy_version import capture_policy_version
from app.requirement_resolution import RequirementCandidate
from app.schedule_policy import keys as K
from app.work_ledger import Provenance
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]

ORG, WORKER = 7520, 7521
ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)

DAY = date(2026, 9, 7)
WEEK_KEY = "2026-09-07"
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
WORKING_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
REQUIRED_MINUTES = 480

POLICY_KEY = "seam-policy-v1"
CONFIG_KEY = "seam-config-v1"


def _local(on: date, hour: int, minute: int = 0) -> datetime:
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=UTC) - IST


@pytest.fixture()
def db():
    import app.clock_session_boundary.models      # noqa: F401
    import app.config_versioning.models           # noqa: F401
    import app.eval_day.leave_snapshot            # noqa: F401
    import app.eval_day.models                    # noqa: F401
    import app.policy_version.models              # noqa: F401
    import app.break_treatment.models             # noqa: F401
    import app.break_classification.models       # noqa: F401
    import app.work_ledger.models                 # noqa: F401
    import app.work_ledger_producer.models        # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(eng, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Seam Org"))
        s.commit()
        s.add(dbm.User(id=WORKER, name="W", email="w@seam.test",
                       organization_id=ORG, role="employee"))
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(WORKING_DAYS)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@seam.test")
        s.commit()
        # NOTE: no PolicyVersion here, deliberately — each test decides.
        yield s
    _tz.invalidate_tz_cache()


def _config(db):
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id=CONFIG_KEY,
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="TENANT_FALLBACK", rank=6,
                                         minutes=REQUIRED_MINUTES)])
    db.flush()


def _policy(db, *, effective_from=date(2026, 1, 1), key=POLICY_KEY):
    capture_policy_version(
        db, organization_id=ORG, policy_version=key,
        effective_from=effective_from, counts_toward_week=True,
        netting_mode="NONE",
        netting_decision_provenance="EXPLICIT_TENANT_CHOICE",
        grace_minutes=0, captured_by="owner@seam.test")
    db.commit()


def _seal(db, on=DAY, minutes=REQUIRED_MINUTES):
    start = _local(on, 9)
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG, clock_in_time=start,
        clock_out_time=start + timedelta(minutes=minutes),
        total_seconds=0, break_seconds=0, offline_seconds=0, is_active=False)
    db.add(row)
    db.flush()
    for boundary, at in ((BOUNDARY_CLOCK_IN, row.clock_in_time),
                         (BOUNDARY_CLOCK_OUT, row.clock_out_time)):
        append_boundary_event(db, session_id=row.id, user_id=WORKER,
                              boundary=boundary, boundary_at=at,
                              provenance=Provenance.WORKER,
                              actor_label="w@seam.test")
    db.commit()
    assert produce_and_seal_work_day(
        db, organization_id=ORG, user_id=WORKER,
        work_date=on).outcome is ProducerOutcome.SEALED
    db.commit()


def _record(db, on=DAY):
    out = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                  work_date=on)
    db.commit()
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 1. No policy — the refusal
# ══════════════════════════════════════════════════════════════════════════════
class TestMissingPolicy:
    def test_a_day_with_no_policy_records_policy_unresolved(self, db):
        _config(db)
        _seal(db)
        record = _record(db).record
        assert record.state == EcoDayState.POLICY_UNRESOLVED.value

    def test_it_carries_no_measurement_and_no_contribution(self, db):
        """An UNCOMPUTED state. Not zeros — zeros would be readable as a
        calculated result that nobody calculated."""
        _config(db)
        _seal(db)
        record = _record(db).record
        for field in ("required_minutes", "qualifying_minutes",
                      "gross_shortfall_minutes", "exclusion_minutes",
                      "attributable_minutes", "schedule_variance_minutes",
                      "contributes_to_accumulator_minutes"):
            assert getattr(record, field) is None, field
        assert record.shortfall_computed is False
        assert record.policy_version is None

    def test_it_is_not_confused_with_an_unresolved_requirement(self, db):
        """The two failures are reported separately: a Tenant fixes them in
        different places, and ``requirement_outcome`` is untouched."""
        _config(db)          # a requirement EXISTS; only the policy is missing
        _seal(db)
        record = _record(db).record
        assert record.state == EcoDayState.POLICY_UNRESOLVED.value
        assert record.state != EcoDayState.REQUIREMENT_UNRESOLVED.value
        assert record.requirement_outcome == "NOT_APPLICABLE"

    def test_a_policy_effective_after_the_day_does_not_rescue_it(self, db):
        """The historical floor. A policy captured later governs later days."""
        _config(db)
        _policy(db, effective_from=DAY + timedelta(days=1))
        _seal(db)
        assert _record(db).record.state == EcoDayState.POLICY_UNRESOLVED.value


# ══════════════════════════════════════════════════════════════════════════════
# 2. Policy present — provenance
# ══════════════════════════════════════════════════════════════════════════════
class TestResolvedPolicy:
    def test_the_day_evaluates_and_persists_the_policy_identity(self, db):
        _config(db)
        _policy(db)
        _seal(db)
        record = _record(db).record
        assert record.state in (EcoDayState.NO_SHORTFALL.value,
                                EcoDayState.SHORTFALL.value)
        assert record.policy_version == POLICY_KEY

    def test_the_four_identities_stay_distinct(self, db):
        """policy · config · ledger · evaluation generation are four facts."""
        _config(db)
        _policy(db)
        _seal(db)
        record = _record(db).record
        assert record.policy_version == POLICY_KEY
        assert record.config_version == CONFIG_KEY
        assert record.policy_version != record.config_version
        assert record.ledger_version == 1
        assert record.evaluation_generation is not None

    def test_the_requirement_still_comes_from_configuration(self, db):
        """PolicyVersion does not replace Requirement Resolution: it carries no
        daily_minutes and must not have supplied this number."""
        _config(db)
        _policy(db)
        _seal(db)
        record = _record(db).record
        assert record.required_minutes == REQUIRED_MINUTES
        from app.policy_version import PolicyVersion

        assert "daily_minutes" not in PolicyVersion.__table__.c
        assert "required_minutes" not in PolicyVersion.__table__.c

    def test_the_policy_in_force_on_the_day_is_the_one_recorded(self, db):
        """A later policy exists but does not govern an earlier day."""
        _config(db)
        _policy(db, effective_from=date(2026, 1, 1), key="early")
        _policy(db, effective_from=DAY + timedelta(days=7), key="later")
        _seal(db)
        assert _record(db).record.policy_version == "early"

    def test_replay_is_idempotent(self, db):
        """Same ledger, same policy, same inputs — one record, not two."""
        _config(db)
        _policy(db)
        _seal(db)
        first = _record(db).record
        again = _record(db).record
        assert again.id == first.id
        assert db.query(EcoDayRecord).filter_by(work_date=DAY).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# 3. The refusal propagates
# ══════════════════════════════════════════════════════════════════════════════
class TestDownstream:
    def test_the_week_cannot_complete_with_an_unresolved_policy(self, db):
        """Owner ruling: it must BLOCK, never contribute a fabricated value.

        No new weekly behaviour was needed — the fold already refuses any state
        its contract does not place, which is what fail-closed looks like when
        the vocabulary grows.
        """
        _config(db)
        _seal(db)
        _record(db)
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=WEEK_KEY)
        assert week.state is not WeeklyState.COMPLETE
        assert week.weekly_attributable_shortfall_minutes is None

    def test_the_week_completes_once_the_policy_exists(self, db):
        """The other half: the block is caused by the policy, not by the
        fixture being incomplete in some other way."""
        _config(db)
        _policy(db)
        _seal(db)
        _record(db)
        week = evaluate_workweek(db, organization_id=ORG, user_id=WORKER,
                                 workweek_key=WEEK_KEY)
        assert week.state is WeeklyState.COMPLETE

    def test_the_renderer_shows_nothing_for_it(self, db):
        """Owner ruling: no product copy was authorized, so the surface fails
        closed rather than borrowing a §28.1 label that means something else."""
        from app.eco_day_view.rendering import UNSUPPORTED_STATUS_STATES

        assert "POLICY_UNRESOLVED" in UNSUPPORTED_STATUS_STATES
        assert UNSUPPORTED_STATUS_STATES["POLICY_UNRESOLVED"].strip()
        # And it is not mapped to any approved label.
        from app.eco_day_view.rendering import _STATUS_KEY_FOR_STATE

        assert "POLICY_UNRESOLVED" not in _STATUS_KEY_FOR_STATE
