"""worker context history

Revision ID: b8c3f6a1d472
Revises: a7e2d5f8b031
Create Date: 2026-09-03 00:00:00.000000

Creates the worker-context temporal authority:

    worker_context_timeline   one period within one immutable timeline version

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
``User.employment_type`` is a single mutable column with no history, no
effective dating and no supersession, and the employee update path assigns it
with ``setattr`` followed by ``commit`` and no audit row. A classification
change therefore destroys the prior value. GV-27 requires
``[240, 240, 525, 525, 525]`` for a Part-Time -> Full-Time mid-week change,
which cannot be produced from a column holding only the current value.

No existing store can supply it. The audit established that WorkEvidenceChain is
observational and event-triggered, ShiftAssignment is a mutable plan rather than
a fact, payroll does not record employment type at all, and AuditLog was never
written for this field. Step 6's configuration store has the right semantics but
only an integer value column, so it cannot hold a categorical classification.

PURELY ADDITIVE
---------------
No existing table is altered, renamed or dropped. ``users`` is untouched —
``employment_type`` keeps its type, its default and all eight of its consumers.
No data is migrated and NO ROWS ARE CREATED.

**No backfill, deliberately.** Writing "they were Full-Time in March" because
they are Full-Time today would fabricate evidence about a period nobody
captured. Under the approved H-1 ruling, dates before a Worker's earliest
captured period resolve to UNKNOWN and are not evaluated. For Workers whose
prior classification was destroyed by an unaudited overwrite, that value is
genuinely unrecoverable and this migration does not pretend otherwise.

ROLLBACK ORDERING
-----------------
Revert the write path BEFORE dropping this table; dropping it while active code
still writes to it breaks every classification change. Note also that rollback
after capture has begun has an irreversible component: history recorded here
cannot be reconstructed once dropped, because H-1 forbids inferring it.

MIGRATION PARENT
----------------
``down_revision = 'a7e2d5f8b031'`` — the current single head. Ordinary
single-parent child: no new head, no merge revision, no modification of existing
history.

NOT EXECUTED
------------
Not applied to any database. The repository ``.env`` points at production, so no
command that executes ``env.py`` was run. Verified offline against a private
in-memory SQLite database and by inspecting the PostgreSQL dialect compilation
of the same DDL without connecting.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8c3f6a1d472'
down_revision: Union[str, Sequence[str], None] = 'a7e2d5f8b031'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "worker_context_timeline" not in existing:
        op.create_table(
            'worker_context_timeline',
            # No primary-key index: a PK is already indexed on both backends,
            # and an auto-generated name is a collision risk — the Step 7
            # lesson, where leave_interval_set.id and leave_interval.set_id
            # produced byte-identical index names.
            sa.Column('id', sa.Integer(), nullable=False),
            # NOT NULL, deliberately stricter than the nullable
            # users.organization_id. A period whose tenant is unknown cannot be
            # isolated or audited, so the service refuses rather than inherits.
            sa.Column('organization_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            # Monotonic per Worker; also the optimistic-concurrency token. A
            # correction appends a COMPLETE new version so a mis-dated change
            # can be re-dated without any row being updated.
            sa.Column('timeline_version', sa.Integer(), nullable=False),
            # Inclusive lower bound. A DATE, not an instant: a classification
            # takes effect on a day in the organization's calendar.
            sa.Column('effective_from', sa.Date(), nullable=False),
            # The classification value, carried VERBATIM.
            sa.Column('worker_context', sa.String(length=64), nullable=False),
            # Which vocabulary the value belongs to. Keeps a product employment
            # type and any future legal worker category from ever being read as
            # each other — D11 is open and nothing maps between them.
            sa.Column('context_vocabulary', sa.String(length=64), nullable=False),
            # CAPTURED | CORRECTION | CREATION_DEFAULT. A value that came from
            # the column default at Worker creation is never recorded as
            # CAPTURED. ATTESTED is deliberately not a value: H-2 was declined.
            sa.Column('provenance', sa.String(length=32), nullable=False),
            # When captured — audit only, never used to select a period.
            sa.Column('recorded_at_utc', sa.DateTime(timezone=True), nullable=False),
            sa.Column('recorded_by_user_id', sa.Integer(), nullable=True),
            # The version this one corrects. A plain integer, not a foreign key:
            # a version spans several rows, so a reference to one would be
            # arbitrary. Written once; never updated. NULL on version 1.
            sa.Column('supersedes_version', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                    ondelete='CASCADE'),
            # SET NULL: removing a person must never remove classification
            # history they happened to record.
            sa.ForeignKeyConstraint(['recorded_by_user_id'], ['users.id'],
                                    ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'timeline_version', 'effective_from',
                                name='uq_worker_context_timeline_period'),
            sa.CheckConstraint('timeline_version >= 1',
                               name='ck_worker_context_timeline_version_positive'),
            sa.CheckConstraint('length(worker_context) > 0',
                               name='ck_worker_context_timeline_context_nonempty'),
            sa.CheckConstraint('length(context_vocabulary) > 0',
                               name='ck_worker_context_timeline_vocabulary_nonempty'),
            sa.CheckConstraint('length(provenance) > 0',
                               name='ck_worker_context_timeline_provenance_nonempty'),
        )
        op.create_index(op.f('ix_worker_context_timeline_organization_id'),
                        'worker_context_timeline', ['organization_id'],
                        unique=False)
        op.create_index(op.f('ix_worker_context_timeline_user_id'),
                        'worker_context_timeline', ['user_id'], unique=False)
        # The hot read: current version for a Worker, then the latest applicable
        # period within it.
        op.create_index('ix_worker_context_timeline_lookup',
                        'worker_context_timeline',
                        ['organization_id', 'user_id', 'timeline_version',
                         'effective_from'], unique=False)

    # No INSERT. Backfilling would fabricate history — see the module docstring.


def downgrade() -> None:
    """Drop only what this revision creates. Nothing pre-existing was changed.

    Revert the write path first: dropping this table while code still writes to
    it breaks every classification change.
    """
    op.drop_table('worker_context_timeline')
