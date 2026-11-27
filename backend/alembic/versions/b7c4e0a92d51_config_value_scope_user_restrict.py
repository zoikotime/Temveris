"""config_value.scope_user_id ON DELETE RESTRICT (Owner Ruling M extension)

Revision ID: b7c4e0a92d51
Revises: a2f9d61c4b83
Create Date: 2026-09-08 00:00:00.000000

``a2f9d61c4b83`` moved five Worker-history tables to ``ON DELETE RESTRICT``.
This extends the same retention boundary to the sixth Worker-referencing
column the audit found, by explicit owner ruling:

    config_value.scope_user_id -> users.id

WHY THIS COLUMN BELONGS TO THE BOUNDARY
---------------------------------------
* ``ConfigValue`` is immutable, and ``scope_user_id`` is in
  ``_IMMUTABLE_VALUE_FIELDS`` — the ORM refuses to change it at all.
* ``scope_user_id`` is inside ``compute_content_hash``, and BOTH capture
  services re-read every stored value and fail closed with "the stored
  content_hash does not describe the stored values" when the digest and the
  rows disagree. The digest is required to describe the COMPLETE persisted
  value set.
* A database-level CASCADE bypasses the ORM listeners, so ``DELETE`` on a
  ``users`` row could strip a Worker-scoped value while leaving the parent
  ``ConfigVersion`` in place with a digest that no longer describes it — a
  silent break, since no read path verifies the hash.
* The five-table protection did NOT cover it: Worker-scoped configuration has
  no dependency on ledger, day-record, snapshot, interval or context rows, so
  the employee-deletion pre-check passed and the cascade fired.

WHY NOT SET NULL
----------------
Prohibited by the ruling, and it is not the same choice as
``ConfigVersion.captured_by_user_id``. There NULL means "a system capture" and
the column is excluded from the digest. HERE NULL MEANS ORGANIZATION-WIDE, so
nulling a Worker's override would silently re-scope it to the entire tenant at
that row's own tier and rank — rank 0 for ``WORKER_OVERRIDE``, which outranks
every other tier. It would also collide with the ``uq_config_value_scope``
unique index whenever an organization-wide row already exists for the same
version, tier and key.

CONSTRAINT NAME
---------------
The foreign key was created UNNAMED by ``d4b7e2f9a615``, so PostgreSQL assigned
``config_value_scope_user_id_fkey``. This revision does not assume that: it
reads the actual name from ``information_schema`` and drops what it finds, so a
differently-named constraint is handled rather than missed. Idempotent —
re-applying finds RESTRICT already in place and re-states it.

NO TABLE RECREATION
-------------------
One ``ALTER TABLE ... DROP CONSTRAINT`` plus one ``ADD CONSTRAINT``. The table
is never rebuilt, so ``uq_config_value_scope`` — a COALESCE expression index
that cannot be reflected — is untouched.

SQLITE
------
SQLite cannot alter a foreign key, so this is a no-op there, exactly like
``a2f9d61c4b83``. A SQLite database is only ever built by ``create_all``, which
takes the action from the model, where this ruling is also declared.

PURELY ADDITIVE
---------------
No table, column or index created or dropped. NO ROW read, written, rewritten
or backfilled. NO ``content_hash`` is recomputed or altered — historical
digests are left exactly as captured, which is what makes them evidence. Only
one foreign key's delete action changes.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so
no command executing ``env.py`` was run. Verified offline against private
throwaway SQLite databases only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c4e0a92d51'
down_revision: Union[str, Sequence[str], None] = 'a2f9d61c4b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "config_value"
COLUMN = "scope_user_id"


def _fk_names() -> list:
    """The actual FK constraint names on ``config_value.scope_user_id``.

    Read from the catalogue rather than assumed: the constraint was created
    unnamed, and guessing the generated name would silently skip a database
    where it differs.
    """
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT tc.constraint_name
              FROM information_schema.table_constraints AS tc
              JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
               AND tc.table_schema = kcu.table_schema
              JOIN information_schema.constraint_column_usage AS ccu
                ON tc.constraint_name = ccu.constraint_name
               AND tc.table_schema = ccu.table_schema
             WHERE tc.constraint_type = 'FOREIGN KEY'
               AND tc.table_name = :table
               AND kcu.column_name = :column
               AND ccu.table_name = 'users'
            """
        ),
        {"table": TABLE, "column": COLUMN},
    ).all()
    return [r[0] for r in rows]


def _apply(ondelete: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # See the module docstring: SQLite cannot alter a foreign key, and a
        # create_all database already carries the model's declaration.
        return

    if TABLE not in set(sa.inspect(bind).get_table_names()):
        # d4b7e2f9a615 has not run in this database. Its FK will be created
        # with the model's RESTRICT when it does.
        return

    for name in _fk_names():
        op.execute(sa.text(
            f'ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS "{name}"'))
    op.execute(sa.text(
        f"ALTER TABLE {TABLE} "
        f"ADD CONSTRAINT {TABLE}_{COLUMN}_fkey "
        f"FOREIGN KEY ({COLUMN}) REFERENCES users (id) "
        f"ON DELETE {ondelete}"
    ))


def upgrade() -> None:
    _apply("RESTRICT")


def downgrade() -> None:
    """Restore CASCADE, which is what ``d4b7e2f9a615`` declared.

    Safe: the constraint is replaced in place, no row is touched, and a Worker
    who could not be deleted under RESTRICT becomes deletable again — the
    pre-ruling behaviour, with the integrity exposure the ruling names.
    """
    _apply("CASCADE")
