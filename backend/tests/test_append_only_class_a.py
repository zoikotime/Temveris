"""
Database-level append-only enforcement — Owner Decision 1A.
===========================================================

Ruling M / D18-F1: ORM listeners are bypassed by SQLAlchemy Core UPDATE, bulk
``query.update()``, ORM ``delete()`` and Core DELETE. Only the database can
refuse a statement the ORM never sees.

These tests exercise the triggers BELOW the ORM. Every mutation attempt here
uses Core SQL, because an ORM test would be stopped by the listener and prove
nothing about the trigger. The ORM guards are asserted separately, so a
regression in either layer is attributable to that layer.

WHAT IS AND IS NOT COVERED
--------------------------
Four tables receive enforcement (Class A — no authorized mutation at all):

    work_state_interval · leave_input_snapshot · config_value ·
    clock_session_boundary_event

EIGHT must NOT, and that is asserted just as hard:

    work_day_ledger, eco_day_record, break_classification_assertion and
    eco_classification_authority each have ONE authorized NULL -> value
    transition (Decision 2C declined triggers for them);

    eco_recipient_designation and eco_view_grant are revoked BY DELETE;
    rollout_control transitions BY UPDATE under optimistic concurrency;

    config_version was AUTHORIZED as a fifth enforced table and then
    disqualified by verification: ``note`` is writable by design, and
    ``captured_by_user_id`` is SET NULL, so deleting a user UPDATEs the row
    from the foreign key itself. Both are asserted below, because they are the
    reasons the trigger is absent.

A trigger on any of those would break the mechanism it was meant to protect,
so the negative assertions are the more important half of this file.
"""
from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

import database.models as dbm
from database.database import Base

from app.clock_session_boundary.append_only_ddl import (
    BOUNDARY_EVENT_TABLE, boundary_append_only_statements,
)
from app.config_versioning.append_only_ddl import (
    VALUE_TABLE, value_append_only_statements,
)
from app.eval_day.append_only_ddl import (
    SNAPSHOT_TABLE, snapshot_append_only_statements,
)
from app.work_ledger.append_only_ddl import (
    INTERVAL_TABLE, append_only_statements as interval_statements,
)

UTC = dt.timezone.utc
BACKEND = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = (BACKEND / "alembic" / "versions"
             / "e4b7d9c2a516_class_a_append_only_triggers.py")

ORG = 9810
OTHER_ORG = 9820
WORKER = 9811

#: The five tables Decision 1A authorized, each with the statement factory that
#: is the ONE definition of its DDL.
ENFORCED = {
    INTERVAL_TABLE: interval_statements,
    SNAPSHOT_TABLE: snapshot_append_only_statements,
    VALUE_TABLE: value_append_only_statements,
    BOUNDARY_EVENT_TABLE: boundary_append_only_statements,
}

#: One real, non-key column per enforced table for the UPDATE probe. Named per
#: table because the five do not share a column: the boundary event is keyed on
#: a session, not an organization.
UPDATE_PROBE = {
    INTERVAL_TABLE: ("duration_seconds", 1),
    SNAPSHOT_TABLE: ("overlap_seconds", 1),
    VALUE_TABLE: ("value_minutes", 1),
    BOUNDARY_EVENT_TABLE: ("actor_label", "someone.else@test"),
}

