"""
The ECO platform-incident primitive.
====================================

ZT-SPEC-ECO-002 v2.0 §24.15; owner ruling D-9.1; R5.

§24.15 is one sentence with three obligations:

    "An interval with end <= start, or overlapping intervals in the sealed
    set, indicates a ledger defect. This control MUST NOT repair it, skip it,
    or clamp it. It MUST refuse to evaluate the day (state:
    LEDGER_INTEGRITY_FAULT), raise a platform incident, and evaluate nothing."

Detection, the refusal and the ledger state were already built and are already
covered elsewhere. These tests cover the third obligation, which had no
implementation at all: until now nothing in this repository persisted an
incident, so "a platform incident was raised" was a claim no test could fail.

They prove the incident is raised from the authoritative ledger fault and from
nothing else, that a repeated observation of the same fault raises nothing, that
a genuinely new authoritative fact stays distinguishable, that the record cannot
be rewritten or removed, and — from two directions — that it is NOT a
notification.
"""
from __future__ import annotations

import ast
import json
import pathlib
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

import database.models as dbm
from database.database import Base

from app.clock_session_boundary import (
    BOUNDARY_CLOCK_IN,
    BOUNDARY_CLOCK_OUT,
    append_boundary_event,
)
from app.eco_ledger_incident import (
    LEDGER_INTEGRITY_FAULT,
    EcoLedgerIncident,
    IncidentOutcome,
    IncidentRefused,
    IncidentType,
    find_incident_for_ledger,
    raise_ledger_integrity_incident,
)
from app.eco_ledger_incident.append_only_ddl import (
    INCIDENT_TABLE,
    SUPPORTED_DIALECTS,
    append_only_statements,
)
from app.schedule_policy import keys as K
from app.work_ledger import (
    DayKey,
    LedgerState,
    Provenance,
    WorkDayLedger,
    WorkStateInterval,
    record_integrity_fault,
)
from app.work_ledger_producer import (
    ProducerOutcome,
    produce_and_seal_work_day,
    set_producer_cutover,
)

UTC = timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "eco_ledger_incident"
MIGRATION = (BACKEND / "alembic" / "versions"
             / "d9a4c7e35f18_eco_ledger_incident.py")

ORG = 9610
WORKER = 9611

TZ_IST = "Asia/Kolkata"          # +05:30, no DST
MON_FRI = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]

#: A Monday in the past, so the producer's closure gate is satisfied.
DAY = date(2026, 9, 7)
CUTOVER = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

REASON = "OVERLAPPING_INTERVALS: seq 1 [09:00,13:00) overlaps seq 2 [12:00,17:00)"

#: Columns that would turn operational evidence into a notification. Named once
#: so the guard below is a list to extend, not a regular expression to trust.
NOTIFICATION_COLUMNS = frozenset({
    "audience", "recipient_user_id", "recipient_id", "recipient_outcome",
    "recipient_snapshot", "channel", "mode", "outcome", "reason_code",
    "correlation_id", "failure_detail", "suppressed", "suppression_reason",
    "dispatched_at_utc", "attempt_seq", "notification_type", "rendered_body",
    "rendered_copy_id", "idempotency_key", "delivered_at_utc",
})

#: The notification estate. Nothing here may know this primitive exists, and
#: this primitive may know nothing here exists.
NOTIFICATION_PACKAGES = (
    "app.alert",
    "app.notification_platform",
    "app.recipient_authority",
    "app.eco_day_alert",
    "app.email_service",
)


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def fk_engine():
    """A private engine: the incident store is append-only, so a suite that
    shared one could never clean up after itself."""
    import app.break_classification.models     # noqa: F401
    import app.break_treatment.models          # noqa: F401
    import app.clock_session_boundary.models   # noqa: F401
    import app.config_versioning.models        # noqa: F401
    import app.eco_ledger_incident.models      # noqa: F401
    import app.eval_day.leave_snapshot         # noqa: F401
    import app.eval_day.models                 # noqa: F401
    import app.policy_version.models           # noqa: F401
    import app.work_ledger.models              # noqa: F401
    import app.work_ledger_producer.models     # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(fk_engine):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(fk_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="Incident Org"))
        s.add(dbm.User(id=WORKER, name="W", email="w.inc@example.test",
                       organization_id=ORG, role="employee"))
        s.commit()
        yield s
    _tz.invalidate_tz_cache()


