"""leave input snapshot and eco day record authority (Owner Ruling 2)

Revision ID: d3a1c8b57f92
Revises: e5f8c2a91d64
Create Date: 2026-09-08 00:00:00.000000

Adds the immutable leave input snapshot, and gives ``eco_day_record`` a leave
identity component plus an authority mechanism:

    leave_input_snapshot        one frozen leave input per evaluation
    leave_input_contribution    one row per leave request considered

    eco_day_record.leave_input_snapshot_key   the seventh identity component
    eco_day_record.evaluation_generation      serializes authority
    eco_day_record.supersedes                 lineage
    eco_day_record.superseded_by              NULL = authoritative

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
MODEL-EXCLUSION made approved-leave overlap an authoritative input to a day
evaluation. Two of its components carry no version: a day's leave can come from
several requests, and ``leave_requests.status`` is mutated in place, so
``approved -> rejected`` changes the answer and increments nothing. Re-reading
leave therefore could not reproduce an older evaluation, and R2's claim that
"every authoritative input is in the key" had stopped being true. No existing
table can hold the frozen input: ``leave_interval_set`` is authoritative leave
EVIDENCE and must not gain ECO evaluation output.

CONTENT-ADDRESSED
-----------------
``snapshot_key`` equals the snapshot's own ``content_hash``, so nothing
allocates a key, it survives a database rebuild by construction, and an exact
replay reuses the existing row instead of writing a second one.

AUTHORITY IS SERIALIZED BY THE GENERATION, NOT THE POINTER
----------------------------------------------------------
Two concurrent evaluations with DIFFERENT inputs have different identities, so
both inserts would succeed and both rows would read as current. The unique
constraint on ``(organization_id, user_id, work_date, evaluation_generation)``
rejects one, and that writer re-reads and retries — the mechanism
``uq_work_day_ledger_natural_key`` provides for the ledger.

A partial unique index on ``superseded_by IS NULL`` is deliberately NOT used: a
pointer needs the replacement's id, so the insert must precede the
predecessor's update, which transiently leaves two rows with NULL and violates
such an index inside the writer's own transaction — and unique indexes are not
deferrable.

NO FOREIGN KEY TO ``leave_requests``
------------------------------------
``leave_input_contribution.leave_request_id`` is a historical value. A foreign
key would either block a deletion the existing leave rules permit — changing
leave semantics, which is out of scope — or ``SET NULL`` and destroy the
provenance at the moment it matters. Tenancy is still enforced, by the
composite foreign key to the snapshot.

PURELY ADDITIVE
---------------
Two new tables. Four new columns on ``eco_day_record``, and two CHECK
constraints on it where the dialect supports adding them. The snapshot
uniqueness index is rebuilt to seven components — dropped and recreated, not
altered, and it governs a table that exists in no database and holds no
rows. No row is read, rewritten or backfilled. Nothing else
is altered, renamed or dropped: ``work_day_ledger``, ``work_state_interval``,
``leave_requests``, ``leave_interval_set``, ``leave_interval``, ``users`` and
every configuration table are untouched. NO ROWS CREATED — no backfill.

SQLite ADD COLUMN
-----------------
``leave_input_snapshot_key`` and ``evaluation_generation`` are NOT NULL, and
SQLite refuses ``ADD COLUMN ... NOT NULL`` without a default. Both are added
with a ``server_default``, following the ``e5f8c2a91d64`` precedent. The
defaults are a schema mechanism only: ``record_evaluation`` refuses an
evaluation carrying no snapshot key, so the empty-string default is never a
value the application relies on.

CHECK CONSTRAINTS ON THE EXISTING TABLE
---------------------------------------
``evaluation_generation >= 1`` and a non-empty ``leave_input_snapshot_key`` are
declared on the ORM model, so a ``create_all``-built database enforces them.
They are ALSO added here by ``ALTER TABLE ... ADD CONSTRAINT``, because a
migration-built database is the one production runs on and it would otherwise
lack guards the tests rely on — the parity gap the owner-gate audit found.

PostgreSQL only, and not from caution: SQLite cannot ADD a CHECK constraint by
ALTER at all, and rebuilding the table to fake it would drop the
expression-based identity index, which SQLite cannot reflect. A SQLite database
is only ever built by ``create_all``, which applies the model's constraints
directly. Each ADD is wrapped in a ``pg_constraint`` existence test so
re-applying the revision stays a no-op.

MIGRATION PARENT
----------------
``down_revision = 'e5f8c2a91d64'`` — the current single head, verified against
the Alembic graph before writing. Ordinary single-parent child: no new head, no
merge revision, no history modified.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so
no command executing ``env.py`` was run. Verified offline against a private
in-memory SQLite database only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd3a1c8b57f92'
down_revision: Union[str, Sequence[str], None] = 'e5f8c2a91d64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SNAPSHOT_TABLE = "leave_input_snapshot"
CONTRIBUTION_TABLE = "leave_input_contribution"
DAY_RECORD_TABLE = "eco_day_record"

#: The four columns added to ``eco_day_record``, with the server defaults the
#: two NOT NULL ones need in order to be added to an existing table.
DAY_RECORD_COLUMNS = (
    ("leave_input_snapshot_key", sa.String(length=64), False, ""),
    ("evaluation_generation", sa.Integer(), False, "1"),
    ("supersedes", sa.Integer(), True, None),
    ("superseded_by", sa.Integer(), True, None),
)

#: CHECK constraints the ORM model declares on ``eco_day_record`` and that a
#: create_all-built database therefore already enforces. They must be added by
#: ALTER here so a MIGRATION-built database — production — enforces them too;
#: without this the guards existed only where the tests run.
#:
#: PostgreSQL only, and not from squeamishness: SQLite cannot ADD a CHECK
#: constraint by ALTER at all, and rebuilding the table to fake it would drop
#: the expression-based identity index that cannot be reflected. A SQLite
#: database gets these from the model, which is the only way it is ever built.
DAY_RECORD_CHECKS = (
    ("ck_eco_day_record_generation_positive", "evaluation_generation >= 1"),
    ("ck_eco_day_record_leave_key_nonempty",
     "length(trim(leave_input_snapshot_key)) > 0"),
)


def _existing_columns(table: str) -> set:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())

    # ── The immutable leave input snapshot ──────────────────────────────────
    if SNAPSHOT_TABLE not in existing_tables:
        op.create_table(
            SNAPSHOT_TABLE,
            sa.Column('id', sa.Integer(), nullable=False),
            # Equals content_hash. Derived, never allocated.
            sa.Column('snapshot_key', sa.String(length=64), nullable=False),
            sa.Column('organization_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('work_date', sa.Date(), nullable=False),
            sa.Column('content_hash', sa.String(length=64), nullable=False),
            # The resolved total, stored so historical arithmetic is checkable
            # without re-deriving it from intervals.
            sa.Column('overlap_seconds', sa.Integer(), nullable=False),
            # Requests considered, including those contributing nothing.
            sa.Column('contribution_count', sa.Integer(), nullable=False),
            # Provenance, excluded from the content hash.
            sa.Column('captured_at_utc', sa.DateTime(timezone=True),
                      nullable=False),

            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                    ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('organization_id', 'snapshot_key',
                                name='uq_leave_input_snapshot_key'),
            # The target of the contribution's composite tenant foreign key.
            sa.UniqueConstraint('id', 'organization_id',
                                name='uq_leave_input_snapshot_id_org'),
            sa.CheckConstraint('length(snapshot_key) > 0',
                               name='ck_leave_input_snapshot_key_nonempty'),
            sa.CheckConstraint('length(content_hash) > 0',
                               name='ck_leave_input_snapshot_hash_nonempty'),
            # Ruling A: the public key IS the content hash. Column-to-column,
            # so the database enforces the relationship without knowing how
            # the hash is computed.
            sa.CheckConstraint('snapshot_key = content_hash',
                               name='ck_leave_input_snapshot_key_is_hash'),
            sa.CheckConstraint(
                'overlap_seconds >= 0',
                name='ck_leave_input_snapshot_seconds_nonnegative'),
            sa.CheckConstraint(
                'contribution_count >= 0',
                name='ck_leave_input_snapshot_count_nonnegative'),
        )
        op.create_index(op.f('ix_leave_input_snapshot_organization_id'),
                        SNAPSHOT_TABLE, ['organization_id'], unique=False)
        op.create_index(op.f('ix_leave_input_snapshot_user_id'),
                        SNAPSHOT_TABLE, ['user_id'], unique=False)
        op.create_index('ix_leave_input_snapshot_day', SNAPSHOT_TABLE,
                        ['organization_id', 'user_id', 'work_date'],
                        unique=False)

    # ── One contribution per leave request considered ───────────────────────
    if CONTRIBUTION_TABLE not in existing_tables:
        op.create_table(
            CONTRIBUTION_TABLE,
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('snapshot_id', sa.Integer(), nullable=False),
            # Denormalised, and load-bearing: half of the composite tenant FK.
            sa.Column('organization_id', sa.Integer(), nullable=False),
            # A HISTORICAL VALUE, deliberately not a foreign key — see the
            # module docstring.
            sa.Column('leave_request_id', sa.Integer(), nullable=False),
            sa.Column('set_version', sa.Integer(), nullable=False),
            # Raw LeaveRequest.status, never normalised to a boolean.
            sa.Column('status_at_evaluation', sa.String(length=50),
                      nullable=False),
            sa.Column('interval_fingerprint', sa.String(length=64),
                      nullable=False),
            sa.Column('interval_count', sa.Integer(), nullable=False),
            # 0 when the status was not ECO-approved.
            sa.Column('contributed_seconds', sa.Integer(), nullable=False),

            sa.ForeignKeyConstraint(
                ['snapshot_id', 'organization_id'],
                ['leave_input_snapshot.id',
                 'leave_input_snapshot.organization_id'],
                name='fk_leave_input_contribution_snapshot_org',
                ondelete='RESTRICT',
            ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('snapshot_id', 'leave_request_id',
                                name='uq_leave_input_contribution_request'),
            sa.CheckConstraint(
                'set_version >= 1',
                name='ck_leave_input_contribution_version_positive'),
            sa.CheckConstraint(
                'length(status_at_evaluation) > 0',
                name='ck_leave_input_contribution_status_nonempty'),
            sa.CheckConstraint(
                'length(interval_fingerprint) > 0',
                name='ck_leave_input_contribution_fingerprint_nonempty'),
            sa.CheckConstraint(
                'interval_count >= 0 AND contributed_seconds >= 0',
                name='ck_leave_input_contribution_nonnegative'),
        )
        op.create_index(op.f('ix_leave_input_contribution_organization_id'),
                        CONTRIBUTION_TABLE, ['organization_id'], unique=False)
        op.create_index('ix_leave_input_contribution_snapshot',
                        CONTRIBUTION_TABLE, ['snapshot_id'], unique=False)

    # ── eco_day_record: leave identity + authority ──────────────────────────
    # A no-op when the table is absent: the chain creates it at c9d4a7e2f581,
    # and a create_all-built database gets these columns from the model.
    present = _existing_columns(DAY_RECORD_TABLE)
    if present:
        for name, column_type, nullable, default in DAY_RECORD_COLUMNS:
            if name in present:
                continue
            op.add_column(DAY_RECORD_TABLE, sa.Column(
                name, column_type, nullable=nullable, server_default=default,
            ))

        # IF NOT EXISTS on both: a ``create_all``-built database already has
        # them from the model, and re-applying the revision must be a no-op.
        op.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_eco_day_record_superseded_by "
            "ON eco_day_record (superseded_by)"
        ))
        # The hot read: the authoritative record for a Worker-day.
        op.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_eco_day_record_current "
            "ON eco_day_record "
            "(organization_id, user_id, work_date, superseded_by)"
        ))
        # THE RACE SERIALIZER. A unique index rather than a named constraint so
        # it can be added to an existing table on SQLite as well as PostgreSQL.
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_generation "
            "ON eco_day_record "
            "(organization_id, user_id, work_date, evaluation_generation)"
        ))
        # The snapshot identity gains its seventh component. Dropped and
        # recreated because an expression index cannot be altered. No COALESCE
        # on the leave key: it is NOT NULL, since a day with no contributing
        # leave references an explicit empty snapshot.
        op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
            "ON eco_day_record "
            "(organization_id, user_id, work_date, ledger_version, "
            " COALESCE(policy_version, ''), COALESCE(config_version, ''), "
            " leave_input_snapshot_key)"
        ))

        # The two CHECK constraints the model declares. Guarded by
        # ``pg_constraint`` so re-applying the revision stays a no-op, and
        # skipped entirely elsewhere — see DAY_RECORD_CHECKS.
        if op.get_bind().dialect.name == "postgresql":
            for name, expression in DAY_RECORD_CHECKS:
                op.execute(sa.text(
                    f"DO $$ BEGIN "
                    f"IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                    f"WHERE conname = '{name}') THEN "
                    f"ALTER TABLE eco_day_record "
                    f"ADD CONSTRAINT {name} CHECK ({expression}); "
                    f"END IF; END $$;"
                ))

    # No INSERT. No backfill: ECO evaluation has not run and is not enabled.


def downgrade() -> None:
    """Drop only what this revision creates, and restore the six-part index.

    Safe because the revision is purely additive: the two tables are new, the
    four columns are new, and the snapshot index is returned to exactly the
    definition ``c9d4a7e2f581`` created.
    """
    present = _existing_columns(DAY_RECORD_TABLE)
    if present:
        if op.get_bind().dialect.name == "postgresql":
            for name, _expression in DAY_RECORD_CHECKS:
                op.execute(sa.text(
                    f"ALTER TABLE eco_day_record "
                    f"DROP CONSTRAINT IF EXISTS {name}"
                ))
        op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
            "ON eco_day_record "
            "(organization_id, user_id, work_date, ledger_version, "
            " COALESCE(policy_version, ''), COALESCE(config_version, ''))"
        ))
        op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_generation"))
        op.execute(sa.text("DROP INDEX IF EXISTS ix_eco_day_record_current"))
        op.execute(sa.text(
            "DROP INDEX IF EXISTS ix_eco_day_record_superseded_by"))
        for name, _type, _nullable, _default in reversed(DAY_RECORD_COLUMNS):
            if name in present:
                op.drop_column(DAY_RECORD_TABLE, name)

    op.drop_table(CONTRIBUTION_TABLE)
    op.drop_table(SNAPSHOT_TABLE)