#: Tables that must NOT be enforced, each with the reason it must stay mutable.
EXCLUDED = {
    "work_day_ledger": "superseded_by NULL -> value links the §23 chain",
    "eco_day_record": "superseded_by NULL -> value, authorized once",
    "break_classification_assertion": "superseded_by NULL -> value",
    "eco_classification_authority": "revoked_at_utc NULL -> value",
    "eco_recipient_designation": "DELETE is revocation",
    "eco_view_grant": "DELETE is revocation",
    "rollout_control": "UPDATE is the durable rollout transition",
    "config_version": "note is writable; captured_by_user_id is SET NULL",
}


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def ao_engine():
    """A private engine carrying the whole ECO schema, triggers included."""
    for module in ("break_classification", "break_treatment",
                   "clock_session_boundary", "config_versioning",
                   "eco_day_alert", "eco_ledger_incident", "eval_day",
                   "eval_event", "alert", "notification_platform",
                   "policy_version", "recipient_authority", "rollout",
                   "eco_view_authority", "work_ledger", "work_ledger_producer"):
        try:
            __import__(f"app.{module}.models")
        except ModuleNotFoundError:
            __import__(f"app.{module}")
    import app.eval_day.leave_snapshot  # noqa: F401

    eng = sa.create_engine("sqlite:///:memory:", future=True)

    @sa_event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(ao_engine):
    from app import _tz
    _tz.invalidate_tz_cache()
    with SaSession(ao_engine, expire_on_commit=False) as s:
        s.add(dbm.Organization(id=ORG, name="AO Org"))
        s.add(dbm.Organization(id=OTHER_ORG, name="Other Org"))
        s.add(dbm.User(id=WORKER, name="W", email="w.ao@example.test",
                       organization_id=ORG, role="employee"))
        s.flush()
        # The boundary event FKs a real session, so one exists for every test.
        for sid in (1, 77):
            s.add(dbm.ClockSession(
                id=sid, user_id=WORKER, organization_id=ORG,
                clock_in_time=dt.datetime(2026, 9, 7, 9, tzinfo=UTC),
                clock_out_time=dt.datetime(2026, 9, 7, 17, tzinfo=UTC),
                total_seconds=1, break_seconds=0, offline_seconds=0,
                is_active=False))
        s.commit()
        yield s
    _tz.invalidate_tz_cache()


def _installed_triggers(db, table):
    return sorted(r[0] for r in db.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=:t"
    ), {"t": table}))


# ── Row builders. Core INSERT throughout, so nothing depends on the ORM. ─────
def _ledger(db, *, version=1, state="SEALED", org=ORG):
    r = db.execute(sa.text(
        "INSERT INTO work_day_ledger (organization_id, user_id, work_date, "
        "shift_attribution_rule, workweek_key, ledger_version, state, "
        "timezone_iana, utc_offset_seconds, is_working_day, "
        "working_day_source, created_by, created_at) VALUES (:o, :u, :d, "
        "'START_DATE', '2026-09-07', :v, :s, 'UTC', 0, 1, 'SCHEDULE', "
        "'TEST', :c) RETURNING id"
    ), {"o": org, "u": WORKER, "d": "2026-09-07 00:00:00", "v": version,
        "s": state, "c": "2026-09-07 00:00:00+00:00"}).scalar()
    db.commit()
    return r


def _interval(db, ledger_id, *, seq=1, org=ORG):
    r = db.execute(sa.text(
        "INSERT INTO work_state_interval (ledger_id, organization_id, seq, "
        "state, start_at_utc, end_at_utc, duration_seconds, provenance, "
        "created_at) VALUES (:l, :o, :q, 'WRK', :a, :b, 3600, 'WORKER', :c) "
        "RETURNING id"
    ), {"l": ledger_id, "o": org, "q": seq,
        "a": "2026-09-07 09:00:00+00:00", "b": "2026-09-07 10:00:00+00:00",
        "c": "2026-09-07 09:00:00+00:00"}
    ).scalar()
    db.commit()
    return r


_EFFECTIVE = {"n": 0}


def _config_version(db, *, key="cfg-1", org=ORG):
    # UNIQUE (organization_id, effective_from): one version per tenant per
    # date, so each helper call takes the next day.
    _EFFECTIVE["n"] += 1
    day = dt.date(2026, 1, 1) + dt.timedelta(days=_EFFECTIVE["n"])
    r = db.execute(sa.text(
        "INSERT INTO config_version (organization_id, version_key, "
        "effective_from, captured_at_utc, captured_by_label, content_hash, "
        "note) VALUES (:o, :k, :e, :t, 'TEST', :h, 'a note') "
        "RETURNING id"
    ), {"o": org, "k": key, "e": day.isoformat(),
        "t": "2026-01-01 00:00:00+00:00", "h": "0" * 64}).scalar()
    db.commit()
    return r


def _config_value(db, version_id, *, org=ORG):
    r = db.execute(sa.text(
        "INSERT INTO config_value (config_version_id, organization_id, tier, "
        "rank, key, value_minutes) VALUES (:v, :o, 'TENANT_FALLBACK', 6, "
        "'eco.required_minutes', 480) RETURNING id"
    ), {"v": version_id, "o": org}).scalar()
    db.commit()
    return r


