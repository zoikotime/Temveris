"""intraday leave intervals

Revision ID: f1a6b9c4d283
Revises: d4b7e2f9a615
Create Date: 2026-09-03 00:00:00.000000

Creates the Step 7 intraday leave foundation:

    leave_interval_set   one recorded version of a request's intraday boundaries
    leave_interval       the boundaries themselves

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
The existing representation cannot hold an intraday boundary at all:

* ``leave_requests.from_date`` / ``to_date`` are ``Column(DateTime)`` — NAIVE on
  PostgreSQL as well as SQLite — and every write path parses them from
  ``"%Y-%m-%d"``, so they are always local midnight with no time of day.
* ``half_day_period`` holds the string "first_half" or "second_half". Nothing in
  the backend ever converts it to clock times; it is a label, not an interval.

So there is nowhere to put a start and end instant, and no timezone-aware column
to put them in. Widening ``leave_requests`` was rejected: it would change the
authoritative meaning of columns that eight production modules read with
day-level overlap predicates, and it could not represent more than one interval
per request in any case.

PURELY ADDITIVE
---------------
No existing table is altered, renamed or dropped. No column is added to
``leave_requests``. No data is migrated, backfilled or synthesised — in
particular, NO intervals are manufactured for existing whole-day or half-day
requests. Doing so would require inventing which hours a half-day consumes,
which is a policy decision Steps 3-4 remain blocked on, and would write
fabricated evidence about days nobody recorded at this granularity.

ONE CONSTRAINT WITH A BEHAVIOURAL CONSEQUENCE
---------------------------------------------
``leave_interval_set.leave_request_id`` is ON DELETE RESTRICT. The existing
``DELETE /leave/{id}`` endpoint hard-deletes leave requests; once a request
carries a recorded interval set, that delete will be refused by the database
rather than silently destroying intraday history. No request has an interval set
today, so nothing changes until intervals are recorded. Raised explicitly in
section N of the Step 7 report.

MIGRATION PARENT
----------------
``down_revision = 'd4b7e2f9a615'`` — the current single head. Ordinary
single-parent child: no new head, no merge revision, no modification of existing
history.

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
revision: str = 'f1a6b9c4d283'
down_revision: Union[str, Sequence[str], None] = 'd4b7e2f9a615'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    # ══════════════════════════════════════════════════════════════════════
    # leave_interval_set — one recorded version
    # ══════════════════════════════════════════════════════════════════════
    if "leave_interval_set" not in existing:
        op.create_table(
            'leave_interval_set',
            sa.Column('id', sa.Integer(), nullable=False),
            # NOT NULL, deliberately stricter than leave_requests.organization_id
            # which is nullable. An interval whose tenant is unknown cannot be
            # isolated or audited; the service refuses to inherit a NULL.
            sa.Column('organization_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            # The canonical grant these boundaries describe. NOT NULL is what
            # stops this table becoming a second source of leave truth.
            sa.Column('leave_request_id', sa.Integer(), nullable=False),
            # Monotonic per request; also the optimistic-concurrency token.
            sa.Column('set_version', sa.Integer(), nullable=False),
            # Written once at insert, never updated. NULL on version 1.
            sa.Column('supersedes', sa.Integer(), nullable=True),
            # The zone in force at recording, so a later tenant timezone change
            # cannot retroactively move a boundary or re-attribute its date.
            sa.Column('timezone_iana', sa.String(length=64), nullable=False),
            sa.Column('recorded_at_utc', sa.DateTime(timezone=True), nullable=False),
            sa.Column('recorded_by', sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            # RESTRICT: intraday history must not vanish because the parent row
            # was hard-deleted. See the module docstring.
            sa.ForeignKeyConstraint(['leave_request_id'], ['leave_requests.id'],
                                    ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['supersedes'], ['leave_interval_set.id'],
                                    ondelete='RESTRICT'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('leave_request_id', 'set_version',
                                name='uq_leave_interval_set_version'),
            # Target of leave_interval's composite tenant foreign key.
            sa.UniqueConstraint('id', 'organization_id',
                                name='uq_leave_interval_set_id_org'),
            sa.CheckConstraint('set_version >= 1',
                               name='ck_leave_interval_set_version_positive'),
            sa.CheckConstraint('length(timezone_iana) > 0',
                               name='ck_leave_interval_set_timezone_nonempty'),
        )
        # No index on the primary key: it is already indexed on both backends,
        # and SQLAlchemy's auto-generated name for it collides exactly with the
        # one for leave_interval.set_id. Dropping the redundant index is the
        # fix; renaming around a collision would leave the redundancy in place.
        op.create_index(op.f('ix_leave_interval_set_organization_id'),
                        'leave_interval_set', ['organization_id'], unique=False)
        op.create_index(op.f('ix_leave_interval_set_user_id'),
                        'leave_interval_set', ['user_id'], unique=False)
        op.create_index(op.f('ix_leave_interval_set_leave_request_id'),
                        'leave_interval_set', ['leave_request_id'], unique=False)
        # The hot read: the current version for a request.
        op.create_index('ix_leave_interval_set_request_version',
                        'leave_interval_set',
                        ['leave_request_id', 'set_version'], unique=False)
        op.create_index('ix_leave_interval_set_org_user', 'leave_interval_set',
                        ['organization_id', 'user_id'], unique=False)

    # ══════════════════════════════════════════════════════════════════════
    # leave_interval — the boundaries
    # ══════════════════════════════════════════════════════════════════════
    if "leave_interval" not in existing:
        op.create_table(
            'leave_interval',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('set_id', sa.Integer(), nullable=False),
            # Half of the composite tenant FK below — load-bearing.
            sa.Column('organization_id', sa.Integer(), nullable=False),
            sa.Column('seq', sa.Integer(), nullable=False),
            # LOCAL calendar date, START_DATE attribution — the rule already in
            # force for the work-day ledger (§12.3), reused rather than
            # re-decided, so leave and work agree which day is meant. A true
            # DATE: it is a date in the organization's calendar, not an instant.
            sa.Column('work_date', sa.Date(), nullable=False),
            # Timezone-AWARE, unlike leave_requests.from_date/to_date. The naive
            # columns are what once failed a payroll run outright; see
            # app/payroll/service.py.
            sa.Column('start_at_utc', sa.DateTime(timezone=True), nullable=False),
            sa.Column('end_at_utc', sa.DateTime(timezone=True), nullable=False),
            # Integer seconds stored beside the instants so a historical figure
            # is verifiable without re-deriving it from timestamps whose zone
            # rules may since have changed.
            sa.Column('duration_seconds', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ['set_id', 'organization_id'],
                ['leave_interval_set.id', 'leave_interval_set.organization_id'],
                name='fk_leave_interval_set_org',
                ondelete='RESTRICT',
            ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('set_id', 'seq', name='uq_leave_interval_seq'),
            # Strictly positive: a zero-length leave interval is never
            # meaningful. An integer comparison is portable where a cross-dialect
            # timestamp comparison is not, which is why the end > start rule is
            # enforced here through the duration rather than on the instants.
            sa.CheckConstraint('duration_seconds > 0',
                               name='ck_leave_interval_duration_positive'),
            sa.CheckConstraint('seq >= 1', name='ck_leave_interval_seq_positive'),
        )
        # No primary-key index, and no standalone set_id index:
        # ix_leave_interval_order leads with set_id and serves those lookups.
        op.create_index(op.f('ix_leave_interval_organization_id'),
                        'leave_interval', ['organization_id'], unique=False)
        op.create_index(op.f('ix_leave_interval_work_date'), 'leave_interval',
                        ['work_date'], unique=False)
        op.create_index('ix_leave_interval_order', 'leave_interval',
                        ['set_id', 'start_at_utc', 'seq'], unique=False)
        op.create_index('ix_leave_interval_org_date', 'leave_interval',
                        ['organization_id', 'work_date'], unique=False)


def downgrade() -> None:
    """Drop only what this revision creates. Nothing pre-existing was changed."""
    op.drop_table('leave_interval')
    op.drop_table('leave_interval_set')
