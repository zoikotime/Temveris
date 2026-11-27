"""
GET /api/eco/day — the GV-17 visibility surface.
================================================

The property under test: **the route is a seam, not a second implementation.**
It authenticates, asks the authority, honours §44 rollout, reads the persisted
day and renders it — and every one of those answers comes from a primitive that
already existed and is tested elsewhere.

Three gates, in this order, and the order is the point:

    authority  -> 403   outermost, so rollout and record existence cannot leak
    rollout    -> 404   §44.2: OFF nobody, SHADOW administrator only
    record     -> 404   identical answer, so OFF reveals nothing

Exercised through a real ASGI app with dependency overrides, so the wiring is
tested rather than the functions being called directly a second time.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession
from sqlalchemy.pool import StaticPool

from tests._eco_policy import capture_test_policy

import database.models as dbm
from database.database import Base, get_db

from app.auth.service import get_current_user
from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.config_versioning import DatabaseConfigVersionProvider
from app.eco_day_view.router import router as eco_day_router
from app.eco_view_authority import EcoViewGrant
from app.eval_day import evaluate_and_record_day
from app.requirement_resolution import RequirementCandidate
from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2, RolloutState
from app.schedule_policy import keys as K
from app.work_ledger import Provenance
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]

ORG, ORG_B = 8810, 8820
WORKER, ADMIN, OTHER = 8811, 8812, 8813
WORKER_B, ADMIN_B = 8821, 8822
OWNER = 8800

ZONE = "Asia/Kolkata"
IST = timedelta(hours=5, minutes=30)
DAY = date(2026, 9, 7)
CUTOVER = datetime(2026, 9, 1, tzinfo=UTC)
ALL_SEVEN = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
             "SATURDAY", "SUNDAY"]
REQUIRED_MINUTES = 480


def _local(on: date, hour: int, minute: int = 0) -> datetime:
    return datetime(on.year, on.month, on.day, hour, minute, tzinfo=UTC) - IST


@pytest.fixture()
def db():
    import app.clock_session_boundary.models      # noqa: F401
    import app.config_versioning.models           # noqa: F401
    import app.eco_view_authority.models          # noqa: F401
    import app.eval_day.leave_snapshot            # noqa: F401
    import app.eval_day.models                    # noqa: F401
    import app.policy_version.models             # noqa: F401
    import app.break_treatment.models             # noqa: F401
    import app.break_classification.models       # noqa: F401
    import app.rollout.models                     # noqa: F401
    import app.work_ledger.models                 # noqa: F401
    import app.work_ledger_producer.models        # noqa: F401

    # StaticPool + check_same_thread=False, the convention tests/conftest.py
    # already documents: TestClient runs the app in its own thread, and a
    # default in-memory engine hands that thread a DIFFERENT, empty database.
    eng = sa.create_engine(
        "sqlite:///:memory:", future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(eng, expire_on_commit=False) as s:
        s.add_all([dbm.Organization(id=ORG, name="Tenant A"),
                   dbm.Organization(id=ORG_B, name="Tenant B")])
        s.commit()
        s.add_all([
            dbm.User(id=WORKER, name="W", email="w@r.test",
                     organization_id=ORG, role="employee"),
            dbm.User(id=ADMIN, name="A", email="a@r.test",
                     organization_id=ORG, role="admin"),
            dbm.User(id=OTHER, name="O", email="o@r.test",
                     organization_id=ORG, role="employee"),
            dbm.User(id=WORKER_B, name="WB", email="wb@r.test",
                     organization_id=ORG_B, role="employee"),
            dbm.User(id=ADMIN_B, name="AB", email="ab@r.test",
                     organization_id=ORG_B, role="admin"),
            dbm.User(id=OWNER, name="Owner", email="owner@r.test",
                     organization_id=None, role="super_admin"),
        ])
        s.add(dbm.OrgSettings(organization_id=ORG, default_timezone=ZONE))
        s.commit()
        for key, value in ((K.SETTING_WORKING_DAYS, json.dumps(ALL_SEVEN)),
                           (K.SETTING_WEEK_START_DAY, "MONDAY")):
            s.add(dbm.AppSettings(organization_id=ORG, team_key=None,
                                  user_id=None, key=key, value=value))
        s.commit()
        set_producer_cutover(s, organization_id=ORG, cutover_at=CUTOVER,
                             actor_label="owner@r.test")
        s.commit()
        # Explicit historical PolicyVersion: evaluate_and_record_day
        # refuses with POLICY_UNRESOLVED without one. Satisfies the
        # production rule rather than weakening it.
        capture_test_policy(s, organization_id=ORG)
        yield s
    _tz.invalidate_tz_cache()


@pytest.fixture()
def client(db):
    """A real ASGI app carrying only this router, so the WIRING is tested."""
    app = FastAPI()
    app.include_router(eco_day_router)
    app.dependency_overrides[get_db] = lambda: db
    # Replaced per-test by ``_caller``; a default keeps construction simple.
    app.dependency_overrides[get_current_user] = lambda: db.get(dbm.User, WORKER)
    c = TestClient(app)
    c.app = app
    return c


def _caller(client, db, user_id: int):
    client.app.dependency_overrides[get_current_user] = \
        lambda: db.get(dbm.User, user_id)


def _session(db, *, clock_in, clock_out, close_prov=Provenance.WORKER,
             actor="w@r.test"):
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=0, break_seconds=0, offline_seconds=0,
        is_active=clock_out is None)
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=WORKER,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=Provenance.WORKER, actor_label=actor)
    if clock_out is not None:
        append_boundary_event(db, session_id=row.id, user_id=WORKER,
                              boundary=BOUNDARY_CLOCK_OUT,
                              boundary_at=clock_out, provenance=close_prov,
                              actor_label=actor)
    db.commit()
    return row


def _requirement(db):
    DatabaseConfigVersionProvider(db).capture_version(
        organization_id=ORG, config_version_id="route-v1",
        effective_from=date(2026, 1, 1),
        candidates=[RequirementCandidate(tier="TENANT_FALLBACK", rank=6,
                                         minutes=REQUIRED_MINUTES)])
    db.flush()


def _record_a_day(db, *, close_prov=Provenance.WORKER, out_hour=17):
    _requirement(db)
    _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, out_hour),
             close_prov=close_prov,
             actor="SYSTEM" if close_prov is not Provenance.WORKER else "w@r.test")
    assert produce_and_seal_work_day(
        db, organization_id=ORG, user_id=WORKER,
        work_date=DAY).outcome is ProducerOutcome.SEALED
    rec = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                  work_date=DAY)
    assert rec.is_recorded, f"{rec.reason} {rec.detail}"
    db.commit()
    return rec


def _grant(db, *, organization_id=ORG, user_id=ADMIN):
    db.add(EcoViewGrant(organization_id=organization_id, user_id=user_id,
                        granted_by="owner@r.test"))
    db.commit()


def _rollout(db, state: RolloutState, *, organization_id=ORG):
    """Set the durable control directly. The route must READ this, and the
    transition service is not exercised here — that is Step 8's own suite."""
    from app.rollout.models import RolloutControl

    row = db.query(RolloutControl).filter(
        RolloutControl.organization_id == organization_id,
        RolloutControl.control_key == CONTROL_EARLY_CLOCK_OUT_V2).one_or_none()
    if row is None:
        row = RolloutControl(organization_id=organization_id,
                             control_key=CONTROL_EARLY_CLOCK_OUT_V2)
        db.add(row)
    row.state = state.value
    db.commit()