def _snapshot(db, *, key="1" * 64, org=ORG):
    r = db.execute(sa.text(
        "INSERT INTO leave_input_snapshot (organization_id, user_id, "
        "work_date, snapshot_key, content_hash, overlap_seconds, "
        "contribution_count, captured_at_utc) VALUES (:o, :u, :d, :k, :k, "
        "0, 0, :t) RETURNING id"
    ), {"o": org, "u": WORKER, "d": "2026-09-07", "k": key,
        "t": "2026-09-07 00:00:00+00:00"}).scalar()
    db.commit()
    return r


def _boundary(db, *, session_id=1):
    r = db.execute(sa.text(
        "INSERT INTO clock_session_boundary_event (session_id, user_id, "
        "boundary, boundary_at, provenance, actor_label, written_at, "
        "sequence) VALUES (:s, :u, 'clock_in', :b, 'WORKER', 'w@t.test', "
        ":r, 1) RETURNING id"
    ), {"s": session_id, "u": WORKER, "b": "2026-09-07 09:00:00+00:00",
        "r": "2026-09-07 09:00:00+00:00"}).scalar()
    db.commit()
    return r


@pytest.fixture()
def rows(db):
    """One row in each of the four enforced tables."""
    ledger = _ledger(db)
    version = _config_version(db)
    return {
        INTERVAL_TABLE: _interval(db, ledger),
        SNAPSHOT_TABLE: _snapshot(db),
        VALUE_TABLE: _config_value(db, version),
        BOUNDARY_EVENT_TABLE: _boundary(db),
    }


# ═════════════════════════════════════════════════════════════════════════════
# 1. The five enforced tables: INSERT yes, UPDATE no, DELETE no
# ═════════════════════════════════════════════════════════════════════════════
class TestEnforcedTables:
    def test_every_enforced_table_carries_both_triggers(self, db):
        for table in ENFORCED:
            names = _installed_triggers(db, table)
            assert names == [f"{table}_no_delete", f"{table}_no_update"], table

    def test_insert_succeeds(self, rows):
        """The point of append-only is that appending still works."""
        assert all(isinstance(v, int) and v > 0 for v in rows.values()), rows

    @pytest.mark.parametrize("table", sorted(ENFORCED))
    def test_a_core_update_is_refused(self, db, rows, table):
        column, value = UPDATE_PROBE[table]
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                f"UPDATE {table} SET {column} = :v WHERE id = :i"  # noqa: S608
            ), {"v": value, "i": rows[table]})
        db.rollback()

    @pytest.mark.parametrize("table", sorted(ENFORCED))
    def test_a_core_delete_is_refused(self, db, rows, table):
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                f"DELETE FROM {table} WHERE id = :i"), {"i": rows[table]})  # noqa: S608
        db.rollback()

    @pytest.mark.parametrize("table", sorted(ENFORCED))
    def test_the_row_survives_both_attempts(self, db, rows, table):
        column, value = UPDATE_PROBE[table]
        for stmt in (f"UPDATE {table} SET {column} = :v WHERE id = :i",  # noqa: S608
                     f"DELETE FROM {table} WHERE id = :i"):  # noqa: S608
            with pytest.raises(IntegrityError):
                db.execute(sa.text(stmt), {"v": value, "i": rows[table]})
            db.rollback()
        still = db.execute(sa.text(
            f"SELECT count(*) FROM {table} WHERE id = :i"), {"i": rows[table]}  # noqa: S608
        ).scalar()
        assert still == 1

    @pytest.mark.parametrize("table", sorted(ENFORCED))
    def test_an_unqualified_bulk_delete_is_refused(self, db, rows, table):
        """The statement class Ruling M names: a Core bulk delete with no
        WHERE, which no ORM listener ever sees."""
        with pytest.raises(IntegrityError):
            db.execute(sa.text(f"DELETE FROM {table}"))  # noqa: S608
        db.rollback()

    @pytest.mark.parametrize("table", sorted(ENFORCED))
    def test_the_refusal_message_is_deterministic_and_names_the_table(
            self, db, rows, table):
        with pytest.raises(IntegrityError) as first:
            db.execute(sa.text(f"DELETE FROM {table} WHERE id = :i"),  # noqa: S608
                       {"i": rows[table]})
        db.rollback()
        with pytest.raises(IntegrityError) as second:
            db.execute(sa.text(f"DELETE FROM {table} WHERE id = :i"),  # noqa: S608
                       {"i": rows[table]})
        db.rollback()
        message = str(first.value)
        assert str(second.value) == message, "message is not deterministic"
        assert table in message, message
        assert "append-only" in message
        assert "owner decision 1A" in message

    def test_a_second_row_can_still_be_appended_after_a_refusal(self, db, rows):
        """A refused mutation must not poison the table for later appends."""
        with pytest.raises(IntegrityError):
            db.execute(sa.text("DELETE FROM leave_input_snapshot"))
        db.rollback()
        assert _snapshot(db, key="2" * 64) > 0


