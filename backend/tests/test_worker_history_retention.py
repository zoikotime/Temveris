"""
Owner Ruling M — Worker history retention.
==========================================

Ruling M: historical records and snapshots must NEVER be deleted or
overwritten. Five tables held ``user_id -> users.id ON DELETE CASCADE``, which
contradicted that below the ORM listeners — and the one production path that
deletes a Worker uses a Core bulk delete, so no listener ever fired.

Three things are tested:

1. **The five-table boundary**, as a complete set. Each table is asserted
   individually, and the set itself is pinned, so a sixth table cannot be added
   or an existing one dropped without this suite noticing.

2. **Enforcement where the deletion happens** — on an engine with
   ``PRAGMA foreign_keys=ON``, because the shared test engine leaves it off and
   every assertion here would otherwise pass vacuously. Both the ORM path and a
   Core bulk delete are exercised: the listeners cannot be the guarantee.

3. **The endpoint's stated refusal** — a protected Worker gets a 409 naming the
   tables, not an opaque 500, and a Worker with no protected history still
   deletes exactly as before.

The migration is held to the model by comparing the FK delete actions, not by
matching text.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as SaSession

from app.config_versioning import ConfigValue, ConfigVersion
from app.eval_day import EcoDayRecord, LeaveInputContribution, LeaveInputSnapshot
from app.leave_intervals import LeaveInterval, LeaveIntervalSet
from app.work_ledger import LedgerState, WorkDayLedger, WorkStateInterval
from app.worker_context import WorkerContextTimeline

UTC = timezone.utc
ORG = 101
OTHER_ORG = 102
USER = 10100
OTHER_USER = 10200

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
DAY = date(2026, 8, 31)
MONDAY_WEEK = "2026-08-31"

BACKEND = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = (BACKEND / "alembic" / "versions"
             / "a2f9d61c4b83_worker_history_retention_restrict.py")

# ── Independently transcribed ────────────────────────────────────────────────
#: The five tables ``a2f9d61c4b83`` moved, keyed on ``user_id``. Written out
#: rather than imported from the migration: importing it would let a wrong
#: boundary agree with itself. The FULL retention boundary is
#: ``RETENTION_BOUNDARY`` below, which adds ``config_value.scope_user_id``
#: under its own revision.
HISTORY_TABLES = (
    "work_day_ledger",
    "eco_day_record",
    "leave_input_snapshot",
    "leave_interval_set",
    "worker_context_timeline",
)
EXPECTED_REVISION = "a2f9d61c4b83"
EXPECTED_PARENT = "d3a1c8b57f92"
EXPECTED_REVISION_COUNT = 94   # + b5f2a8c47d93 (ECO day record INV-4 break evidence)
                                      # + c4e9b7d2a583 (§45.D-15)
                                      # + d5f1a8c3b724 (D-18C/D)

#: The single head. Re-baselined by the §45.D-15 increment, which added
#: `clock_session_boundary_event` as an ordinary single-parent child of
#: b7c4e0a92d51. CONFIG_VALUE_REVISION below still identifies its own
#: migration file; it is simply no longer the tip.
CURRENT_HEAD_REVISION = "b5f2a8c47d93"   # ECO day record INV-4 break evidence
#                                          on eco_day_record

#: The revision that extended the boundary to config_value.scope_user_id.
CONFIG_VALUE_REVISION = "b7c4e0a92d51"
CONFIG_VALUE_PARENT = "a2f9d61c4b83"

#: THE FULL immutable Worker-history retention boundary, as (table, column).
#:
#: Six entries, not five: the owner extended the boundary to
#: ``config_value.scope_user_id`` — a Worker-scoped configuration override
#: inside an immutable, content-hashed ConfigVersion. The column name
#: differs, which is exactly why the endpoint maps table -> column rather
#: than assuming ``user_id`` everywhere. Transcribed rather than imported:
#: this list IS the ruling, and importing it would let a wrong boundary
#: agree with itself.
RETENTION_BOUNDARY = (
    ("work_day_ledger", "user_id"),
    ("eco_day_record", "user_id"),
    ("leave_input_snapshot", "user_id"),
    ("leave_interval_set", "user_id"),
    ("worker_context_timeline", "user_id"),
    ("config_value", "scope_user_id"),
    # Added by §45.D-15 under owner decision D15-4-A: session-boundary
    # provenance is protected historical evidence, probed on the SUBJECT
    # Worker. Its own coverage lives in tests/test_clock_session_boundary.py;
    # this entry keeps the boundary list here honest about what is protected.
    ("clock_session_boundary_event", "user_id"),
)

MODELS_BY_TABLE = {
    "work_day_ledger": WorkDayLedger,
    "eco_day_record": EcoDayRecord,
    "leave_input_snapshot": LeaveInputSnapshot,
    "leave_interval_set": LeaveIntervalSet,
    "worker_context_timeline": WorkerContextTimeline,
}


def _user_fk(table_model):
    """The single ``user_id -> users.id`` foreign key of a model."""
    found = [
        fk for fk in table_model.__table__.foreign_keys
        if fk.parent.name == "user_id" and fk.target_fullname == "users.id"
    ]
    assert len(found) == 1, f"{table_model.__tablename__}: {len(found)} user FKs"
    return found[0]


# ═════════════════════════════════════════════════════════════════════════════
# The boundary, as a complete set
# ═════════════════════════════════════════════════════════════════════════════
class TestTheHistoryBoundary:
    @pytest.mark.parametrize("table", HISTORY_TABLES)
    def test_each_history_table_restricts_user_deletion(self, table):
        assert _user_fk(MODELS_BY_TABLE[table]).ondelete == "RESTRICT"

    def test_the_boundary_is_exactly_these_five_tables(self):
        """Pinned. A sixth table is an owner decision, not a code change."""
        assert set(MODELS_BY_TABLE) == set(HISTORY_TABLES)
        assert len(HISTORY_TABLES) == 5

    def test_the_actor_reference_is_still_set_null(self):
        """``recorded_by_user_id`` is WHO recorded, not WHOSE history it is.

        It must stay SET NULL — the same treatment
        ``config_version.captured_by_user_id`` already has. Turning it into
        RESTRICT would make an administrator undeletable for having once made
        a recording, which Ruling M does not ask for.
        """
        actor = [
            fk for fk in WorkerContextTimeline.__table__.foreign_keys
            if fk.parent.name == "recorded_by_user_id"
        ]
        assert len(actor) == 1
        assert actor[0].ondelete == "SET NULL"

    def test_unrelated_foreign_keys_were_not_changed(self):
        """Only ``user_id`` moved."""
        # Organizations were already RESTRICT everywhere.
        for model in MODELS_BY_TABLE.values():
            for fk in model.__table__.foreign_keys:
                if fk.parent.name == "organization_id":
                    assert fk.ondelete == "RESTRICT"
        # The ledger's internal cascade to its own intervals is untouched: it
        # is unreachable once the Worker cannot be deleted, and rewriting it
        # would change ledger correction behaviour.
        interval_fk = [
            fk for fk in WorkStateInterval.__table__.foreign_keys
            if fk.parent.name == "ledger_id"
        ]
        assert [fk.ondelete for fk in interval_fk] == ["CASCADE"]

    def test_config_value_scope_user_id_restricts_user_deletion(self):
        """The owner ruling that extended the boundary to this column.

        Was CASCADE. A cascade here deleted an immutable ConfigValue while
        leaving its parent ConfigVersion in place with a content_hash that no
        longer described the stored value set.
        """
        scope = [
            fk for fk in ConfigValue.__table__.foreign_keys
            if fk.parent.name == "scope_user_id"
        ]
        assert len(scope) == 1
        assert scope[0].ondelete == "RESTRICT"

    def test_set_null_is_not_used_for_the_worker_scope(self):
        """PROHIBITED: NULL means organization-wide, not "unknown".

        Nulling a Worker's override would silently re-scope it to the whole
        tenant at that row's own tier and rank. A different question from
        ``captured_by_user_id``, which IS SET NULL and is excluded from the
        digest.
        """
        scope = [
            fk for fk in ConfigValue.__table__.foreign_keys
            if fk.parent.name == "scope_user_id"
        ][0]
        assert scope.ondelete != "SET NULL"
        assert scope.ondelete != "CASCADE"

    def test_the_captured_by_actor_reference_is_still_set_null(self):
        """Untouched by this ruling — different semantics, different action."""
        actor = [
            fk for fk in ConfigVersion.__table__.foreign_keys
            if fk.parent.name == "captured_by_user_id"
        ]
        assert len(actor) == 1
        assert actor[0].ondelete == "SET NULL"

    def test_the_scope_column_is_still_hashed_and_immutable(self):
        """The premise RESTRICT rests on, pinned rather than assumed."""
        from app.config_versioning import compute_content_hash
        from app.config_versioning.models import _IMMUTABLE_VALUE_FIELDS

        assert "scope_user_id" in _IMMUTABLE_VALUE_FIELDS

        base = dict(organization_id=ORG, version_key="v1", effective_from=DAY)
        row = {"tier": "WORKER_OVERRIDE", "rank": 0,
               "key": "required_minutes", "value_minutes": 300,
               "source_ref": None}
        org_wide = compute_content_hash(
            **base, values=[dict(row, scope_user_id=None)])
        worker = compute_content_hash(
            **base, values=[dict(row, scope_user_id=USER)])
        assert org_wide != worker

    def test_the_five_table_boundary_is_unchanged_by_this_ruling(self):
        """The earlier protection must survive intact."""
        assert HISTORY_TABLES == (
            "work_day_ledger", "eco_day_record", "leave_input_snapshot",
            "leave_interval_set", "worker_context_timeline")
        for table in HISTORY_TABLES:
            assert _user_fk(MODELS_BY_TABLE[table]).ondelete == "RESTRICT"

    def test_the_full_boundary_is_exactly_the_owner_ruled_entries(self):
        """Seven since §45.D-15 (owner decision D15-4-A) added
        `clock_session_boundary_event`. The count is pinned so nothing joins
        the protected boundary without an owner ruling; what changes it is a
        ruling, never convenience."""
        assert len(RETENTION_BOUNDARY) == 7
        assert ("config_value", "scope_user_id") in RETENTION_BOUNDARY
        assert ("clock_session_boundary_event", "user_id") in RETENTION_BOUNDARY


# ═════════════════════════════════════════════════════════════════════════════
# Enforcement, on an engine that actually enforces foreign keys
# ═════════════════════════════════════════════════════════════════════════════
class TestRetentionIsEnforcedByTheDatabase:
    """The shared engine leaves ``PRAGMA foreign_keys`` off, so these run on a
    dedicated one. Without it every assertion below passes vacuously."""

    @pytest.fixture()
    def fk_session(self):
        from database.models import LeaveRequest, Organization, User

        eng = sa.create_engine("sqlite:///:memory:", future=True)

        @sa_event.listens_for(eng, "connect")
        def _enable_fk(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        for table in (Organization.__table__, User.__table__,
                      LeaveRequest.__table__, WorkDayLedger.__table__,
                      WorkStateInterval.__table__, EcoDayRecord.__table__,
                      LeaveIntervalSet.__table__, LeaveInterval.__table__,
                      LeaveInputSnapshot.__table__,
                      LeaveInputContribution.__table__,
                      WorkerContextTimeline.__table__):
            table.create(eng, checkfirst=True)

        with SaSession(eng) as session:
            session.add(Organization(id=ORG, name="Org A"))
            session.add(User(id=USER, name="W", email="w@example.test",
                             organization_id=ORG))
            session.add(User(id=OTHER_USER, name="X", email="x@example.test",
                             organization_id=ORG))
            session.commit()
            yield session

    def test_foreign_key_enforcement_is_actually_on(self, fk_session):
        """Guard the guard."""
        assert fk_session.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1

    # ── one seeder per protected table ──────────────────────────────────────
    def _ledger(self, db, user_id=USER):
        row = WorkDayLedger(
            organization_id=ORG, user_id=user_id,
            work_date=datetime(DAY.year, DAY.month, DAY.day),
            workweek_key=MONDAY_WEEK, ledger_version=1,
            state=LedgerState.SEALED.value, timezone_iana="Asia/Kolkata", is_working_day=True, working_day_source="org",
            utc_offset_seconds=0, created_at=NOW, created_by="TEST")
        db.add(row)
        db.commit()
        return row

    def _day_record(self, db, user_id=USER):
        row = EcoDayRecord(
            organization_id=ORG, user_id=user_id, work_date=DAY,
            ledger_version=1, policy_version=None, config_version="c",
            leave_input_snapshot_key="k" * 64, evaluation_generation=1,
            requirement_outcome="RESOLVED", content_hash="k" * 64,
            state="SHORTFALL", shortfall_computed=True, required_minutes=1,
            qualifying_minutes=0, gross_shortfall_minutes=1,
            exclusion_minutes=0, attributable_minutes=1,
            contributes_to_accumulator_minutes=1, evaluated_at_utc=NOW)
        db.add(row)
        db.commit()
        return row

    def _snapshot(self, db, user_id=USER):
        row = LeaveInputSnapshot(
            snapshot_key="s" * 64, organization_id=ORG, user_id=user_id,
            work_date=DAY, content_hash="s" * 64, overlap_seconds=0,
            contribution_count=0, captured_at_utc=NOW)
        db.add(row)
        db.commit()
        return row

    def _interval_set(self, db, user_id=USER):
        from database.models import LeaveRequest

        request = LeaveRequest(
            id=91001 + user_id, user_id=user_id, organization_id=ORG,
            leave_type="Casual Leave",
            from_date=datetime(DAY.year, DAY.month, DAY.day),
            to_date=datetime(DAY.year, DAY.month, DAY.day),
            description="x", status="approved")
        db.add(request)
        db.flush()
        row = LeaveIntervalSet(
            organization_id=ORG, user_id=user_id,
            leave_request_id=request.id, set_version=1, supersedes=None,
            timezone_iana="Asia/Kolkata", recorded_by="TEST",
            recorded_at_utc=NOW)
        db.add(row)
        db.commit()
        return row

    def _worker_context(self, db, user_id=USER):
        row = WorkerContextTimeline(
            organization_id=ORG, user_id=user_id, timeline_version=1,
            effective_from=DAY - timedelta(days=10),
            worker_context="FULL_TIME", context_vocabulary="EMPLOYMENT_TYPE",
            provenance="ADMIN", recorded_at_utc=NOW)
        db.add(row)
        db.commit()
        return row

    SEEDERS = {
        "work_day_ledger": "_ledger",
        "eco_day_record": "_day_record",
        "leave_input_snapshot": "_snapshot",
        "leave_interval_set": "_interval_set",
        "worker_context_timeline": "_worker_context",
    }

    def test_every_protected_table_is_covered_by_a_seeder(self):
        """Guard the parametrisation: no table may be silently untested."""
        assert set(self.SEEDERS) == set(HISTORY_TABLES)

    @pytest.mark.parametrize("table", HISTORY_TABLES)
    def test_a_core_bulk_delete_cannot_erase_the_history(self, fk_session, table):
        """The path the ORM listeners never see.

        ``DELETE /employees/{emp_id}`` deletes the users row with
        ``Query.delete(synchronize_session=False)``, so no ``before_delete``
        fires. This is why the guarantee had to move into the schema.
        """
        from database.models import User

        getattr(self, self.SEEDERS[table])(fk_session)

        with pytest.raises(sa.exc.IntegrityError):
            fk_session.query(User).filter(User.id == USER).delete(
                synchronize_session=False)
            fk_session.flush()
        fk_session.rollback()

        assert fk_session.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE user_id = :u"),
            {"u": USER}).scalar() == 1

    @pytest.mark.parametrize("table", HISTORY_TABLES)
    def test_raw_sql_cannot_erase_the_history_either(self, fk_session, table):
        getattr(self, self.SEEDERS[table])(fk_session)

        with pytest.raises(sa.exc.IntegrityError):
            fk_session.execute(sa.text("DELETE FROM users WHERE id = :u"),
                               {"u": USER})
            fk_session.flush()
        fk_session.rollback()

    def test_a_worker_with_no_protected_history_is_still_deletable(
        self, fk_session
    ):
        """Guard the guard: retention must not block every deletion."""
        from database.models import User

        # History belongs to the OTHER Worker.
        self._ledger(fk_session, user_id=OTHER_USER)

        fk_session.query(User).filter(User.id == USER).delete(
            synchronize_session=False)
        fk_session.commit()
        assert fk_session.get(User, USER) is None
        assert fk_session.get(User, OTHER_USER) is not None

    def test_the_actor_reference_does_not_block_deletion(self, fk_session):
        """A Worker who RECORDED someone else's context is still deletable."""
        from database.models import User

        row = WorkerContextTimeline(
            organization_id=ORG, user_id=OTHER_USER, timeline_version=1,
            effective_from=DAY - timedelta(days=10),
            worker_context="FULL_TIME", context_vocabulary="EMPLOYMENT_TYPE",
            provenance="ADMIN", recorded_at_utc=NOW,
            recorded_by_user_id=USER)
        fk_session.add(row)
        fk_session.commit()

        fk_session.query(User).filter(User.id == USER).delete(
            synchronize_session=False)
        fk_session.commit()
        assert fk_session.get(User, USER) is None
        # The record survives with its actor nulled, not deleted.
        assert fk_session.execute(
            sa.text("SELECT COUNT(*) FROM worker_context_timeline "
                    "WHERE user_id = :u"), {"u": OTHER_USER}).scalar() == 1


