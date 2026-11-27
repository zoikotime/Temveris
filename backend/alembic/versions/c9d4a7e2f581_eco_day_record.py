"""eco day record (EVAL-DAY)

Revision ID: c9d4a7e2f581
Revises: b8c3f6a1d472
Create Date: 2026-09-03 00:00:00.000000

Creates the derived ECO day-evaluation record:

    eco_day_record   one evaluation per Worker-day per authoritative snapshot

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
Owner ruling R1 defines the DayRecord as the PERSISTED ECO evaluation result,
and R15 requires repeated evaluation of one snapshot not to create duplicate
logical records. Neither is achievable without storage. No existing table can
hold it: ``work_day_ledger`` is authoritative EVIDENCE and must not gain derived
output, and every other candidate is a different subject.

DERIVED, NOT EVIDENCE
---------------------
The sealed ledger stays authoritative for source intervals. This table records
what the evaluator concluded. Recomputation against a changed snapshot writes a
NEW row; historical evaluations are never mutated in place (R1).

SNAPSHOT IDENTITY (R2)
----------------------
    (organization_id, user_id, work_date,
     ledger_version, policy_version, config_version)

``user_id`` rather than R2's ``worker_id``, for consistency with D-09 Option C
and with Steps 1, 6, 7, 8 and Worker Context, all of which use ``user_id``.

``policy_version`` is nullable and, in this increment, always NULL: the platform
has no policy-version concept distinct from Step 6 configuration versioning.
The column exists because R2 names it in the authoritative identity; it is left
unpopulated rather than aliased to ``config_version``, which would invent a
second meaning for one fact.

Because two identity components are nullable, uniqueness is a COALESCE
expression index — NULL never equals NULL in a unique index on PostgreSQL OR
SQLite, so a plain constraint would let duplicate snapshots through and defeat
R15. Same defect found and fixed in Step 6. The ORM model declares the identical
index so a create_all-built database is governed the same way.

PURELY ADDITIVE
---------------
No existing table altered, renamed or dropped. ``work_day_ledger``,
``work_state_interval``, ``users`` and every configuration table are untouched.
NO ROWS CREATED — no backfill, no historical evaluation. ECO evaluation has not
run and is not enabled.

MIGRATION PARENT
----------------
``down_revision = 'b8c3f6a1d472'`` — the current single head. Ordinary
single-parent child: no new head, no merge revision, no history modified.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so no
command executing ``env.py`` was run. Verified offline against a private
in-memory SQLite database and by PostgreSQL dialect compilation without
connecting.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c9d4a7e2f581'
down_revision: Union[str, Sequence[str], None] = 'b8c3f6a1d472'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "eco_day_record" not in existing:
        op.create_table(
            'eco_day_record',
            # No primary-key index: already indexed on both backends, and an
            # auto-generated name is a collision risk (the Step 7 lesson).
            sa.Column('id', sa.Integer(), nullable=False),

            # ── Snapshot identity + provenance (R2, R14) ──────────────────
            sa.Column('organization_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('work_date', sa.Date(), nullable=False),
            sa.Column('ledger_version', sa.Integer(), nullable=False),
            # Always NULL in this increment — see the module docstring.
            sa.Column('policy_version', sa.String(length=64), nullable=True),
            # Step 6 config version key; NULL when no version covered the date.
            sa.Column('config_version', sa.String(length=64), nullable=True),
            # The Step 5 outcome verbatim, so an unresolved result records WHY.
            sa.Column('requirement_outcome', sa.String(length=32), nullable=False),
            # SHA-256 over identity + result: a snapshot re-evaluating to a
            # different answer means the evaluator changed, and must be
            # detectable rather than silently overwritten.
            sa.Column('content_hash', sa.String(length=64), nullable=False),
            sa.Column('evaluated_at_utc', sa.DateTime(timezone=True),
                      nullable=False),

            # ── Normative result fields (R3) ──────────────────────────────
            # Text, not a DB enum (§32: enum values cannot be corrected later
            # without a breaking change).
            sa.Column('state', sa.String(length=32), nullable=False),
            # All NULL when the day could not be computed. §10.2: an unevaluated
            # day must not carry a number a caller could read as a result.
            sa.Column('required_minutes', sa.Integer(), nullable=True),
            sa.Column('qualifying_minutes', sa.Integer(), nullable=True),
            sa.Column('gross_shortfall_minutes', sa.Integer(), nullable=True),
            sa.Column('exclusion_minutes', sa.Integer(), nullable=True),
            sa.Column('attributable_minutes', sa.Integer(), nullable=True),
            # Never populated in this increment: R12 forbids inventing
            # schedule-variance mathematics.
            sa.Column('schedule_variance_minutes', sa.Integer(), nullable=True),
            sa.Column('contributes_to_accumulator_minutes', sa.Integer(),
                      nullable=True),
            sa.Column('shortfall_computed', sa.Boolean(), nullable=False),

            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                    ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.CheckConstraint('ledger_version >= 1',
                               name='ck_eco_day_record_ledger_version_positive'),
            sa.CheckConstraint('length(state) > 0',
                               name='ck_eco_day_record_state_nonempty'),
            sa.CheckConstraint('length(content_hash) > 0',
                               name='ck_eco_day_record_hash_nonempty'),
            # Minutes are counts. R12 floors the shortfall at zero; this stops a
            # negative reaching storage by any other route.
            sa.CheckConstraint(
                "(required_minutes IS NULL OR required_minutes >= 0) AND "
                "(qualifying_minutes IS NULL OR qualifying_minutes >= 0) AND "
                "(gross_shortfall_minutes IS NULL OR gross_shortfall_minutes >= 0) AND "
                "(exclusion_minutes IS NULL OR exclusion_minutes >= 0) AND "
                "(attributable_minutes IS NULL OR attributable_minutes >= 0) AND "
                "(contributes_to_accumulator_minutes IS NULL OR "
                " contributes_to_accumulator_minutes >= 0)",
                name='ck_eco_day_record_minutes_nonnegative',
            ),
        )
        op.create_index(op.f('ix_eco_day_record_organization_id'),
                        'eco_day_record', ['organization_id'], unique=False)
        op.create_index(op.f('ix_eco_day_record_user_id'), 'eco_day_record',
                        ['user_id'], unique=False)
        op.create_index(op.f('ix_eco_day_record_work_date'), 'eco_day_record',
                        ['work_date'], unique=False)
        op.create_index(op.f('ix_eco_day_record_state'), 'eco_day_record',
                        ['state'], unique=False)
        op.create_index('ix_eco_day_record_org_user_date', 'eco_day_record',
                        ['organization_id', 'user_id', 'work_date'],
                        unique=False)
        # The authoritative snapshot uniqueness rule (R15). COALESCE because
        # policy_version and config_version are nullable and NULL never equals
        # NULL in a unique index — on PostgreSQL AND SQLite.
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_eco_day_record_snapshot "
            "ON eco_day_record "
            "(organization_id, user_id, work_date, ledger_version, "
            " COALESCE(policy_version, ''), COALESCE(config_version, ''))"
        ))

    # No INSERT. No backfill, no historical evaluation.


def downgrade() -> None:
    """Drop only what this revision creates. Nothing pre-existing was changed."""
    op.execute(sa.text("DROP INDEX IF EXISTS uq_eco_day_record_snapshot"))
    op.drop_table('eco_day_record')