# ═════════════════════════════════════════════════════════════════════════════
# 2. The excluded tables: their lifecycles must still work
# ═════════════════════════════════════════════════════════════════════════════
class TestExcludedTables:
    @pytest.mark.parametrize("table", sorted(EXCLUDED))
    def test_no_trigger_was_installed(self, db, table):
        assert _installed_triggers(db, table) == [], EXCLUDED[table]

    def test_ledger_supersession_still_works(self, db):
        """§23 / INV-3. The exact write an unconditional trigger would break."""
        first = _ledger(db, version=1)
        second = _ledger(db, version=2)
        db.execute(sa.text(
            "UPDATE work_day_ledger SET superseded_by = :n WHERE id = :i"),
            {"n": second, "i": first})
        db.commit()
        assert db.execute(sa.text(
            "SELECT superseded_by FROM work_day_ledger WHERE id = :i"),
            {"i": first}).scalar() == second

    def test_a_new_ledger_version_can_still_be_written(self, db):
        """Supersession is by INSERT of a new version, and the interval trigger
        must not interfere with the intervals that belong to it."""
        v1 = _ledger(db, version=1)
        _interval(db, v1, seq=1)
        v2 = _ledger(db, version=2)
        _interval(db, v2, seq=1)
        assert db.execute(sa.text(
            "SELECT count(*) FROM work_state_interval")).scalar() == 2

    def test_rollout_transition_still_works(self, db):
        """OFF -> SHADOW -> ENABLED under the state_version token. A trigger
        here would make the rollout impossible to start."""
        from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2
        from app.rollout.models import RolloutControl

        row = RolloutControl(organization_id=ORG,
                             control_key=CONTROL_EARLY_CLOCK_OUT_V2,
                             state="OFF", state_version=1,
                             last_transition_by="test")
        db.add(row)
        db.commit()
        for target, version in (("SHADOW", 2), ("ENABLED", 3)):
            db.execute(sa.text(
                "UPDATE rollout_control SET state = :s, state_version = :v "
                "WHERE id = :i AND state_version = :prev"),
                {"s": target, "v": version, "i": row.id, "prev": version - 1})
            db.commit()
        assert db.execute(sa.text(
            "SELECT state, state_version FROM rollout_control WHERE id = :i"),
            {"i": row.id}).one() == ("ENABLED", 3)

    def test_optimistic_concurrency_still_rejects_a_stale_write(self, db):
        """The token still does its job: a write against a stale version
        matches no row. Enforcement must not have replaced this mechanism."""
        from app.rollout import CONTROL_EARLY_CLOCK_OUT_V2
        from app.rollout.models import RolloutControl

        row = RolloutControl(organization_id=ORG,
                             control_key=CONTROL_EARLY_CLOCK_OUT_V2,
                             state="SHADOW", state_version=5,
                             last_transition_by="test")
        db.add(row)
        db.commit()
        result = db.execute(sa.text(
            "UPDATE rollout_control SET state = 'ENABLED' "
            "WHERE id = :i AND state_version = 4"), {"i": row.id})
        db.commit()
        assert result.rowcount == 0
        assert db.execute(sa.text(
            "SELECT state FROM rollout_control WHERE id = :i"),
            {"i": row.id}).scalar() == "SHADOW"

    def test_a_recipient_designation_can_still_be_revoked_by_delete(self, db):
        from app.recipient_authority import EcoRecipientDesignation

        db.add(EcoRecipientDesignation(organization_id=ORG,
                                       audience="PRIMARY_ADMIN",
                                       user_id=WORKER))
        db.commit()
        db.execute(sa.delete(EcoRecipientDesignation))
        db.commit()
        assert db.query(EcoRecipientDesignation).count() == 0

    def test_a_view_grant_can_still_be_revoked_by_delete(self, db):
        from app.eco_view_authority.models import EcoViewGrant

        db.add(EcoViewGrant(organization_id=ORG, user_id=WORKER,
                            granted_by="test"))
        db.commit()
        db.execute(sa.delete(EcoViewGrant))
        db.commit()
        assert db.query(EcoViewGrant).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# 3. Tenant isolation and idempotency