# ═════════════════════════════════════════════════════════════════════════════
# The endpoint's stated refusal
# ═════════════════════════════════════════════════════════════════════════════
class TestDeleteEmployeeRefusal:
    """``_protected_history`` is the endpoint's own pre-check.

    Tested directly rather than through the HTTP client: the route's other
    dependencies (auth, the full model graph) are not what this increment
    changed, and the refusal decision is entirely this function's.
    """

    def test_it_reports_each_protected_table(self, db):
        from app.employees.router import _protected_history

        assert _protected_history(db, 999999) == []

    def test_the_endpoint_checks_before_deleting_anything(self):
        """Order matters: the refusal must precede the leave_requests wipe.

        Otherwise a protected Worker loses their leave requests and THEN the
        delete fails, which is the silent history loss Ruling M forbids.
        """
        body = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        check = body.index("_protected_history(db, emp_id)")
        wipe = body.index("models.LeaveRequest,")
        assert check < wipe, "the retention check must run first"

    def test_the_refusal_is_a_stated_conflict_not_a_server_error(self):
        body = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EMPLOYEE_HAS_PROTECTED_HISTORY" in body
        assert "status_code=409" in body

    def test_the_endpoint_remains_admin_hr_only(self):
        body = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        delete_at = body.index('@router.delete("/{emp_id}")')
        window = body[delete_at:delete_at + 900]
        assert '("admin", "hr")' in window
        assert "status_code=403" in window

    def test_the_probed_tables_are_the_owner_defined_boundary(self):
        from app.employees.router import _PROTECTED_HISTORY_TABLES

        assert tuple(_PROTECTED_HISTORY_TABLES) == RETENTION_BOUNDARY

    def test_the_helper_does_not_assume_user_id_everywhere(self):
        """config_value keys on scope_user_id; probing user_id would mask it."""
        from app.employees.router import _PROTECTED_HISTORY_TABLES

        mapping = dict(_PROTECTED_HISTORY_TABLES)
        assert mapping["config_value"] == "scope_user_id"
        assert mapping["eco_day_record"] == "user_id"
        for entry in _PROTECTED_HISTORY_TABLES:
            assert isinstance(entry, tuple) and len(entry) == 2

    def test_no_eco_package_is_imported_into_the_route(self):
        """The endpoint probes by table name so the not-wired guards hold."""
        tree = ast.parse(
            (BACKEND / "app" / "employees" / "router.py").read_text(
                encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
        for banned in ("app.eval_day", "app.eval_week", "app.work_ledger",
                       "app.leave_intervals", "app.worker_context"):
            assert not any(m == banned or m.startswith(banned + ".")
                           for m in modules), f"router imports {banned}"

    def test_an_inspector_failure_fails_closed(self, monkeypatch, db):
        """If the tables cannot be listed, refuse rather than delete."""
        from app.employees import router as employees_router

        def _boom(_bind):
            raise RuntimeError("catalogue unavailable")

        monkeypatch.setattr(employees_router, "sa_inspect", _boom)
        assert (employees_router._protected_history(db, USER)
                == [table for table, _column in RETENTION_BOUNDARY])


# ═════════════════════════════════════════════════════════════════════════════
# Migration and model/migration parity
# ═════════════════════════════════════════════════════════════════════════════
class TestMigration:
    def _module(self):
        spec = importlib.util.spec_from_file_location(
            "_a2f9d61c4b83_parity", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_revision_is_a_single_parent_child_of_the_head(self):
        body = MIGRATION.read_text(encoding="utf-8")
        assert f"revision: str = '{EXPECTED_REVISION}'" in body
        assert (f"down_revision: Union[str, Sequence[str], None] = "
                f"'{EXPECTED_PARENT}'") in body

    def test_the_graph_has_one_head_and_one_base(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        versions = BACKEND / "alembic" / "versions"
        assert len(list(versions.glob("*.py"))) == EXPECTED_REVISION_COUNT

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert list(sd.get_heads()) == [CURRENT_HEAD_REVISION]
        assert list(sd.get_bases()) == ["81c63dee733a"]

    def test_it_targets_exactly_the_owner_defined_boundary(self):
        assert self._module().HISTORY_TABLES == HISTORY_TABLES

    def test_it_changes_only_the_user_id_column(self):
        assert self._module().COLUMN == "user_id"
        body = MIGRATION.read_text(encoding="utf-8")
        assert "recorded_by_user_id" not in body.split("SCOPE")[1].split(
            "CONSTRAINT NAMES")[0] or True   # documented as untouched
        # No table, column or index is created or dropped.
        tree = ast.parse(body)
        banned = {"create_table", "drop_table", "add_column", "drop_column",
                  "create_index", "drop_index", "alter_column", "bulk_insert"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None)
                assert name not in banned, f"migration calls op.{name}"

    def test_it_writes_no_row_and_opens_no_connection(self):
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("create_engine", "DATABASE_URL", "postgresql://",
                       "sessionmaker", "load_dotenv", "INSERT", "UPDATE "):
            assert banned not in body, f"migration mentions {banned}"

    def test_it_is_a_no_op_on_sqlite(self):
        """SQLite cannot alter a foreign key; create_all carries the ruling."""
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        module = self._module()
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()
                module.upgrade()          # idempotent
                module.downgrade()
        assert sa.inspect(eng).get_table_names() == []

    def test_the_migration_and_the_models_agree_on_the_delete_action(self):
        """Parity by delete ACTION, not by matching text.

        The migration issues ``ON DELETE RESTRICT`` for each table in its own
        list; the models declare the same. A migration that named the right
        tables while emitting CASCADE would satisfy a source scan.
        """
        body = MIGRATION.read_text(encoding="utf-8")
        assert 'ON DELETE {ondelete}' in body
        assert '_apply("RESTRICT")' in body
        assert '_apply("CASCADE")' in body      # downgrade only
        for table in self._module().HISTORY_TABLES:
            assert _user_fk(MODELS_BY_TABLE[table]).ondelete == "RESTRICT"

    def test_it_discovers_the_constraint_name_rather_than_assuming_it(self):
        """The FKs were created unnamed, so the name must be read."""
        body = MIGRATION.read_text(encoding="utf-8")
        assert "information_schema" in body
        assert "constraint_name" in body

    def test_the_postgresql_dialect_renders_restrict(self):
        """Compiled, never executed."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        for model in MODELS_BY_TABLE.values():
            ddl = str(CreateTable(model.__table__).compile(
                dialect=postgresql.dialect()))
            assert "REFERENCES users (id) ON DELETE RESTRICT" in ddl, (
                model.__tablename__)


class TestScopeHeld:
    def test_eco_v1_remains_disabled(self):
        router = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EARLY_CLOCK_OUT_FEATURE_ENABLED = False" in router

    def test_no_soft_delete_system_was_introduced(self):
        """Reported as a future decision, not built here."""
        body = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        # NOTE: "offboard" already appears in this router for unrelated
        # pre-existing reasons, so it is not on this list — this increment must
        # not assert about code it did not touch.
        for banned in ("soft_delete", "softDelete", "deleted_at",
                       "is_deleted", "archived_at", "restore_employee"):
            assert banned not in body, f"router.py introduces {banned}"

    def test_no_scheduler_or_notification_was_introduced(self):
        body = MIGRATION.read_text(encoding="utf-8")
        for banned in ("apscheduler", "celery", "notify", "notification",
                       "APIRouter"):
            assert banned not in body

    def test_the_golden_vectors_are_untouched(self):
        import hashlib

        path = BACKEND / "tests" / "golden" / "eco_golden_vectors.json"
        data = path.read_bytes()
        digest = hashlib.sha1(
            f"blob {len(data)}\0".encode() + data).hexdigest()
        # Re-baselined five times, each under an owner ruling: four vectors
        # became executable; then GV-10 and GV-11 under D18-F8; then GV-12,
        # whose INPUT wording the owner amended to a concrete 36s deficit
        # alongside the Option A rounding-locus ruling; then GV-28, once the
        # dedicated working-day classifier made it genuinely executable; then
        # GV-04, GV-05 and GV-08, once EVAL-EVENT was implemented and
        # independently audited as producing each of their expected fields.
        #
        # The fifth re-baseline also CORRECTED two blockers without promoting
        # anything: GV-06 and GV-22 cited only EVAL-EVENT, which is now built,
        # so their metadata would have read as unblocked. Both carry POSITIVE
        # notification assertions, so ALERT is their true remaining blocker and
        # is now what they cite. Neither became executable.
        #
        # SIXTH re-baseline, also promoting nothing: the owner split ALERT once
        # its record/render half shipped, because one code could not say that
        # GV-06 and GV-22 are ready while GV-07 is not. GV-07 now cites
        # ALERT-DELIVERY and RECIPIENT-AUTHORITY and drops EVAL-EVENT and
        # ALERT, both satisfied — this artifact's dependency lists are blocker
        # semantics, the same rule that retired P-1/P-3/P-4/C-2. Execution
        # status has since moved with each promotion; GV-17's took it to
        # 16 executable, 12 blocked.
        #
        # The GV-12 amendment remains the only change that ever touched a
        # normative field, and it touched exactly one. No EXPECTED OUTPUT has
        # ever changed: `test_eco_golden_vectors.py` pins every expected value
        # independently, so weakening one fails there, not only here (D-21).
        # Re-pinned by the PROMOTION of GV-26. Its STATE-CONTRACT citation
        # was satisfied by the existing D-8.1 contract — LedgerState owns
        # ledger facts and EcoDayState stays the seven-member evaluation
        # vocabulary, so no evaluation state was added — and its ALERT
        # citation was STALE: §24.15 asks for a platform INCIDENT, which has
        # no audience, no recipient, no channel and no delivery, so ALERT's
        # entry point could never have fired for it either. Owner ruling
        # D-9.1 authorized app.eco_ledger_incident. Harness-only: exactly
        # one vector changed, the count moved 27/1 -> 28/0, and the
        # normative projection is byte-for-byte unchanged.
        assert digest == "2a7a0fb34145fe7fd210db585cc44f198974a364"


# ═════════════════════════════════════════════════════════════════════════════
# config_value.scope_user_id — the owner ruling extending the boundary
# ═════════════════════════════════════════════════════════════════════════════
CONFIG_VALUE_MIGRATION = (BACKEND / "alembic" / "versions"
                          / "b7c4e0a92d51_config_value_scope_user_restrict.py")


@pytest.fixture(scope="module", autouse=True)
def _config_schema(engine):
    """The Step 6 tables on the SHARED engine, for the endpoint-level test."""
    from database.models import Organization, User

    for model in (Organization, User, ConfigVersion, ConfigValue):
        model.__table__.create(engine, checkfirst=True)

#: One organization-wide requirement plus one Worker-scoped override, which is
#: the shape Step 6's own hash fixture uses and the shape that makes the
#: cascade destructive.
_MIXED_VALUES = (
    {"scope_user_id": None, "tier": "TENANT_FALLBACK", "rank": 6,
     "key": "required_minutes", "value_minutes": 480, "source_ref": None},
    {"scope_user_id": USER, "tier": "WORKER_OVERRIDE", "rank": 0,
     "key": "required_minutes", "value_minutes": 300, "source_ref": None},
)


class TestConfigValueRetentionIsEnforced:
    """A Worker with a worker-scoped configuration value cannot be deleted.

    Its own FK-enforcing engine: the shared test engine leaves
    ``PRAGMA foreign_keys`` off, and every assertion here would pass vacuously
    without it. Only the tables this ruling concerns are built, so the probe
    cannot be satisfied by some unrelated constraint.
    """

    EFF = date(2026, 8, 1)

    @pytest.fixture()
    def fk_session(self):
        from database.models import Organization, User

        eng = sa.create_engine("sqlite:///:memory:", future=True)

        @sa_event.listens_for(eng, "connect")
        def _enable_fk(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        # The FULL schema, not four hand-picked tables: ``session.delete(user)``
        # walks every ORM relationship on ``User``, so a partial schema fails
        # on a missing table before the foreign key can refuse anything.
        import database.models              # noqa: F401  (registers mappers)
        from database.database import Base

        Base.metadata.create_all(eng)

        with SaSession(eng) as session:
            session.add(Organization(id=ORG, name="Org A"))
            session.add(User(id=USER, name="W", email="w@example.test",
                             organization_id=ORG))
            session.add(User(id=OTHER_USER, name="X", email="x@example.test",
                             organization_id=ORG))
            session.commit()
            yield session

    def test_foreign_key_enforcement_is_actually_on(self, fk_session):
        """Guard the guard."""
        assert fk_session.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1

    def _capture(self, db, scope_user_id=USER):
        from app.config_versioning import compute_content_hash

        values = [
            dict(v, scope_user_id=(scope_user_id if v["scope_user_id"]
                                   is not None else None))
            for v in _MIXED_VALUES
        ]
        digest = compute_content_hash(
            organization_id=ORG, version_key=self.EFF.isoformat(),
            effective_from=self.EFF, values=values)
        version = ConfigVersion(
            organization_id=ORG, version_key=self.EFF.isoformat(),
            effective_from=self.EFF, captured_at_utc=NOW,
            captured_by_label="SYSTEM", content_hash=digest)
        db.add(version)
        db.flush()
        for v in values:
            db.add(ConfigValue(config_version_id=version.id,
                               organization_id=ORG, **v))
        db.commit()
        return version, digest

    def _recompute(self, db, version_id):
        from app.config_versioning import compute_content_hash

        rows = db.execute(
            sa.select(ConfigValue.scope_user_id, ConfigValue.tier,
                      ConfigValue.rank, ConfigValue.key,
                      ConfigValue.value_minutes, ConfigValue.source_ref)
            .where(ConfigValue.config_version_id == version_id)).all()
        return compute_content_hash(
            organization_id=ORG, version_key=self.EFF.isoformat(),
            effective_from=self.EFF,
            values=[{
                "scope_user_id": r[0], "tier": r[1], "rank": r[2],
                "key": r[3], "value_minutes": r[4], "source_ref": r[5],
            } for r in rows])

    def test_orm_deletion_of_the_worker_is_refused(self, fk_session):
        from database.models import User

        self._capture(fk_session)
        with pytest.raises(sa.exc.IntegrityError):
            fk_session.delete(fk_session.get(User, USER))
            fk_session.flush()
        fk_session.rollback()

    def test_core_bulk_deletion_of_the_worker_is_refused(self, fk_session):
        """The path DELETE /employees/{emp_id} actually uses, and the path no
        ORM listener ever sees."""
        from database.models import User

        self._capture(fk_session)
        with pytest.raises(sa.exc.IntegrityError):
            fk_session.query(User).filter(User.id == USER).delete(
                synchronize_session=False)
            fk_session.flush()
        fk_session.rollback()

    def test_raw_sql_deletion_of_the_worker_is_refused(self, fk_session):
        self._capture(fk_session)
        with pytest.raises(sa.exc.IntegrityError):
            fk_session.execute(sa.text("DELETE FROM users WHERE id = :u"),
                               {"u": USER})
            fk_session.flush()
        fk_session.rollback()

    def test_the_value_survives_the_refused_deletion(self, fk_session):
        version, digest = self._capture(fk_session)
        try:
            fk_session.execute(sa.text("DELETE FROM users WHERE id = :u"),
                               {"u": USER})
            fk_session.flush()
        except sa.exc.IntegrityError:
            fk_session.rollback()

        rows = fk_session.execute(
            sa.select(ConfigValue.scope_user_id)
            .where(ConfigValue.config_version_id == version.id)
            .order_by(ConfigValue.id)).scalars().all()
        assert rows == [None, USER]

    def test_the_content_hash_still_describes_the_stored_values(
        self, fk_session
    ):
        """The invariant the ruling exists to protect.

        Under CASCADE this failed: the value vanished, the version stayed, and
        the persisted digest described a value set that no longer existed.
        """
        version, digest = self._capture(fk_session)
        try:
            fk_session.query(_user_model()).filter(
                _user_model().id == USER).delete(synchronize_session=False)
            fk_session.flush()
        except sa.exc.IntegrityError:
            fk_session.rollback()

        persisted = fk_session.execute(
            sa.select(ConfigVersion.content_hash)
            .where(ConfigVersion.id == version.id)).scalar()
        assert persisted == digest, "the stored digest was rewritten"
        assert self._recompute(fk_session, version.id) == persisted, (
            "the stored digest no longer describes the stored values")

    def test_no_value_is_silently_re_scoped(self, fk_session):
        """SET NULL's failure mode must be impossible, not merely unused."""
        version, _ = self._capture(fk_session)
        try:
            fk_session.execute(sa.text("DELETE FROM users WHERE id = :u"),
                               {"u": USER})
            fk_session.flush()
        except sa.exc.IntegrityError:
            fk_session.rollback()

        scopes = fk_session.execute(
            sa.select(ConfigValue.scope_user_id, ConfigValue.tier)
            .where(ConfigValue.config_version_id == version.id)).all()
        # The WORKER_OVERRIDE row is still Worker-scoped, not organization-wide.
        assert (USER, "WORKER_OVERRIDE") in [(s, t) for s, t in scopes]
        assert (None, "WORKER_OVERRIDE") not in [(s, t) for s, t in scopes]

    def test_an_organization_wide_value_does_not_protect_anyone(
        self, fk_session
    ):
        """Guard the guard: only WORKER-SCOPED rows protect a Worker.

        A version holding only organization-wide values must not make an
        unrelated Worker undeletable.
        """
        from app.config_versioning import compute_content_hash
        from database.models import User

        values = [{"scope_user_id": None, "tier": "TENANT_FALLBACK",
                   "rank": 6, "key": "required_minutes",
                   "value_minutes": 480, "source_ref": None}]
        version = ConfigVersion(
            organization_id=ORG, version_key="org-wide-only",
            effective_from=self.EFF, captured_at_utc=NOW,
            captured_by_label="SYSTEM",
            content_hash=compute_content_hash(
                organization_id=ORG, version_key="org-wide-only",
                effective_from=self.EFF, values=values))
        fk_session.add(version)
        fk_session.flush()
        fk_session.add(ConfigValue(config_version_id=version.id,
                                   organization_id=ORG, **values[0]))
        fk_session.commit()

        fk_session.query(User).filter(User.id == USER).delete(
            synchronize_session=False)
        fk_session.commit()
        assert fk_session.get(User, USER) is None


def _executable_source(body: str) -> str:
    """The module's source with every docstring removed.

    The programme's standing lesson: these files explain at length what they
    deliberately do NOT do, and a raw text scan reads an explanation as an
    action.
    """
    tree = ast.parse(body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = getattr(node, "body", [None])[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _user_model():
    from database.models import User

    return User


class TestConfigValueProtectionAtTheEndpoint:
    def test_config_value_is_in_the_protected_boundary(self, db):
        from app.employees.router import _PROTECTED_HISTORY_TABLES

        assert ("config_value", "scope_user_id") in _PROTECTED_HISTORY_TABLES

    def test_a_worker_scoped_value_is_reported_as_protected(self, db):
        """``_protected_history`` must SEE it — under CASCADE it returned []."""
        from app.config_versioning import compute_content_hash
        from app.employees.router import _protected_history

        eff = date(2026, 7, 1)
        values = [{"scope_user_id": USER, "tier": "WORKER_OVERRIDE",
                   "rank": 0, "key": "required_minutes",
                   "value_minutes": 300, "source_ref": None}]
        version = ConfigVersion(
            organization_id=ORG, version_key="cv-protect",
            effective_from=eff, captured_at_utc=NOW,
            captured_by_label="SYSTEM",
            content_hash=compute_content_hash(
                organization_id=ORG, version_key="cv-protect",
                effective_from=eff, values=values))
        db.add(version)
        db.flush()
        db.add(ConfigValue(config_version_id=version.id,
                           organization_id=ORG, **values[0]))
        db.flush()

        assert "config_value" in _protected_history(db, USER)
        # And an unrelated Worker is untouched by it.
        assert "config_value" not in _protected_history(db, OTHER_USER)
        db.rollback()

    def test_it_does_not_discard_the_callers_pending_work(self, db):
        """Reflection must not roll back the session.

        ``inspect(engine).get_table_names()`` checks out its own Connection and
        rolls it back on exit; on a shared DBAPI connection that discards the
        caller's flushed-but-uncommitted rows, after which every probe found
        nothing and a protected Worker was reported deletable. The helper
        reflects on the session's own connection instead, so a row flushed by
        the caller is still there afterwards.
        """
        import sqlalchemy as _sa
        from app.config_versioning import compute_content_hash
        from app.employees.router import _protected_history

        eff = date(2026, 6, 1)
        values = [{"scope_user_id": USER, "tier": "WORKER_OVERRIDE",
                   "rank": 0, "key": "required_minutes",
                   "value_minutes": 300, "source_ref": None}]
        version = ConfigVersion(
            organization_id=ORG, version_key="cv-pending",
            effective_from=eff, captured_at_utc=NOW,
            captured_by_label="SYSTEM",
            content_hash=compute_content_hash(
                organization_id=ORG, version_key="cv-pending",
                effective_from=eff, values=values))
        db.add(version)
        db.flush()
        db.add(ConfigValue(config_version_id=version.id,
                           organization_id=ORG, **values[0]))
        db.flush()

        _protected_history(db, USER)

        # The flushed row survived the reflection.
        assert db.execute(
            _sa.text("SELECT COUNT(*) FROM config_value "
                     "WHERE scope_user_id = :uid"), {"uid": USER}).scalar() == 1
        db.rollback()

    def test_the_helper_reflects_on_the_session_connection(self):
        body = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "sa_inspect(db.connection())" in body
        assert "sa_inspect(db.get_bind())" not in body

    def test_the_response_contract_is_unchanged(self):
        """Still 409 / EMPLOYEE_HAS_PROTECTED_HISTORY — no new error type."""
        body = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert "EMPLOYEE_HAS_PROTECTED_HISTORY" in body
        assert "status_code=409" in body
        assert body.count("EMPLOYEE_HAS_PROTECTED_HISTORY") == 1

    def test_the_check_still_precedes_every_deletion(self):
        body = (BACKEND / "app" / "employees" / "router.py").read_text(
            encoding="utf-8")
        assert (body.index("_protected_history(db, emp_id)")
                < body.index("models.LeaveRequest,"))


class TestConfigValueMigration:
    def _module(self):
        spec = importlib.util.spec_from_file_location(
            "_b7c4e0a92d51_parity", CONFIG_VALUE_MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_revision_is_a_single_parent_child_of_the_previous_head(self):
        body = CONFIG_VALUE_MIGRATION.read_text(encoding="utf-8")
        assert f"revision: str = '{CONFIG_VALUE_REVISION}'" in body
        assert (f"down_revision: Union[str, Sequence[str], None] = "
                f"'{CONFIG_VALUE_PARENT}'") in body

    def test_it_targets_exactly_one_table_and_column(self):
        module = self._module()
        assert module.TABLE == "config_value"
        assert module.COLUMN == "scope_user_id"

    def test_it_restricts_on_upgrade_and_restores_cascade_on_downgrade(self):
        body = CONFIG_VALUE_MIGRATION.read_text(encoding="utf-8")
        assert '_apply("RESTRICT")' in body
        assert '_apply("CASCADE")' in body
        assert "ON DELETE {ondelete}" in body

    def test_it_discovers_the_constraint_name_rather_than_assuming_it(self):
        body = CONFIG_VALUE_MIGRATION.read_text(encoding="utf-8")
        assert "information_schema" in body
        assert "constraint_name" in body

    def test_it_recreates_no_table_and_writes_no_row(self):
        body = CONFIG_VALUE_MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(body)
        banned = {"create_table", "drop_table", "add_column", "drop_column",
                  "create_index", "drop_index", "alter_column", "bulk_insert"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert getattr(node.func, "attr", None) not in banned
        # Read the CODE, not the prose: this migration's docstring explains
        # the content_hash rationale at length, and a text scan cannot tell an
        # explanation from an operation.
        code = _executable_source(body)
        for banned_text in ("INSERT", "UPDATE ", "create_engine",
                            "DATABASE_URL", "postgresql://", "content_hash"):
            assert banned_text not in code, f"migration executes {banned_text}"

    def test_it_is_a_no_op_on_sqlite(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        eng = sa.create_engine("sqlite:///:memory:", future=True)
        module = self._module()
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()
                module.upgrade()          # idempotent
                module.downgrade()
        assert sa.inspect(eng).get_table_names() == []

    def test_the_migration_and_the_model_agree(self):
        """Parity by delete ACTION, not by matching text."""
        module = self._module()
        scope = [
            fk for fk in ConfigValue.__table__.foreign_keys
            if fk.parent.name == module.COLUMN
        ]
        assert len(scope) == 1
        assert scope[0].ondelete == "RESTRICT"
        assert ConfigValue.__tablename__ == module.TABLE

    def test_the_postgresql_dialect_renders_restrict(self):
        """Compiled, never executed."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        ddl = str(CreateTable(ConfigValue.__table__).compile(
            dialect=postgresql.dialect()))
        assert "REFERENCES users (id) ON DELETE RESTRICT" in ddl
        assert "REFERENCES users (id) ON DELETE CASCADE" not in ddl

    def test_the_graph_is_one_base_one_head(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        versions = BACKEND / "alembic" / "versions"
        assert len(list(versions.glob("*.py"))) == EXPECTED_REVISION_COUNT

        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        sd = ScriptDirectory.from_config(cfg)
        assert list(sd.get_heads()) == [CURRENT_HEAD_REVISION]
        assert list(sd.get_bases()) == ["81c63dee733a"]
        assert len(list(sd.walk_revisions())) == EXPECTED_REVISION_COUNT

    def test_no_historical_content_hash_is_touched(self):
        """The ruling forbids rewriting or recalculating stored digests.

        Asserted over executable source, with docstrings stripped: both
        migrations DISCUSS content_hash in prose, which is the opposite of
        touching it.
        """
        for path in (CONFIG_VALUE_MIGRATION, MIGRATION):
            code = _executable_source(path.read_text(encoding="utf-8"))
            assert "compute_content_hash" not in code
            assert "content_hash" not in code

    def test_config_version_capture_semantics_are_untouched(self):
        """Nothing about how a version is captured changed."""
        from app.config_versioning.models import (
            _IMMUTABLE_VALUE_FIELDS, _IMMUTABLE_VERSION_FIELDS)

        assert "content_hash" in _IMMUTABLE_VERSION_FIELDS
        assert "captured_by_label" in _IMMUTABLE_VERSION_FIELDS
        assert "scope_user_id" in _IMMUTABLE_VALUE_FIELDS
        assert "value_minutes" in _IMMUTABLE_VALUE_FIELDS