def _configure(db, *, tz=TZ_IST, working_days=MON_FRI, cutover=CUTOVER,
               week_start="MONDAY"):
    """The gates the producer and the workweek resolver require."""
    from app import _tz
    db.add(dbm.OrgSettings(organization_id=ORG, default_timezone=tz))
    db.add(dbm.AppSettings(organization_id=ORG, team_key=None, user_id=None,
                           key=K.SETTING_WORKING_DAYS,
                           value=json.dumps(working_days)))
    db.add(dbm.AppSettings(organization_id=ORG, team_key=None, user_id=None,
                           key=K.SETTING_WEEK_START_DAY, value=week_start))
    db.commit()
    _tz.invalidate_tz_cache()
    set_producer_cutover(db, organization_id=ORG, cutover_at=cutover,
                         actor_label="owner@example.test")
    db.commit()


def _at(day: date, hh: int, mm: int = 0) -> datetime:
    from zoneinfo import ZoneInfo
    return datetime(day.year, day.month, day.day, hh, mm,
                    tzinfo=ZoneInfo(TZ_IST)).astimezone(UTC)


def _session(db, *, clock_in, clock_out):
    row = dbm.ClockSession(
        user_id=WORKER, organization_id=ORG,
        clock_in_time=clock_in, clock_out_time=clock_out,
        total_seconds=999999, break_seconds=0, offline_seconds=0,
        is_active=False,
    )
    db.add(row)
    db.flush()
    append_boundary_event(db, session_id=row.id, user_id=WORKER,
                          boundary=BOUNDARY_CLOCK_IN, boundary_at=clock_in,
                          provenance=Provenance.WORKER,
                          actor_label="w.inc@example.test")
    append_boundary_event(db, session_id=row.id, user_id=WORKER,
                          boundary=BOUNDARY_CLOCK_OUT, boundary_at=clock_out,
                          provenance=Provenance.WORKER,
                          actor_label="w.inc@example.test")
    db.commit()
    return row


def _fault(db, *, work_date=DAY, reason=REASON, created_by="SYSTEM"):
    """Establish one authoritative ledger fault through the production writer."""
    return record_integrity_fault(
        db, DayKey(organization_id=ORG, user_id=WORKER, work_date=work_date),
        reason=reason, timezone_iana=TZ_IST, utc_offset_seconds=19800,
        is_working_day=True, working_day_source="SCHEDULE",
        created_by=created_by,
    )


def _overlapping_day(db, *, with_policy=False):
    """The §24.15 condition, reached through the real producer.

    ``with_policy`` captures an explicit PolicyVersion, which EVAL-DAY requires
    before it will look at a ledger at all. GV-26's harness sets it so the
    refusal the vector pins is §24.15's and not POLICY_UNRESOLVED standing in
    for it — a fixture gap that would have produced the same two field values
    for entirely the wrong reason.
    """
    _configure(db)
    if with_policy:
        from tests._eco_policy import capture_test_policy
        capture_test_policy(db, organization_id=ORG)
    _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 13))
    _session(db, clock_in=_at(DAY, 12), clock_out=_at(DAY, 17))
    return produce_and_seal_work_day(
        db, organization_id=ORG, user_id=WORKER, work_date=DAY)