# ═════════════════════════════════════════════════════════════════════════════
class TestIsolationAndRetry:
    def test_config_version_note_is_still_writable(self, db, rows):
        """The first reason config_version is NOT enforced.

        ``note`` is administrator prose, not configuration: outside the content
        hash, nothing resolves against it, and correcting a typo cannot change
        what any date resolves to. A trigger would have taken that away.
        """
        version = _config_version(db, key="note-test")
        db.execute(sa.text("UPDATE config_version SET note = :n WHERE id = :i"),
                   {"n": "corrected wording", "i": version})
        db.commit()
        assert db.execute(sa.text(
            "SELECT note FROM config_version WHERE id = :i"),
            {"i": version}).scalar() == "corrected wording"

    def test_deleting_a_user_still_nulls_the_captured_by_reference(self, db):
        """The second, and the one no AST scan could have found.

        ``captured_by_user_id`` is ``ondelete="SET NULL"``, so deleting a user
        issues a database-level UPDATE against config_version -- performed by
        the foreign key, not by any statement in the codebase. That SET NULL is
        Ruling M's mechanism for "removing a person must never remove
        configuration history"; an unconditional trigger refuses it and makes
        the user undeletable.
        """
        db.add(dbm.User(id=98100, name="C", email="c.ao@example.test",
                        organization_id=ORG, role="admin"))
        db.commit()
        version = db.execute(sa.text(
            "INSERT INTO config_version (organization_id, version_key, "
            "effective_from, captured_at_utc, captured_by_user_id, "
            "captured_by_label, content_hash) VALUES (:o, 'by-user', "
            "'2026-01-01', :t, 98100, 'C', :h) RETURNING id"),
            {"o": ORG, "t": "2026-01-01 00:00:00+00:00", "h": "0" * 64}).scalar()
        db.commit()

        db.execute(sa.text("DELETE FROM users WHERE id = 98100"))
        db.commit()

        row = db.execute(sa.text(
            "SELECT captured_by_user_id, captured_by_label FROM config_version "
            "WHERE id = :i"), {"i": version}).one()
        assert row == (None, "C"), "the label must survive, the id must null"

    def test_a_cross_tenant_update_is_refused_like_any_other(self, db, rows):
        """Enforcement is not tenant-aware and must not appear to be: a
        cross-tenant rewrite is refused because it is an UPDATE, which is a
        stronger guarantee than a tenant predicate would give."""
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "UPDATE config_value SET organization_id = :o WHERE id = :i"),
                {"o": OTHER_ORG, "i": rows[VALUE_TABLE]})
        db.rollback()
        assert db.execute(sa.text(
            "SELECT organization_id FROM config_value WHERE id = :i"),
            {"i": rows[VALUE_TABLE]}).scalar() == ORG

    def test_another_tenants_rows_are_untouched_by_a_refusal(self, db):
        mine = _config_value(db, _config_version(db, key="mine"))
        theirs = _config_value(
            db, _config_version(db, key="theirs", org=OTHER_ORG),
            org=OTHER_ORG)
        with pytest.raises(IntegrityError):
            db.execute(sa.text("DELETE FROM config_value"))
        db.rollback()
        assert db.execute(sa.text(
            "SELECT count(*) FROM config_value WHERE id IN (:a, :b)"),
            {"a": mine, "b": theirs}).scalar() == 2

    def test_an_idempotent_retry_still_appends_nothing_twice(self, db):
        """A retry that re-inserts an identical row is stopped by the natural
        key, NOT by the append-only trigger — the two mechanisms are separate
        and both must survive."""
        version = _config_version(db, key="dup")
        effective = db.execute(sa.text(
            "SELECT effective_from FROM config_version WHERE id = :i"),
            {"i": version}).scalar()
        with pytest.raises(IntegrityError):
            db.execute(sa.text(
                "INSERT INTO config_version (organization_id, version_key, "
                "effective_from, captured_at_utc, captured_by_label, "
                "content_hash) VALUES (:o, 'dup', :e, :t, 'TEST', :h)"),
                {"o": ORG, "e": effective, "t": "2026-01-01 00:00:00+00:00",
                 "h": "0" * 64})
        db.rollback()
        assert db.execute(sa.text(
            "SELECT count(*) FROM config_version WHERE effective_from = :e"),
            {"e": effective}).scalar() == 1