def _get(client, db, *, caller_id, user_id=WORKER, work_date="2026-09-07"):
    _caller(client, db, caller_id)
    return client.get("/api/eco/day",
                      params={"user_id": user_id, "work_date": work_date})


# ══════════════════════════════════════════════════════════════════════════════
# 1. ENABLED — the ordinary paths
# ══════════════════════════════════════════════════════════════════════════════
class TestEnabled:
    @pytest.fixture(autouse=True)
    def _enabled(self, db):
        _rollout(db, RolloutState.ENABLED)

    def test_a_worker_reads_their_own_day(self, client, db):
        """1."""
        _record_a_day(db)
        r = _get(client, db, caller_id=WORKER)
        assert r.status_code == 200, r.text
        assert r.json()["work_date"] == "2026-09-07"

    def test_a_worker_cannot_read_another_worker(self, client, db):
        """2. And the typed reason is not echoed."""
        _record_a_day(db)
        r = _get(client, db, caller_id=OTHER, user_id=WORKER)
        assert r.status_code == 403
        body = r.text
        for leaked in ("NO_GRANT", "CROSS_TENANT", "NO_TENANT_CONTEXT",
                       "TARGET_UNKNOWN"):
            assert leaked not in body, leaked

    def test_a_granted_administrator_reads_a_worker_day(self, client, db):
        """3."""
        _record_a_day(db)
        _grant(db)
        r = _get(client, db, caller_id=ADMIN)
        assert r.status_code == 200, r.text

    def test_a_cross_tenant_administrator_is_denied(self, client, db):
        """4."""
        _record_a_day(db)
        _grant(db, organization_id=ORG_B, user_id=ADMIN_B)
        _rollout(db, RolloutState.ENABLED, organization_id=ORG_B)
        assert _get(client, db, caller_id=ADMIN_B).status_code == 403

    def test_an_administrator_without_a_grant_is_denied(self, client, db):
        """5."""
        _record_a_day(db)
        assert _get(client, db, caller_id=ADMIN).status_code == 403

    def test_a_caller_without_tenant_context_is_denied(self, client, db):
        """6. Includes super_admin, whose tenant id is cleared on login."""
        _record_a_day(db)
        assert _get(client, db, caller_id=OWNER).status_code == 403

    def test_no_day_record_is_404(self, client, db):
        """7."""
        r = _get(client, db, caller_id=WORKER)
        assert r.status_code == 404

    def test_an_unmapped_state_is_200_with_nulls(self, client, db):
        """8. REQUIREMENT_UNRESOLVED: a record exists, no approved label."""
        _session(db, clock_in=_local(DAY, 9), clock_out=_local(DAY, 17))
        produce_and_seal_work_day(db, organization_id=ORG, user_id=WORKER,
                                  work_date=DAY)
        rec = evaluate_and_record_day(db, organization_id=ORG, user_id=WORKER,
                                      work_date=DAY)          # no requirement
        assert rec.is_recorded
        assert rec.record.state == "REQUIREMENT_UNRESOLVED"
        db.commit()

        r = _get(client, db, caller_id=WORKER)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["status"] is None
        assert d["body"] is None
        assert d["has_explanation"] is False
        assert d["copy_keys"] == []