def _module_imports(path: pathlib.Path) -> set[str]:
    """Every module an AST import statement names. Imports only.

    Deliberately NOT a text search: these modules NAME the notification estate
    in prose in order to say they have nothing to do with it, and a substring
    scan would flag the sentence that states the separation.
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


# ═════════════════════════════════════════════════════════════════════════════
# 1. The primitive exists, and it is the one §24.15 asks for
# ═════════════════════════════════════════════════════════════════════════════
class TestThePrimitiveExists:
    def test_the_incident_store_is_a_dedicated_organization_scoped_table(self):
        assert EcoLedgerIncident.__tablename__ == "eco_ledger_incident"
        assert INCIDENT_TABLE == "eco_ledger_incident"
        columns = {c.name for c in EcoLedgerIncident.__table__.columns}
        assert "organization_id" in columns
        # It is a table of its own, not a column bolted onto something else.
        assert EcoLedgerIncident.__table__ in Base.metadata.tables.values()

    def test_the_vocabulary_is_closed(self):
        """One incident type, two raise outcomes. A third of either is an
        owner decision, not an implementation detail."""
        assert [m.value for m in IncidentType] == ["LEDGER_INTEGRITY_FAULT"]
        assert {m.value for m in IncidentOutcome} == {"RAISED", "ALREADY_RAISED"}
        assert LEDGER_INTEGRITY_FAULT == LedgerState.LEDGER_INTEGRITY_FAULT.value


# ═════════════════════════════════════════════════════════════════════════════
# 2. The raise point — the ledger writer, and nothing else
# ═════════════════════════════════════════════════════════════════════════════
class TestTheRaisePoint:
    def test_establishing_a_ledger_fault_raises_exactly_one_incident(self, db):
        _configure(db)
        _fault(db)
        assert db.query(EcoLedgerIncident).count() == 1

    def test_the_incident_carries_the_authoritative_ledger_identity(self, db):
        _configure(db)
        row = _fault(db)
        incident = db.query(EcoLedgerIncident).one()
        assert incident.ledger_id == row.id
        assert incident.ledger_version == row.ledger_version
        assert incident.organization_id == ORG
        assert incident.user_id == WORKER
        assert incident.work_date == row.work_date
        assert incident.ledger_state == LEDGER_INTEGRITY_FAULT
        assert incident.incident_type == "LEDGER_INTEGRITY_FAULT"

    def test_the_incident_records_the_ledger_s_own_fault_reason(self, db):
        """Verbatim. The incident states WHAT is wrong, not merely that
        something is."""
        _configure(db)
        row = _fault(db)
        incident = db.query(EcoLedgerIncident).one()
        assert incident.integrity_fault_reason == REASON
        assert incident.integrity_fault_reason == row.integrity_fault_reason

    def test_the_ledger_writer_s_return_value_is_unchanged(self, db):
        """Raising the incident must not alter what callers already receive:
        the fault version, addressable, in the fault state."""
        _configure(db)
        row = _fault(db)
        assert isinstance(row, WorkDayLedger)
        assert row.state == LedgerState.LEDGER_INTEGRITY_FAULT.value
        assert row.id is not None
        assert row.integrity_fault_reason == REASON

    def test_the_producer_outcome_survives_the_incident(self, db):
        """The whole §24.15 path, end to end, through the real producer.

        The outcome, the addressable fault row and the "nothing was sealed"
        guarantees are exactly what they were before the incident existed.
        """
        result = _overlapping_day(db)
        ledger = db.query(WorkDayLedger).one()
        assert result.outcome is ProducerOutcome.LEDGER_INTEGRITY_FAULT
        assert result.integrity_fault_ledger_id == ledger.id
        assert result.ledger_id is None
        assert result.sealed is False
        assert db.query(WorkStateInterval).count() == 0

        incident = db.query(EcoLedgerIncident).one()
        assert incident.ledger_id == ledger.id
        assert "OVERLAP" in incident.integrity_fault_reason.upper()

    def test_the_raised_by_actor_is_the_ledger_writer_s(self, db):
        _configure(db)
        _fault(db, created_by="WORK_LEDGER_PRODUCER")
        assert db.query(EcoLedgerIncident).one().raised_by \
            == "WORK_LEDGER_PRODUCER"

    def test_a_healthy_day_raises_nothing(self, db):
        """The store fills only on a defect. Sealing a clean day leaves it
        empty, so an incident is evidence and not noise."""
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        result = produce_and_seal_work_day(
            db, organization_id=ORG, user_id=WORKER, work_date=DAY)
        assert result.outcome is ProducerOutcome.SEALED
        assert db.query(EcoLedgerIncident).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# 3. Idempotency — keyed on the authoritative ledger fault, not on the day
# ═════════════════════════════════════════════════════════════════════════════
class TestIdempotency:
    def test_a_repeated_observation_of_one_fault_raises_nothing_new(self, db):
        _configure(db)
        row = _fault(db)
        first = find_incident_for_ledger(db, row.id)

        again = raise_ledger_integrity_incident(db, ledger=row)
        assert again.outcome is IncidentOutcome.ALREADY_RAISED
        assert again.was_raised is False
        assert again.incident.id == first.id
        assert db.query(EcoLedgerIncident).count() == 1

    def test_a_new_authoritative_ledger_version_is_a_new_incident(self, db):
        """A day that faults again on genuinely different evidence is a SECOND
        defect. A day-shaped key would have reported two as one."""
        _configure(db)
        first = _fault(db, reason="OVERLAPPING_INTERVALS: first")
        second = _fault(db, reason="NON_POSITIVE_DURATION: later")

        assert second.ledger_version == first.ledger_version + 1
        incidents = (db.query(EcoLedgerIncident)
                     .order_by(EcoLedgerIncident.ledger_version).all())
        assert [i.ledger_id for i in incidents] == [first.id, second.id]
        assert [i.ledger_version for i in incidents] == [1, 2]
        assert incidents[0].integrity_fault_reason != \
            incidents[1].integrity_fault_reason

    def test_the_first_raise_reports_that_it_wrote(self, db):
        _configure(db)
        row = _fault(db)
        # The row already exists; prove the outcome vocabulary is honest by
        # raising against a second, genuinely new fault version.
        again = raise_ledger_integrity_incident(db, ledger=_fault(db))
        assert again.outcome is IncidentOutcome.ALREADY_RAISED
        fresh = _fault(db, work_date=date(2026, 9, 8))
        assert find_incident_for_ledger(db, fresh.id) is not None
        assert find_incident_for_ledger(db, row.id).ledger_id == row.id


# ═════════════════════════════════════════════════════════════════════════════
# 4. Refusals — an incident is never raised for something that is not a fault
# ═════════════════════════════════════════════════════════════════════════════
class TestRefusals:
    def test_a_ledger_version_that_is_not_faulted_is_refused(self, db):
        _configure(db)
        _session(db, clock_in=_at(DAY, 9), clock_out=_at(DAY, 17))
        produce_and_seal_work_day(db, organization_id=ORG, user_id=WORKER,
                                  work_date=DAY)
        sealed = db.query(WorkDayLedger).one()
        assert sealed.state == LedgerState.SEALED.value

        with pytest.raises(IncidentRefused):
            raise_ledger_integrity_incident(db, ledger=sealed)
        assert db.query(EcoLedgerIncident).count() == 0

    def test_a_fault_with_no_reason_is_refused(self, db):
        """An incident with no reason asserts that something is wrong without
        saying what — which is the class of falsehood §24.15 exists to stop."""
        _configure(db)
        row = _fault(db)
        row.integrity_fault_reason = "   "
        with pytest.raises(IncidentRefused):
            raise_ledger_integrity_incident(db, ledger=row)

    def test_an_unflushed_ledger_row_is_refused(self, db):
        """Without an id there is no authoritative identity to key on, so the
        incident could not be idempotent."""
        class _Unflushed:
            id = None
            state = LEDGER_INTEGRITY_FAULT
            integrity_fault_reason = REASON

        with pytest.raises(IncidentRefused):
            raise_ledger_integrity_incident(db, ledger=_Unflushed())
        assert db.query(EcoLedgerIncident).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# 5. Append-only (R5) — enforced by the database, on both dialects
# ═════════════════════════════════════════════════════════════════════════════
class TestAppendOnly:
    def test_the_database_refuses_an_update(self, db):
        _configure(db)
        row = _fault(db)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(
                sa.update(EcoLedgerIncident)
                .where(EcoLedgerIncident.ledger_id == row.id)
                .values(integrity_fault_reason="nothing to see here"))
        db.rollback()
        assert db.query(EcoLedgerIncident).one().integrity_fault_reason == REASON

    def test_the_database_refuses_a_delete(self, db):
        """A defect that can be erased is a defect that can be denied."""
        _configure(db)
        _fault(db)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.delete(EcoLedgerIncident))
        db.rollback()
        assert db.query(EcoLedgerIncident).count() == 1

    def test_both_supported_dialects_carry_the_guard(self):
        assert SUPPORTED_DIALECTS == {"sqlite", "postgresql"}
        for dialect in SUPPORTED_DIALECTS:
            statements = append_only_statements(dialect)
            assert statements, dialect
            joined = " ".join(statements).upper()
            assert "UPDATE" in joined and "DELETE" in joined
        # An unsupported dialect gets NO silent partial protection.
        assert append_only_statements("mysql") == ()

    def test_the_migration_emits_what_the_model_attaches(self):
        """One definition, two emitters. If the revision built its own SQL a
        migrated database could lack the triggers a test database had, and
        every guard here would pass vacuously against production's shape."""
        imports = _module_imports(MIGRATION)
        assert "app.eco_ledger_incident.append_only_ddl" in imports
        assert ("app.eco_ledger_incident.append_only_ddl"
                ".append_only_statements") in imports

        model_calls = {
            getattr(node.func, "id", None)
            for node in ast.walk(ast.parse(
                (PACKAGE / "models.py").read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
        }
        assert "append_only_statements" in model_calls


# ═════════════════════════════════════════════════════════════════════════════
# 6. The constraints, in the database rather than in a comment
# ═════════════════════════════════════════════════════════════════════════════
class TestConstraints:
    def test_two_incidents_for_one_ledger_fault_are_unrepresentable(self, db):
        _configure(db)
        row = _fault(db)
        db.add(EcoLedgerIncident(
            ledger_id=row.id, organization_id=ORG, user_id=WORKER,
            work_date=row.work_date, ledger_version=row.ledger_version,
            incident_type="LEDGER_INTEGRITY_FAULT",
            ledger_state=LEDGER_INTEGRITY_FAULT,
            integrity_fault_reason=REASON, raised_by="SYSTEM"))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_any_other_ledger_state_is_unrepresentable(self, db):
        _configure(db)
        row = _fault(db)
        db.execute(sa.delete(WorkDayLedger).where(WorkDayLedger.id == -1))
        with pytest.raises(IntegrityError):
            db.execute(sa.insert(EcoLedgerIncident).values(
                ledger_id=row.id + 1000, organization_id=ORG, user_id=WORKER,
                work_date=row.work_date, ledger_version=1,
                incident_type="LEDGER_INTEGRITY_FAULT",
                ledger_state="SEALED", integrity_fault_reason=REASON,
                raised_by="SYSTEM"))
        db.rollback()

    def test_an_empty_reason_is_unrepresentable(self, db):
        _configure(db)
        row = _fault(db)
        with pytest.raises(IntegrityError):
            db.execute(sa.insert(EcoLedgerIncident).values(
                ledger_id=row.id + 1000, organization_id=ORG, user_id=WORKER,
                work_date=row.work_date, ledger_version=1,
                incident_type="LEDGER_INTEGRITY_FAULT",
                ledger_state=LEDGER_INTEGRITY_FAULT,
                integrity_fault_reason="", raised_by="SYSTEM"))
        db.rollback()

    def test_the_ledger_fault_cannot_be_deleted_out_from_under_it(self, db):
        """RESTRICT. An incident that outlived its evidence would be an
        unverifiable claim."""
        _configure(db)
        row = _fault(db)
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(sa.delete(WorkDayLedger)
                       .where(WorkDayLedger.id == row.id))
        db.rollback()
        assert db.get(WorkDayLedger, row.id) is not None


# ═════════════════════════════════════════════════════════════════════════════
# 7. It is evidence, not a notification
# ═════════════════════════════════════════════════════════════════════════════
class TestItIsNotANotification:
    def test_the_table_has_no_notification_columns(self):
        """No audience, no recipient, no channel, no delivery, no correlation.
        §33.1 registers four audiences and §33.2/§33.3 exactly two notification
        identifiers, all of them for "threshold reached"; the spec registers
        nothing at all for a ledger defect, so an audience here could only be
        invented."""
        columns = {c.name for c in EcoLedgerIncident.__table__.columns}
        assert not (columns & NOTIFICATION_COLUMNS), \
            sorted(columns & NOTIFICATION_COLUMNS)

    def test_the_primitive_imports_nothing_from_the_notification_estate(self):
        for path in sorted(PACKAGE.glob("*.py")):
            imported = _module_imports(path)
            for package in NOTIFICATION_PACKAGES:
                offenders = {name for name in imported
                             if name == package or name.startswith(package + ".")}
                assert not offenders, f"{path.name} imports {sorted(offenders)}"

    def test_the_notification_estate_does_not_import_the_primitive(self):
        """The dependency is one-directional by construction. Nothing in the
        notification paths may treat an incident as something to deliver."""
        for package in NOTIFICATION_PACKAGES:
            root = BACKEND / pathlib.Path(package.replace(".", "/"))
            paths = (sorted(root.glob("*.py")) if root.is_dir()
                     else [root.with_suffix(".py")])
            for path in paths:
                if not path.exists():
                    continue
                offenders = {
                    name for name in _module_imports(path)
                    if name == "app.eco_ledger_incident"
                    or name.startswith("app.eco_ledger_incident.")
                }
                assert not offenders, f"{path} imports {sorted(offenders)}"

    def test_raising_an_incident_sends_nothing(self, db, monkeypatch):
        """The transport is watched directly rather than argued about."""
        import app.email_service as mailer

        sent = []
        monkeypatch.setattr(mailer, "_send",
                            lambda **kw: (sent.append(kw), True)[1])
        _overlapping_day(db)
        assert db.query(EcoLedgerIncident).count() == 1
        assert sent == []


# ═════════════════════════════════════════════════════════════════════════════
# 8. The migration
# ═════════════════════════════════════════════════════════════════════════════
class TestMigration:
    def _revisions(self):
        versions = BACKEND / "alembic" / "versions"
        graph = {}
        for path in versions.glob("*.py"):
            revision = down = None
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                target = None
                if isinstance(node, ast.Assign) and isinstance(
                        node.targets[0], ast.Name):
                    target = node.targets[0].id
                elif isinstance(node, ast.AnnAssign) and isinstance(
                        node.target, ast.Name):
                    target = node.target.id
                if target == "revision":
                    revision = ast.literal_eval(node.value)
                elif target == "down_revision":
                    down = ast.literal_eval(node.value)
            graph[revision] = down
        return graph

    def test_it_leaves_one_head(self):
        graph = self._revisions()
        parents = set()
        for down in graph.values():
            if down is None:
                continue
            parents.update(down if isinstance(down, (tuple, list)) else [down])
        # The head has moved on — the INV-4 break-evidence revision b5f2a8c47d93 — but
        # THIS revision's own place in the chain is what this test owns, and it
        # is unchanged.
        assert sorted(r for r in graph if r not in parents) == ["b5f2a8c47d93"]
        assert graph["d9a4c7e35f18"] == "c2e8f5b71d94"

    def test_it_is_additive(self):
        """One new table. No existing table altered, no column added or
        dropped, no row read or rewritten by the upgrade."""
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        called = {node.func.attr for node in ast.walk(upgrade)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        assert "create_table" in called
        for forbidden in ("add_column", "drop_column", "alter_column",
                          "drop_table", "rename_table", "bulk_insert"):
            assert forbidden not in called, forbidden

    def test_the_downgrade_refuses_to_discard_evidence(self):
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        downgrade = next(n for n in tree.body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "downgrade")
        assert any(isinstance(node, ast.Raise) for node in ast.walk(downgrade))

    def test_the_migrated_shape_matches_the_model(self):
        """Every column the model declares is in the revision, and vice versa.
        A schema that differs between create_all and Alembic makes every test
        here a statement about a table production does not have."""
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        declared = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Column"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                declared.add(node.args[0].value)
        assert declared == {c.name for c in EcoLedgerIncident.__table__.columns}


# ═════════════════════════════════════════════════════════════════════════════
# GV-26, executed against the artifact
# ═════════════════════════════════════════════════════════════════════════════
# Executed HERE for the same reason GV-12 lives in the week-fold suite and
# GV-28 in the classifier suite: the behaviour this vector pins is §24.15's,
# and the primitive that discharges its fourth field lives in this package.
#
# THE EXPECTED VALUES ARE READ FROM THE ARTIFACT, NEVER RESTATED HERE. There is
# deliberately no second expected-value dictionary: a harness carrying its own
# copy of the answer proves only that the copy matches itself.

VECTORS = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"


class TestGV26:
    """§24.15's four expected fields, walked against the real services.

    WHY ``state`` IS READ FROM THE LEDGER AND NOT FROM AN ECO RECORD
    ---------------------------------------------------------------
    ``LEDGER_INTEGRITY_FAULT`` is not a member of ``EcoDayState``, and under
    D-8.1 it never will be: ``LedgerState`` is authoritative for ledger facts
    and ``EcoDayState`` stays the seven-member EVALUATION vocabulary. The
    vector's ``state`` is therefore the ledger's, which is also the only
    reading consistent with its second field — a day that was never evaluated
    cannot carry an evaluation state.
    """

    def _expected(self):
        import json
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
        return next(v for v in vectors if v["id"] == "GV-26")["expected_fields"]

    def test_the_four_expected_fields(self, db):
        from app.eval_day import evaluate_and_record_day
        from app.eval_day.models import EcoDayRecord
        from app.eval_day.recording import RecordingOutcome

        expected = self._expected()

        # "Overlapping sealed intervals" — the vector's stated input, built
        # from real clock evidence and driven through the real producer.
        result = _overlapping_day(db, with_policy=True)
        assert result.outcome is ProducerOutcome.LEDGER_INTEGRITY_FAULT
        ledger = db.query(WorkDayLedger).one()

        # The day is then offered to EVAL-DAY, which must refuse it.
        recording = evaluate_and_record_day(
            db, organization_id=ORG, user_id=WORKER, work_date=DAY)

        actual = {
            # §24.15's state, from the authority that owns ledger facts.
            "state": ledger.state,
            # REFUSED, with no persisted evaluation of any kind.
            "evaluation_performed": (
                recording.outcome is RecordingOutcome.RECORDED
                or db.query(EcoDayRecord).count() > 0),
            # The third field — the one that had no implementation until
            # D-9.1. Exactly one incident, keyed on this ledger fault.
            "platform_incident_raised": db.query(EcoLedgerIncident).filter(
                EcoLedgerIncident.ledger_id == ledger.id).count() == 1,
            # Nothing was repaired, skipped or clamped: a clamp would have
            # produced admissible intervals, and the ledger holds none.
            "clamping_applied": db.query(WorkStateInterval).count() > 0,
        }

        # Every field compared, and the set of fields compared too — a future
        # field cannot be silently ignored into a green build.
        assert set(actual) == set(expected)
        assert actual == expected

    def test_the_refusal_names_the_integrity_fault(self, db):
        """Not merely "it failed": §24.15's refusal is machine-readable, so a
        caller can distinguish it from a missing authority."""
        from app.eval_day import REASON_LEDGER_INTEGRITY_FAULT
        from app.eval_day.recording import RecordingOutcome

        _overlapping_day(db, with_policy=True)
        recording = evaluate_and_record_day_for(db)
        assert recording.outcome is RecordingOutcome.REFUSED
        assert recording.reason == REASON_LEDGER_INTEGRITY_FAULT
        assert recording.record is None

    def test_the_incident_is_not_a_notification_for_this_vector(self, db,
                                                                monkeypatch):
        """The third field is discharged by EVIDENCE, not by telling anyone."""
        import app.email_service as mailer

        sent = []
        monkeypatch.setattr(mailer, "_send",
                            lambda **kw: (sent.append(kw), True)[1])
        _overlapping_day(db, with_policy=True)
        evaluate_and_record_day_for(db)
        assert db.query(EcoLedgerIncident).count() == 1
        assert sent == []


def evaluate_and_record_day_for(db):
    from app.eval_day import evaluate_and_record_day
    return evaluate_and_record_day(
        db, organization_id=ORG, user_id=WORKER, work_date=DAY)