# ═════════════════════════════════════════════════════════════════════════════
# 4. The ORM guards are still there
# ═════════════════════════════════════════════════════════════════════════════
class TestOrmGuardsIntact:
    def test_the_snapshot_orm_guard_still_raises_its_typed_error(self, db):
        """Two layers, two errors. The ORM raises something the application can
        reason about; the trigger raises a database error. Neither replaces the
        other."""
        from app.eval_day.leave_snapshot import (
            LeaveInputSnapshot, LeaveSnapshotImmutable,
        )
        _snapshot(db, key="3" * 64)
        row = db.query(LeaveInputSnapshot).filter(
            LeaveInputSnapshot.snapshot_key == "3" * 64).one()
        row.overlap_seconds = 999
        with pytest.raises(LeaveSnapshotImmutable):
            db.flush()
        db.rollback()

    def test_the_boundary_orm_guard_still_raises(self, db):
        from app.clock_session_boundary.models import (
            BoundaryEventImmutable, ClockSessionBoundaryEvent,
        )
        _boundary(db, session_id=77)
        row = db.query(ClockSessionBoundaryEvent).filter(
            ClockSessionBoundaryEvent.session_id == 77).one()
        row.provenance = "ADMIN_OVERRIDE"
        with pytest.raises(BoundaryEventImmutable):
            db.flush()
        db.rollback()

    def test_the_widened_ledger_frozen_set_is_in_force(self):
        """Owner Decision 2C: eleven columns joined the frozen tuple."""
        from app.work_ledger.models import _FROZEN_LEDGER_EVIDENCE as F

        for field in ("organization_id", "user_id", "work_date",
                      "ledger_version", "supersedes", "timezone_iana",
                      "utc_offset_seconds", "is_working_day",
                      "working_day_source", "created_at", "created_by"):
            assert field in F, field
        # And the four the owner reserved stay OUT.
        for field in ("state", "sealed_at", "integrity_fault_reason",
                      "superseded_by"):
            assert field not in F, field


# ═════════════════════════════════════════════════════════════════════════════
# 5. Dialects and the migration
# ═════════════════════════════════════════════════════════════════════════════
class TestDialectsAndMigration:
    @pytest.mark.parametrize("table,factory", sorted(ENFORCED.items()))
    def test_both_supported_dialects_carry_statements(self, table, factory):
        for dialect in ("sqlite", "postgresql"):
            statements = factory(dialect)
            assert statements, (table, dialect)
            joined = " ".join(statements).upper()
            assert "UPDATE" in joined and "DELETE" in joined
            assert table.upper() in joined
        # An unsupported dialect gets NO silent partial protection.
        assert factory("mysql") == ()

    def test_the_postgres_statements_compile_as_one_function_per_table(self):
        """PostgreSQL is unreachable here, so its statements are verified by
        structure rather than execution — the standing position every other
        append-only module states."""
        for table, factory in ENFORCED.items():
            pg = factory("postgresql")
            assert len(pg) == 3, table
            assert pg[0].startswith("CREATE OR REPLACE FUNCTION")
            assert f"{table}_append_only" in pg[0]
            assert "plpgsql" in pg[0]
            assert all("FOR EACH ROW EXECUTE FUNCTION" in s for s in pg[1:])

    def test_the_migration_emits_what_the_models_attach(self):
        """One definition, two emitters."""
        import ast
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for package in ("app.work_ledger", "app.eval_day",
                        "app.config_versioning", "app.clock_session_boundary"):
            assert f"{package}.append_only_ddl" in imported, package

    def test_the_migration_is_additive_and_rewrites_no_row(self):
        import ast
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "upgrade")
        called = {n.func.attr for n in ast.walk(upgrade)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for forbidden in ("create_table", "drop_table", "add_column",
                          "drop_column", "alter_column", "bulk_insert"):
            assert forbidden not in called, forbidden
        assert "execute" in called

        # The migration executes only what the DDL modules hand it. Asserted by
        # AST, not by text: the emitted statements are BEFORE UPDATE / BEFORE
        # DELETE triggers, so the words appear legitimately, and the docstring
        # quotes the audit that found a Core UPDATE. What must be true is that
        # no SQL LITERAL lives in this file at all.
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        sql_literals = [v for v in literals
                        if v.strip().upper().startswith(
                            ("INSERT", "UPDATE", "DELETE", "SELECT", "CREATE",
                             "DROP", "ALTER"))]
        assert sql_literals == [], sql_literals

        # And it opens no database of its own.
        imported = {n.module for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) and n.module}
        for banned in ("sqlalchemy.orm", "database.database"):
            assert banned not in imported, banned
        assert "create_engine" not in {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree) if isinstance(n, ast.Call)}

    def test_the_migration_touches_no_excluded_table(self):
        body = MIGRATION.read_text(encoding="utf-8")
        # Named in prose to explain the exclusion; never imported as DDL.
        import ast
        tree = ast.parse(body)
        names = {a.name for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) for a in n.names}
        for excluded in ("rollout", "recipient_authority", "eco_view_authority",
                         "break_classification"):
            assert not any(excluded in n for n in names), excluded