# ══════════════════════════════════════════════════════════════════════════════
# 2. Rollout — §44.2, and it is asymmetric
# ══════════════════════════════════════════════════════════════════════════════
class TestRollout:
    def test_shadow_blocks_the_worker(self, client, db):
        """9. §44.1/§44.2: no Worker-facing surface during shadow."""
        _record_a_day(db)
        _rollout(db, RolloutState.SHADOW)
        assert _get(client, db, caller_id=WORKER).status_code == 404

    def test_shadow_shows_the_administrator(self, client, db):
        """10. §44.2: "The Tenant administrator sees what would have happened."

        The half that makes shadow mode worth having.
        """
        _record_a_day(db)
        _grant(db)
        _rollout(db, RolloutState.SHADOW)
        r = _get(client, db, caller_id=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["evaluation_state"]

    def test_off_blocks_the_worker(self, client, db):
        """11."""
        _record_a_day(db)
        _rollout(db, RolloutState.OFF)
        assert _get(client, db, caller_id=WORKER).status_code == 404

    def test_off_blocks_the_administrator_too(self, client, db):
        """12. Owner Ruling A: OFF means nobody, grant or not."""
        _record_a_day(db)
        _grant(db)
        _rollout(db, RolloutState.OFF)
        assert _get(client, db, caller_id=ADMIN).status_code == 404

    def test_off_does_not_reveal_whether_a_record_exists(self, client, db):
        """13. The response is byte-identical with and without a record."""
        _rollout(db, RolloutState.OFF)
        without = _get(client, db, caller_id=WORKER)
        _record_a_day(db)
        _rollout(db, RolloutState.OFF)
        with_record = _get(client, db, caller_id=WORKER)
        assert without.status_code == with_record.status_code == 404
        assert without.json() == with_record.json()

    def test_absent_rollout_row_reads_as_off(self, client, db):
        """No row means OFF — the rollout service's own default, honoured
        rather than re-implemented. The table ships empty."""
        _record_a_day(db)
        assert _get(client, db, caller_id=WORKER).status_code == 404

    def test_the_route_never_writes_rollout_state(self, client, db):
        """Reading must not transition anything."""
        from app.rollout.models import RolloutControl

        _record_a_day(db)
        _rollout(db, RolloutState.SHADOW)
        before = [(r.state, r.state_version)
                  for r in db.query(RolloutControl).all()]
        _get(client, db, caller_id=WORKER)
        _grant(db)
        _get(client, db, caller_id=ADMIN)
        db.expire_all()
        assert [(r.state, r.state_version)
                for r in db.query(RolloutControl).all()] == before


# ══════════════════════════════════════════════════════════════════════════════
# 3. Contract — audience, parameters, and what is returned
# ══════════════════════════════════════════════════════════════════════════════
class TestContract:
    @pytest.fixture(autouse=True)
    def _enabled(self, db):
        _rollout(db, RolloutState.ENABLED)

    def test_the_audience_comes_from_the_authority_not_the_request(
            self, client, db):
        """15. The same day, the same URL — the body differs only because the
        AUTHORITY differed. A client cannot ask to be an administrator."""
        _record_a_day(db, close_prov=Provenance.STALE_AUTOCLOSE)
        _grant(db)

        worker = _get(client, db, caller_id=WORKER).json()
        admin = _get(client, db, caller_id=ADMIN).json()
        assert worker["status"] == admin["status"] == "Deferred"
        assert "eco.admin.disclaimer" in admin["copy_keys"]
        assert "eco.admin.disclaimer" not in worker["copy_keys"]

    def test_an_audience_parameter_is_ignored(self, client, db):
        """15, adversarially: passing one changes nothing."""
        _record_a_day(db, close_prov=Provenance.STALE_AUTOCLOSE)
        _caller(client, db, WORKER)
        plain = client.get("/api/eco/day",
                           params={"user_id": WORKER, "work_date": "2026-09-07"})
        spoofed = client.get("/api/eco/day",
                             params={"user_id": WORKER,
                                     "work_date": "2026-09-07",
                                     "audience": "ADMINISTRATOR"})
        assert plain.json() == spoofed.json()

    def test_there_is_no_organization_id_parameter(self, client, db):
        """16. Supplying one cannot move the tenant boundary."""
        import inspect

        from app.eco_day_view.router import get_eco_day

        params = set(inspect.signature(get_eco_day).parameters)
        assert "organization_id" not in params
        assert params == {"user_id", "work_date", "db", "caller"}

        _record_a_day(db)
        _caller(client, db, WORKER)
        spoofed = client.get("/api/eco/day",
                             params={"user_id": WORKER,
                                     "work_date": "2026-09-07",
                                     "organization_id": ORG_B})
        assert spoofed.status_code == 200
        assert spoofed.json()["work_date"] == "2026-09-07"

    def test_the_response_carries_the_authorized_fields(self, client, db):
        """Ruling B's list, and nothing invented beyond it."""
        _record_a_day(db)
        d = _get(client, db, caller_id=WORKER).json()
        assert set(d) == {
            "work_date", "status", "body", "has_explanation", "copy_keys",
            "evaluation_state", "evaluation_generation", "ledger_version",
            "policy_version", "config_version", "leave_input_snapshot_key",
            "required_minutes", "qualifying_minutes", "gross_shortfall_minutes",
            "exclusion_minutes", "attributable_minutes",
            "schedule_variance_minutes", "contributes_to_accumulator_minutes",
            "shortfall_computed"}

    def test_qualifying_seconds_is_absent(self, client, db):
        """J. §35 mandates it persisted and §28.1 requires it displayed, but
        `EcoDayRecord` does not carry it. Ruling B forbids inventing it or
        reconstructing it at read time, so it is absent — not zero, not
        derived, not guessed."""
        _record_a_day(db)
        d = _get(client, db, caller_id=WORKER).json()
        assert not any("second" in k for k in d), sorted(d)

    def test_no_exclusion_category_is_fabricated(self, client, db):
        """K. Only the persisted leave snapshot identity is exposed. No
        "Employer closure: none recorded" style claim is manufactured."""
        _record_a_day(db)
        d = _get(client, db, caller_id=WORKER)
        assert d.status_code == 200
        text = d.text.lower()
        for invented in ("employer closure", "authorised release",
                         "authorized release", "none recorded",
                         "none found", "considered"):
            assert invented not in text, invented
        assert d.json()["leave_input_snapshot_key"]     # the real, persisted one

    def test_a_malformed_date_is_400(self, client, db):
        _caller(client, db, WORKER)
        r = client.get("/api/eco/day",
                       params={"user_id": WORKER, "work_date": "07-09-2026"})
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# 4. Rendering, through the route
# ══════════════════════════════════════════════════════════════════════════════
class TestRenderedStatuses:
    @pytest.fixture(autouse=True)
    def _enabled(self, db):
        _rollout(db, RolloutState.ENABLED)

    def test_deferred_worker_rendering_is_exact(self, client, db):
        """19."""
        from app.early_clock_out import OWNER_AUTHORIZED_COPY

        _record_a_day(db, close_prov=Provenance.STALE_AUTOCLOSE)
        d = _get(client, db, caller_id=WORKER).json()
        assert d["status"] == "Deferred"
        assert d["body"] == OWNER_AUTHORIZED_COPY["eco.deferred.body"]
        assert d["has_explanation"] is True
        assert d["contributes_to_accumulator_minutes"] == 0
        assert d["shortfall_computed"] is False

    def test_deferred_administrator_keeps_the_disclaimer_in_body(
            self, client, db):
        """20. §29.1 / §33.4 — in the body, not a footer."""
        from app.early_clock_out import LOCKED_COPY, OWNER_AUTHORIZED_COPY

        _record_a_day(db, close_prov=Provenance.STALE_AUTOCLOSE)
        _grant(db)
        d = _get(client, db, caller_id=ADMIN).json()
        assert d["status"] == "Deferred"
        assert OWNER_AUTHORIZED_COPY["eco.deferred.body"] in d["body"]
        assert LOCKED_COPY["eco.admin.disclaimer"] in d["body"]
        assert "footer" not in d

    def test_complete_renders_complete(self, client, db):
        """21. Eight hours against a 480-minute requirement."""
        _record_a_day(db)
        d = _get(client, db, caller_id=WORKER).json()
        assert d["evaluation_state"] == "NO_SHORTFALL"
        assert d["status"] == "Complete"
        assert d["body"] is None and d["has_explanation"] is False

    def test_counted_renders_counted(self, client, db):
        """22. Seven hours against the same requirement."""
        _record_a_day(db, out_hour=16)
        d = _get(client, db, caller_id=WORKER).json()
        assert d["evaluation_state"] == "SHORTFALL"
        assert d["status"] == "Counted"
        assert d["gross_shortfall_minutes"] == 60

    def test_not_a_working_day_renders_its_label(self, client, db):
        """23."""
        db.query(dbm.AppSettings).filter(
            dbm.AppSettings.key == K.SETTING_WORKING_DAYS).update(
            {"value": json.dumps(["TUESDAY"])})
        db.commit()
        _record_a_day(db)
        d = _get(client, db, caller_id=WORKER).json()
        assert d["evaluation_state"] == "NOT_A_WORKING_DAY"
        assert d["status"] == "Not a working day"
        assert d["body"] is None

    def test_unsupported_and_unknown_states_stay_fail_closed(self):
        """24, 25 — asserted on the mapping the route consumes, which is the
        single place the decision lives."""
        from app.eco_day_view.rendering import (UNSUPPORTED_STATUS_STATES,
                                                _STATUS_KEY_FOR_STATE)

        assert set(UNSUPPORTED_STATUS_STATES) == {
            "REQUIREMENT_UNRESOLVED", "EVALUABLE", "POLICY_UNRESOLVED"}
        for key in ("eco.status.underreview", "eco.status.excluded"):
            assert key not in _STATUS_KEY_FOR_STATE.values()


# ══════════════════════════════════════════════════════════════════════════════
# 5. Historical determinism, and what the route must never do
# ══════════════════════════════════════════════════════════════════════════════
class TestHistoricalAndScope:
    @pytest.fixture(autouse=True)
    def _enabled(self, db):
        _rollout(db, RolloutState.ENABLED)

    def test_current_configuration_cannot_change_the_response(self, client, db):
        """14. Every input a later administrator could plausibly change."""
        _record_a_day(db, close_prov=Provenance.STALE_AUTOCLOSE)
        before = _get(client, db, caller_id=WORKER).json()

        db.query(dbm.AppSettings).filter(
            dbm.AppSettings.key == K.SETTING_WEEK_START_DAY).update(
            {"value": "SUNDAY"})
        db.query(dbm.AppSettings).filter(
            dbm.AppSettings.key == K.SETTING_WORKING_DAYS).update(
            {"value": json.dumps(["TUESDAY"])})
        db.query(dbm.OrgSettings).filter(
            dbm.OrgSettings.organization_id == ORG).update(
            {"default_timezone": "America/New_York"})
        db.commit()
        from app import _tz
        _tz.invalidate_tz_cache()

        assert _get(client, db, caller_id=WORKER).json() == before

    def test_the_response_names_the_frozen_versions(self, client, db):
        """H. The identity that makes the day checkable later."""
        from app.eval_day.models import EcoDayRecord

        _record_a_day(db)
        d = _get(client, db, caller_id=WORKER).json()
        rec = db.query(EcoDayRecord).filter(
            EcoDayRecord.superseded_by.is_(None)).one()
        assert d["ledger_version"] == rec.ledger_version
        assert d["evaluation_generation"] == rec.evaluation_generation
        assert d["config_version"] == rec.config_version
        assert d["policy_version"] == rec.policy_version
        assert d["leave_input_snapshot_key"] == rec.leave_input_snapshot_key

    def test_reading_writes_nothing(self, client, db):
        """18, and the whole read-only claim."""
        _record_a_day(db)
        _grant(db)
        tables = ("eco_day_record", "work_day_ledger", "work_state_interval",
                  "eco_view_grant", "rollout_control", "clock_sessions")
        before = {t: db.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar()
                  for t in tables}
        for caller_id in (WORKER, ADMIN):
            assert _get(client, db, caller_id=caller_id).status_code == 200
        db.expire_all()
        after = {t: db.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar()
                 for t in tables}
        assert after == before

    def test_repeated_reads_are_identical(self, client, db):
        _record_a_day(db, close_prov=Provenance.STALE_AUTOCLOSE)
        first = _get(client, db, caller_id=WORKER).json()
        assert _get(client, db, caller_id=WORKER).json() == first

    def test_the_route_module_performs_no_evaluation_or_legacy_fallback(self):
        """17, 18. Structural: it names no evaluator, no legacy attendance
        source, and no second authorization rule."""
        import ast

        tree = ast.parse(
            (BACKEND / "app" / "eco_day_view" / "router.py").read_text(
                encoding="utf-8"))
        names = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                 | {n.attr for n in ast.walk(tree)
                    if isinstance(n, ast.Attribute)})
        for banned in (
                # evaluation / recomputation
                "evaluate_day", "evaluate_and_record_day", "evaluate_workweek",
                "produce_and_seal_work_day", "get_current_ledger",
                "resolve_requirement_strict",
                # legacy attendance
                "ClockSession", "BreakSession", "resolve_expected_work_seconds",
                "local_day_bounds",
                # a second authorization rule
                "role", "head_user_id", "is_admin",
                # writes and delivery
                "add", "commit", "flush", "notify", "send"):
            assert banned not in names, f"router.py names {banned}"

    def test_the_route_transitions_no_rollout_state(self):
        import ast

        tree = ast.parse(
            (BACKEND / "app" / "eco_day_view" / "router.py").read_text(
                encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for banned in ("transition", "set_canary", "set_observation_days"):
            assert banned not in names, banned

    def test_authority_is_the_outermost_gate(self, client, db):
        """A caller with no authority gets 403 whatever the rollout state —
        so rollout and record existence never leak to them."""
        _record_a_day(db)
        for state in (RolloutState.OFF, RolloutState.SHADOW,
                      RolloutState.ENABLED):
            _rollout(db, state)
            assert _get(client, db, caller_id=OTHER,
                        user_id=WORKER).status_code == 403, state
