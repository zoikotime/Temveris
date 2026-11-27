"""durable rollout control

Revision ID: a7e2d5f8b031
Revises: f1a6b9c4d283
Create Date: 2026-09-03 00:00:00.000000

Creates the Step 8 durable rollout foundation:

    rollout_control    one row per (organization, control); no row means OFF

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
The existing rollout machinery is not durable, which is the single property
this step exists to provide:

* ``app/commercial/phase5.py`` holds every flag in a module-level dict. Its own
  comment says so — "in-memory: it resets to OFF on restart" — and persisting
  them was always described as future work.
* ``app/commercial/enforcement.py`` layers an environment variable, an
  in-process kill switch and two in-memory capability sets on top of that dict.
  Nothing in either survives a process restart.

``app_settings`` was considered and rejected as the home: it is durable and
organization-scoped, but it is written by 194 call sites across 30 modules
through generic key/value endpoints, carries no version column, and has no
notion of a transition — so a rollout state living there could be set directly
to ENABLED by a code path that knows nothing about observation windows.

PURELY ADDITIVE
---------------
No existing table is altered, renamed or dropped. ``app_settings``,
``audit_logs`` and the commercial tables are untouched. No data is migrated or
backfilled, and NO ROWS ARE CREATED — deliberately. A control with no row reads
OFF, so the correct post-migration state for Early Clock-Out v2 is zero rows,
not a row saying OFF. Seeding rows would also be fabricated rollout history:
records of decisions nobody made.

NO CHECK CONSTRAINT ON ``state``
--------------------------------
Deliberate. The load-bearing guarantee is that an unrecognised stored value
READS as OFF. A CHECK would make that path unreachable from a test, leaving the
guarantee asserted but never exercised — and it matters precisely when
something has already gone wrong and written a value the application did not
expect. The write path validates the transition; the read path coerces anything
unknown to OFF; both are tested end to end.

MIGRATION PARENT
----------------
``down_revision = 'f1a6b9c4d283'`` — the current single head. Ordinary
single-parent child: no new head, no merge revision, no modification of
existing history.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so no
command that executes ``env.py`` was run. Verified offline against a private
in-memory SQLite database, and by inspecting the PostgreSQL dialect compilation
of the same DDL without connecting.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7e2d5f8b031'
down_revision: Union[str, Sequence[str], None] = 'f1a6b9c4d283'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "rollout_control" not in existing:
        op.create_table(
            'rollout_control',
            sa.Column('id', sa.Integer(), nullable=False),
            # The INTEGER organizations.id, per owner ruling D-09 Option C.
            # Note phase5.is_enabled takes the STRING commercial organization
            # id — a different identifier, deliberately not adopted here.
            sa.Column('organization_id', sa.Integer(), nullable=False),
            # Which control. Unknown keys are refused by the service rather
            # than created on demand.
            sa.Column('control_key', sa.String(length=64), nullable=False),
            # OFF | SHADOW | ENABLED as text. Not a database enum (§32: enum
            # values cannot be corrected later without a breaking change), and
            # deliberately without a CHECK — see the module docstring.
            sa.Column('state', sa.String(length=16), nullable=False),
            # Optimistic-concurrency token. A transition computed against a
            # stale value is rejected, never merged.
            sa.Column('state_version', sa.Integer(), nullable=False),
            # Explicitly designated by an administrator. Never inferred.
            sa.Column('is_canary', sa.Boolean(), nullable=False),
            # Days required in SHADOW before ENABLED may be requested.
            sa.Column('min_observation_days', sa.Integer(), nullable=False),
            # Start of the CURRENT observation period; reset on each fresh
            # entry into SHADOW so a re-shadowed control observes again.
            sa.Column('shadow_entered_at', sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column('enabled_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_transition_at', sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column('last_transition_by', sa.String(length=64),
                      nullable=False),
            sa.Column('last_transition_reason', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            sa.PrimaryKeyConstraint('id'),
            # One row per control per organization. This is also what makes a
            # global switch unrepresentable: there is no row shape meaning
            # "every organization".
            sa.UniqueConstraint('organization_id', 'control_key',
                                name='uq_rollout_control_org_key'),
            sa.CheckConstraint('state_version >= 1',
                               name='ck_rollout_control_version_positive'),
            sa.CheckConstraint('min_observation_days >= 0',
                               name='ck_rollout_control_observation_nonnegative'),
            sa.CheckConstraint('length(control_key) > 0',
                               name='ck_rollout_control_key_nonempty'),
        )
        op.create_index(op.f('ix_rollout_control_organization_id'),
                        'rollout_control', ['organization_id'], unique=False)
        # "which organizations are in SHADOW for this control" — the operational
        # read during a rollout.
        op.create_index('ix_rollout_control_key_state', 'rollout_control',
                        ['control_key', 'state'], unique=False)

    # No INSERT. A control with no row reads OFF, which is the required state.


def downgrade() -> None:
    """Drop only what this revision creates. Nothing pre-existing was changed."""
    op.drop_table('rollout_control')