# ═════════════════════════════════════════════════════════════════════════════
# 6. Owner Decision 2 — the ledger guard survives an expired instance
# ═════════════════════════════════════════════════════════════════════════════
# The guard used to compare against ``history.deleted`` and skip a field when
# that was empty. On an EXPIRED instance it always is, and ``SessionLocal`` uses
# SQLAlchemy's default ``expire_on_commit=True`` — so in production the whole
# frozen tuple was inert, and only suites that set ``expire_on_commit=False``
# ever saw it fire. These tests run EVERY case under BOTH settings, because a
# suite that pinned one of them is exactly how the hole stayed open.

#: One forbidden write per frozen field. ``superseded_by`` is absent on purpose
#: — it is the authorized transition and has its own test below.
FROZEN_WRITES = [
    ("organization_id", OTHER_ORG),
    ("user_id", 98111),
    ("work_date", dt.datetime(2026, 1, 2)),
    ("ledger_version", 9),
    ("supersedes", 4242),
    ("workweek_key", "2026-02-02"),
    ("shift_attribution_rule", "END_DATE"),
    ("timezone_iana", "Europe/London"),
    ("utc_offset_seconds", 3600),
    ("is_working_day", False),
    ("working_day_source", "MANUAL"),
    ("created_by", "SOMEONE_ELSE"),
    ("created_at", dt.datetime(2020, 1, 1, tzinfo=UTC)),
    ("departure_at", dt.datetime(2026, 1, 1, 18, tzinfo=UTC)),
    ("departure_provenance", "ADMIN_OVERRIDE"),
    ("source_fingerprint", "deadbeef"),
    ("scheduled_end_source", "manual"),
]


def _ledger_engine():
    import app.work_ledger.models  # noqa: F401
    eng = sa.create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _seed_ledger(session, *, state="SEALED", version=1, org=ORG):
    """One persisted, committed ledger row, built through the ORM."""
    from app.work_ledger.models import WorkDayLedger

    if session.get(dbm.Organization, org) is None:
        session.add(dbm.Organization(id=org, name=f"o{org}"))
    if session.get(dbm.Organization, OTHER_ORG) is None:
        session.add(dbm.Organization(id=OTHER_ORG, name="other"))
    session.flush()
    for uid in (WORKER, 98111):
        if session.get(dbm.User, uid) is None:
            session.add(dbm.User(id=uid, name="W", email=f"{uid}@ao.test",
                                 organization_id=org, role="employee"))
    session.flush()
    row = WorkDayLedger(
        organization_id=org, user_id=WORKER,
        work_date=dt.datetime(2026, 1, 1), workweek_key="2026-01-01",
        ledger_version=version, state=state, timezone_iana="UTC",
        utc_offset_seconds=0, is_working_day=True,
        working_day_source="SCHEDULE", created_by="PRODUCER",
        created_at=dt.datetime(2026, 1, 1, tzinfo=UTC), supersedes=None)
    session.add(row)
    session.commit()
    return row


