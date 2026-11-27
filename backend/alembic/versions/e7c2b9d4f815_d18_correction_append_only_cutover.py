"""D-18 correction: append-only cutover history, day-level departure, source fingerprint

Revision ID: e7c2b9d4f815
Revises: d5f1a8c3b724
Create Date: 2026-09-10 00:00:00.000000

Implements the owner-approved D-18 correction rulings.

WHAT THIS DOES
--------------
1. **D18-F1** — replaces the mutable ``work_ledger_producer_cutover`` row with
   an append-only ``work_ledger_producer_cutover_history`` table whose
   authoritative value is ``MAX(producer_cutover_at)``, and installs
   database-level triggers refusing UPDATE and DELETE on it.

2. **D18-F2** — adds three nullable columns to ``work_day_ledger`` recording
   the day's verified departure: ``departure_at``, ``departure_provenance``,
   ``departure_source_ref``, with an all-or-none CHECK and a CHECK restricting
   the provenance to the verified vocabulary.

3. **D18-F3-S** — adds ``work_day_ledger.source_fingerprint``, the digest of
   the SOURCE evidence a version was built from, so a source change can be
   distinguished from a context change.

WHY THE CUTOVER TABLE IS REPLACED
---------------------------------
The post-implementation audit demonstrated four routes that moved the mutable
watermark EARLIER — SQLAlchemy Core UPDATE, ORM bulk ``query.update()``, ORM
``delete()`` then re-set, and Core DELETE then re-set — restoring eligibility
for historical days and defeating the forward-only guarantee. Ruling M had
already established that ORM listeners cannot be the guarantee wherever Core
statements reach the same rows.

Append-only history makes monotonicity structural: an EARLIER row cannot lower
a maximum, and the only way to lower the maximum is to UPDATE or DELETE the
maximum row, which the triggers below refuse.

WHY THE TRIGGER SQL IS IMPORTED, NOT TYPED HERE
-----------------------------------------------
``app/work_ledger_producer/append_only_ddl.py`` is the single definition,
shared with the ORM ``after_create`` hook. It was verified that
``op.create_table`` builds its own ``Table`` object, so the ORM-table listener
does NOT fire during a migration: without the explicit emission below, an
Alembic-migrated database would lack the triggers while a ``create_all`` test
database had them, and every below-ORM test would pass vacuously against
production's real shape. That module imports nothing — no ``Base``, no models,
no SQLAlchemy — precisely so a revision may import it safely.

DIALECTS
--------
Two, and only two, are supported by this repository: PostgreSQL (production)
and SQLite (tests). SQLite receives two ``CREATE TRIGGER`` statements;
PostgreSQL receives a PL/pgSQL guard function plus two triggers. A dialect with
no entry receives NO database-level protection and the migration says so rather
than guessing.

The SQLite statements are verified at runtime by the test suite. **The
PostgreSQL statements are NOT verified at runtime**: no PostgreSQL database is
reachable in this environment and connecting to one is prohibited, so only
their emission and syntax can be asserted offline. That limitation is reported
rather than papered over.

OLD CUTOVER DATA
----------------
The old table is dropped only when it is EMPTY. If it holds rows the migration
RAISES, because its value was reachable by the very bypasses this correction
closes: converting a possibly-lowered mutable value into an append-only
historical claim would fabricate provenance, which the ruling forbids. In
practice the situation cannot arise — ``d5f1a8c3b724`` is unapplied in every
database, as is the whole Phase 2 chain.

NO ROWS CREATED. NO BACKFILL.
-----------------------------
Nothing is inserted. The three departure columns and the fingerprint are
NULLABLE, so they can be added to a populated ``work_day_ledger`` without
inventing a departure or a digest for a day that was sealed before they
existed — and NULL is the truthful value for such a row.

PURELY ADDITIVE, apart from removing the superseded empty cutover table. No
other table is created, altered, renamed or dropped.

MIGRATION PARENT
----------------
``down_revision = 'd5f1a8c3b724'`` — verified offline as the single head of a
72-revision graph with a single base (``81c63dee733a``) and no dangling
parents.

NOT EXECUTED against any real database. The repository ``.env`` points at
production, so no command executing ``env.py`` was run; verification used
private in-memory SQLite only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.work_ledger_producer.append_only_ddl import (
    CUTOVER_HISTORY_TABLE,
    append_only_statements,
    drop_statements,
)

# revision identifiers, used by Alembic.
revision: str = 'e7c2b9d4f815'
down_revision: Union[str, Sequence[str], None] = 'd5f1a8c3b724'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_CUTOVER_TABLE = "work_ledger_producer_cutover"
LEDGER_TABLE = "work_day_ledger"

DEPARTURE_COLUMNS = (
    "departure_at", "departure_provenance", "departure_source_ref",
)
FINGERPRINT_COLUMN = "source_fingerprint"

#: Generated in the model from ``VERIFIED_DEPARTURE_PROVENANCE``; transcribed
#: here so the revision imports no ORM. The parity is asserted by test.
VERIFIED_DEPARTURE_SQL = "'ADMIN_OVERRIDE', 'WORKER'"

DEPARTURE_ALL_OR_NONE_SQL = (
    "(departure_at IS NULL AND departure_provenance IS NULL "
    " AND departure_source_ref IS NULL) OR "
    "(departure_at IS NOT NULL AND departure_provenance IS NOT NULL "
    " AND departure_source_ref IS NOT NULL)"
)
DEPARTURE_VERIFIED_SQL = (
    f"departure_provenance IS NULL OR departure_provenance IN "
    f"({VERIFIED_DEPARTURE_SQL})"
)


def _tables(inspector) -> set:
    return set(inspector.get_table_names())


def _columns(inspector, table: str) -> set:
    if table not in _tables(inspector):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    inspector = sa.inspect(bind)

    # ── D18-F1: the append-only history table ───────────────────────────────
    if CUTOVER_HISTORY_TABLE not in _tables(inspector):
        op.create_table(
            CUTOVER_HISTORY_TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            # No server default: an unconfigured tenant has no rows and is
            # refused, never defaulted.
            sa.Column("producer_cutover_at",
                      sa.DateTime(timezone=True), nullable=False),
            sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("set_by", sa.String(length=64), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["organization_id"], ["organizations.id"], ondelete="RESTRICT",
            ),
            sa.CheckConstraint("length(set_by) > 0",
                               name="ck_wl_cutover_history_actor"),
        )
        op.create_index(
            f"ix_{CUTOVER_HISTORY_TABLE}_id", CUTOVER_HISTORY_TABLE, ["id"],
            unique=False,
        )
        op.create_index(
            f"ix_{CUTOVER_HISTORY_TABLE}_organization_id",
            CUTOVER_HISTORY_TABLE, ["organization_id"], unique=False,
        )
        op.create_index(
            "ix_wl_cutover_history_org_at", CUTOVER_HISTORY_TABLE,
            ["organization_id", "producer_cutover_at"], unique=False,
        )

        # The SAME statements the ORM after_create hook emits. Explicit here
        # because op.create_table builds its own Table and the listener does
        # not fire for it.
        for statement in append_only_statements(dialect):
            op.execute(statement)

    # ── D18-F1: retire the superseded mutable table ─────────────────────────
    inspector = sa.inspect(bind)
    if OLD_CUTOVER_TABLE in _tables(inspector):
        rows = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {OLD_CUTOVER_TABLE}")  # noqa: S608
        ).scalar() or 0
        if rows:
            raise RuntimeError(
                f"{OLD_CUTOVER_TABLE} holds {rows} row(s). Its value was "
                f"reachable by the Core UPDATE / bulk UPDATE / delete-then-"
                f"reset bypasses this correction closes, so it cannot be "
                f"asserted to be the maximum ever set. Converting it into an "
                f"append-only historical claim would fabricate provenance, "
                f"which owner ruling D18-F1 forbids. Resolve those rows by an "
                f"explicit owner-ruled decision, not by this migration."
            )
        # DROP TABLE takes the indexes and constraints with it. Dropping them
        # first is not just redundant -- on PostgreSQL it is fatal, because
        # get_indexes() reports the index BACKING the unique constraint and
        # the server refuses DROP INDEX on it ("cannot drop index ... because
        # constraint ... requires it"). SQLite does not report that index, so
        # the loop looked harmless in every test.
        op.drop_table(OLD_CUTOVER_TABLE)

    # ── D18-F2 / D18-F3-S: the frozen day-level evidence ────────────────────
    inspector = sa.inspect(bind)
    if LEDGER_TABLE not in _tables(inspector):
        # Created — with these columns, from the ORM model — by d4b7e2f9a615
        # plus this revision, or by create_all. Nothing to alter.
        return

    existing = _columns(inspector, LEDGER_TABLE)

    if "departure_at" not in existing:
        op.add_column(LEDGER_TABLE,
                      sa.Column("departure_at", sa.DateTime(timezone=True),
                                nullable=True))
    if "departure_provenance" not in existing:
        op.add_column(LEDGER_TABLE,
                      sa.Column("departure_provenance", sa.String(length=32),
                                nullable=True))
    if "departure_source_ref" not in existing:
        op.add_column(LEDGER_TABLE,
                      sa.Column("departure_source_ref", sa.String(length=128),
                                nullable=True))
    if FINGERPRINT_COLUMN not in existing:
        op.add_column(LEDGER_TABLE,
                      sa.Column(FINGERPRINT_COLUMN, sa.String(length=64),
                                nullable=True))

    # SQLite cannot ADD CONSTRAINT to an existing table; the CHECKs are part
    # of the ORM model, so a create_all schema carries them and PostgreSQL —
    # production — gets them here. Stated rather than silently skipped.
    if dialect != "sqlite":
        existing_checks = {
            c.get("name")
            for c in inspector.get_check_constraints(LEDGER_TABLE)
        }
        if "ck_work_day_ledger_departure_all_or_none" not in existing_checks:
            op.create_check_constraint(
                "ck_work_day_ledger_departure_all_or_none", LEDGER_TABLE,
                DEPARTURE_ALL_OR_NONE_SQL,
            )
        if "ck_work_day_ledger_departure_verified" not in existing_checks:
            op.create_check_constraint(
                "ck_work_day_ledger_departure_verified", LEDGER_TABLE,
                DEPARTURE_VERIFIED_SQL,
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    inspector = sa.inspect(bind)

    if LEDGER_TABLE in _tables(inspector):
        if dialect != "sqlite":
            existing_checks = {
                c.get("name")
                for c in inspector.get_check_constraints(LEDGER_TABLE)
            }
            for name in ("ck_work_day_ledger_departure_verified",
                         "ck_work_day_ledger_departure_all_or_none"):
                if name in existing_checks:
                    op.drop_constraint(name, LEDGER_TABLE, type_="check")
        existing = _columns(inspector, LEDGER_TABLE)
        for column in (FINGERPRINT_COLUMN,) + tuple(reversed(DEPARTURE_COLUMNS)):
            if column in existing:
                op.drop_column(LEDGER_TABLE, column)

    inspector = sa.inspect(bind)
    if CUTOVER_HISTORY_TABLE in _tables(inspector):
        for statement in drop_statements(dialect):
            op.execute(statement)
        for index in {ix["name"]
                      for ix in inspector.get_indexes(CUTOVER_HISTORY_TABLE)}:
            op.drop_index(index, table_name=CUTOVER_HISTORY_TABLE)
        op.drop_table(CUTOVER_HISTORY_TABLE)
