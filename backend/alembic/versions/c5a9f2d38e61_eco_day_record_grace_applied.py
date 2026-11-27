"""MODEL-GRACE — record the §11 grace a day was judged against.

Revision ID: c5a9f2d38e61
Revises: b4e8c2a95d17
Create Date: 2026-09-18

ADDITIVE. One nullable column on ``eco_day_record``. No existing column is
altered, no index is rebuilt, no row is rewritten.

**NO BACKFILL, DELIBERATELY.**
------------------------------
Every record written before this revision was evaluated with no grace applied,
because no code path applied any. Writing ``0`` into those rows would state
that a policy grace of zero was resolved and used, which is not what happened —
the comparison was never performed at all. §11 introduces ``grace_minutes``
precisely so the decision is never an implied number, and §10.2 forbids an
unevaluated quantity carrying a value a caller could read as a result. NULL is
therefore the correct and only honest value for history, and it stays NULL.

The same reasoning makes the column NULLABLE rather than ``NOT NULL DEFAULT 0``:
a NOT NULL column would force exactly the fabricated zero this revision refuses
to write, and would go on fabricating it for every future day where no grace
comparison occurs — an uncomputed state, or a day that was not a working one,
for which §11 itself says "no evaluation occurs".

THE CHECK CONSTRAINT IS POSTGRESQL-ONLY, AS IN a9d3f6b21c58
-----------------------------------------------------------
SQLite cannot ADD a CHECK to an existing table, and rebuilding the table to add
one would be a destructive operation on evidentiary records. The model declares
the constraint, so a ``create_all`` database (the test suite) has it and a
migrated PostgreSQL database has it; a migrated SQLite database does not, which
is the same trade already accepted for ``config_value``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c5a9f2d38e61"
down_revision = "b4e8c2a95d17"
branch_labels = None
depends_on = None

TABLE = "eco_day_record"
COLUMN = "grace_minutes_applied"
CONSTRAINT = "ck_eco_day_record_grace_applied_nonnegative"


def _columns(inspector) -> set:
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        # EVAL-DAY has not run in this database. Nothing to widen; create_all
        # builds the table with the column already present.
        return

    if COLUMN not in _columns(inspector):
        op.add_column(TABLE, sa.Column(COLUMN, sa.Integer(), nullable=True))

    if bind.dialect.name == "postgresql":
        op.create_check_constraint(
            CONSTRAINT, TABLE,
            f"{COLUMN} IS NULL OR {COLUMN} >= 0",
        )


def downgrade() -> None:
    """Drop the column.

    Safe: nothing else reads it, and no other column's meaning depends on it.
    What is lost is the explanation for any row where grace zeroed the
    attributable shortfall — after this runs, such a row reads as
    ``gross - exclusion != attributable`` with no account of why. That is the
    honest consequence of removing the field, not a reason to keep a copy of it
    somewhere else.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        return

    if bind.dialect.name == "postgresql":
        op.drop_constraint(CONSTRAINT, TABLE, type_="check")

    if COLUMN in _columns(inspector):
        op.drop_column(TABLE, COLUMN)