@pytest.mark.parametrize("expire_on_commit", [False, True],
                         ids=["loaded", "expired"])
class TestLedgerFrozenFieldsUnderBothExpireModes:
    def test_every_frozen_field_is_refused(self, expire_on_commit):
        """The whole point of Owner Decision 2: identical protection either way.

        A fresh engine and session per field, because a rollback expires the
        instance and would silently turn the 'loaded' case into the 'expired'
        one after the first assertion.
        """
        from app.work_ledger.states import ProvenanceImmutable

        accepted = []
        for field, value in FROZEN_WRITES:
            with SaSession(_ledger_engine(),
                           expire_on_commit=expire_on_commit) as session:
                row = _seed_ledger(session)
                setattr(row, field, value)
                try:
                    session.flush()
                    accepted.append(field)
                except ProvenanceImmutable:
                    pass
        assert accepted == [], accepted

    def test_the_row_is_unchanged_after_a_refusal(self, expire_on_commit):
        from app.work_ledger.models import WorkDayLedger
        from app.work_ledger.states import ProvenanceImmutable

        with SaSession(_ledger_engine(),
                       expire_on_commit=expire_on_commit) as session:
            row = _seed_ledger(session)
            row.timezone_iana = "Europe/London"
            with pytest.raises(ProvenanceImmutable):
                session.flush()
            session.rollback()
            assert session.get(WorkDayLedger, row.id).timezone_iana == "UTC"

    def test_supersession_is_still_allowed(self, expire_on_commit):
        """Invariant 7. The one authorized transition, in both modes."""
        from app.work_ledger.models import WorkDayLedger

        with SaSession(_ledger_engine(),
                       expire_on_commit=expire_on_commit) as session:
            first = _seed_ledger(session, version=1)
            second = WorkDayLedger(
                organization_id=ORG, user_id=WORKER,
                work_date=dt.datetime(2026, 1, 1), workweek_key="2026-01-01",
                ledger_version=2, state="SEALED", timezone_iana="UTC",
                utc_offset_seconds=0, is_working_day=True,
                working_day_source="SCHEDULE", created_by="PRODUCER",
                supersedes=first.id)
            session.add(second)
            session.flush()
            first.superseded_by = second.id
            session.flush()
            session.commit()
            assert session.get(
                WorkDayLedger, first.id).superseded_by == second.id

    def test_an_open_row_may_still_complete_a_first_write(self,
                                                          expire_on_commit):
        """The two-strictness rule is preserved: NULL -> value on an OPEN row is
        how a first write completes, and is still refused once sealed."""
        with SaSession(_ledger_engine(),
                       expire_on_commit=expire_on_commit) as session:
            row = _seed_ledger(session, state="OPEN")
            row.source_fingerprint = "abc123"
            session.flush()
            session.commit()
            assert row.source_fingerprint == "abc123"

    def test_a_no_op_rewrite_is_not_a_change(self, expire_on_commit):
        """Writing the identical value must not be refused: it changes nothing,
        and refusing it would make an idempotent retry fail."""
        with SaSession(_ledger_engine(),
                       expire_on_commit=expire_on_commit) as session:
            row = _seed_ledger(session)
            row.timezone_iana = "UTC"
            session.flush()
            session.commit()


class TestLedgerGuardScope:
    def test_the_guard_reads_the_stored_value_not_the_session(self):
        """Structural: the fix is the flush connection, not a bigger history.

        Asserted so a future edit cannot quietly go back to ``history.deleted``,
        which is what made the tuple inert.
        """
        import ast
        source = (BACKEND / "app" / "work_ledger"
                  / "models.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        guard = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "_reject_frozen_ledger_evidence_update")
        called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                  for c in ast.walk(guard) if isinstance(c, ast.Call)}
        assert "load_history" in called
        assert "select" in called
        assert "execute" in called
        attrs = {n.attr for n in ast.walk(guard) if isinstance(n, ast.Attribute)}
        assert "deleted" not in attrs, (
            "the guard is reading history.deleted again; that is empty on an "
            "expired instance and is exactly what Owner Decision 2 replaced")

    def test_work_day_ledger_still_has_no_database_trigger(self, db):
        """Invariant 10: no new conditional trigger pattern was introduced."""
        assert _installed_triggers(db, "work_day_ledger") == []
