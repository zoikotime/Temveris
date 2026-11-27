"""Classification-scoped configuration values (GV-27, owner ruling R1/R4/R5).

Revision ID: a9d3f6b21c58
Revises: d6a8f3c19e47
Create Date: 2026-09-17

WHY
---
GV-27 needs one configuration version to say BOTH

    EMPLOYMENT_POLICY + part_time -> 240
    EMPLOYMENT_POLICY + full_time -> 525

so that re-evaluating a past day resolves the requirement the Worker actually
owed on that day. ``uq_config_value_scope`` made that impossible: its identity
was ``(config_version_id, COALESCE(scope_user_id, 0), tier, key)``, so the two
rows above collided. This revision adds the missing dimension and rebuilds the
index around it.

ADDITIVE AND NON-DESTRUCTIVE
---------------------------
The column is NULLABLE and every existing row keeps NULL, which means exactly
what those rows have always meant: "applies whatever the Worker was". **No
backfill is performed and none is required.** No row is rewritten, no value is
recomputed, and no stored ``content_hash`` is touched — owner ruling R6 requires
historical digests to stay as captured, so a version re-hashed under the new
algorithm would differ only if it were deliberately re-captured.

THE CHECK CONSTRAINTS ARE POSTGRESQL-ONLY, DELIBERATELY
--------------------------------------------------------
SQLite cannot ``ALTER TABLE ... ADD CONSTRAINT``, and a batch rebuild to add one
would copy the table. It does not need to: a SQLite database in this repository
is built by ``Base.metadata.create_all``, which reads the constraints off the
model, so it already has them. The same dialect guard ``e7c2b9d4f815`` uses.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9d3f6b21c58"
down_revision = "d6a8f3c19e47"
branch_labels = None
depends_on = None


TABLE = "config_value"
COLUMN = "worker_classification"

#: The rebuilt identity. COALESCE on BOTH nullable scopes, for the reason the
#: original index existed: NULL never equals NULL in a unique index on either
#: engine, so without it two classification-independent rows for one tier/key
#: would both be storable again.
_NEW_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_config_value_scope "
    "ON config_value "
    "(config_version_id, COALESCE(scope_user_id, 0), "
    "COALESCE(worker_classification, ''), tier, key)"
)

_OLD_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_config_value_scope "
    "ON config_value "
    "(config_version_id, COALESCE(scope_user_id, 0), tier, key)"
)


def _columns(inspector) -> set:
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        # The Phase 2 foundation has not run in this database. Nothing to widen;
        # ``create_all`` will build the table with the column already present.
        return

    if COLUMN not in _columns(inspector):
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=64), nullable=True))

    # Rebuild the uniqueness rule around the new identity. Dropped first: an
    # index cannot be redefined in place, and leaving the old one would keep
    # rejecting exactly the two rows GV-27 needs.
    op.execute(sa.text("DROP INDEX IF EXISTS uq_config_value_scope"))
    op.execute(sa.text(_NEW_INDEX))

    if bind.dialect.name == "postgresql":
        # R4: the two scopes are mutually exclusive.
        op.create_check_constraint(
            "ck_config_value_scope_exclusive", TABLE,
            "scope_user_id IS NULL OR worker_classification IS NULL",
        )
        # R2: exactly two authorized values, or NULL.
        op.create_check_constraint(
            "ck_config_value_classification_vocabulary", TABLE,
            "worker_classification IS NULL OR "
            "worker_classification IN ('full_time', 'part_time')",
        )


def downgrade() -> None:
    """Restore the previous identity and drop the column.

    Safe only while no classification-scoped row exists: with the narrower
    index, ``EMPLOYMENT_POLICY + part_time`` and ``EMPLOYMENT_POLICY +
    full_time`` become duplicates and the unique index would refuse to build.
    That is the correct failure — it says the data now depends on the dimension
    being removed, rather than silently discarding one of the two answers.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        return

    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "ck_config_value_classification_vocabulary", TABLE, type_="check")
        op.drop_constraint(
            "ck_config_value_scope_exclusive", TABLE, type_="check")

    op.execute(sa.text("DROP INDEX IF EXISTS uq_config_value_scope"))
    op.execute(sa.text(_OLD_INDEX))

    if COLUMN in _columns(inspector):
        op.drop_column(TABLE, COLUMN)
