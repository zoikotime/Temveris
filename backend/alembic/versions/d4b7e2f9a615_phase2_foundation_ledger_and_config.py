"""phase 2 foundation: sealed work-state ledger and immutable versioned configuration

Revision ID: d4b7e2f9a615
Revises: e4f7a2b91c60
Create Date: 2026-09-03 00:00:00.000000

Creates the four tables of the owner-authorised Phase-2 foundation:

    work_day_ledger      (Step 1)  one sealable Worker-day
    work_state_interval  (Step 1/2) sealed state intervals, provenance required
    config_version       (Step 6)  one immutable configuration capture
    config_value         (Step 6)  values frozen into exactly one version

PURELY ADDITIVE
---------------
No existing table is altered, renamed or dropped. No column is added to an
existing table. No data is migrated, backfilled or synthesised. ``app_settings``
is untouched and keeps its meaning ("the configuration in force right now");
``config_version`` answers the different question these tables exist for ("what
was in force on that date?"), which a mutate-in-place store cannot.

There is deliberately NO backfill of configuration history and NO reconstruction
of historical ledger intervals. Both would be fabricated evidence about periods
nobody captured. A date preceding the first captured version resolves to "no
version", and the strict resolver declines to evaluate that day — which is the
correct answer, not a gap.

MIGRATION PARENT
----------------
``down_revision = 'e4f7a2b91c60'`` — the single head. An earlier audit reported
four heads; that was a parser error which could not read the tuple-valued
``down_revision`` of the existing merge ``z9f8e7d6c5b4``. Verified by AST parse
and by Alembic's own ScriptDirectory: 61 revisions, root ``81c63dee733a``, head
``e4f7a2b91c60``, one legitimate three-parent merge. This revision is an
ordinary single-parent child of that head: no new head, no merge revision, no
modification of existing history.

ALREADY-PROVISIONED DATABASES
-----------------------------
The test suite materialises these tables with ``create_all``. Each table is
guarded by an existence check, following the
``f0e1d2c3b4a5_add_missing_commercial_tables`` precedent, so applying this
revision to a database that already has them is a no-op rather than a
"table already exists" failure.

NOT EXECUTED
------------
This revision has not been applied to any database. The repository ``.env``
points at production, so no command that executes ``env.py`` was run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4b7e2f9a615'
down_revision: Union[str, Sequence[str], None] = 'e4f7a2b91c60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    # ══════════════════════════════════════════════════════════════════════
    # STEP 1 — work_day_ledger
    # ══════════════════════════════════════════════════════════════════════
    if "work_day_ledger" not in existing:
        op.create_table(
            'work_day_ledger',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('organization_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            # Local work date the day is attributed to (attribution = START_DATE).
            # Naive by design: it is a calendar date in the organization's zone,
            # and the zone in force is persisted separately in timezone_iana.
            sa.Column('work_date', sa.DateTime(timezone=False), nullable=False),
            sa.Column('workweek_key', sa.String(length=16), nullable=False),
            # Optimistic-concurrency token. A write against a stale version is
            # rejected and re-run, never merged (§22.2).
            sa.Column('ledger_version', sa.Integer(), nullable=False),
            # LedgerState value as text, not a DB enum: the platform convention
            # is String + application validation, and an enum would need a
            # migration for every future state (§32).
            sa.Column('state', sa.String(length=32), nullable=False),
            sa.Column('integrity_fault_reason', sa.Text(), nullable=True),
            sa.Column('sealed_at', sa.DateTime(timezone=True), nullable=True),
            # Persisted so a later organization timezone change cannot
            # retroactively re-attribute a sealed day (§12.2).
            sa.Column('timezone_iana', sa.String(length=64), nullable=False),
            sa.Column('utc_offset_seconds', sa.Integer(), nullable=False),
            # Supersession chain (§23). Corrections supersede; never mutate.
            sa.Column('supersedes', sa.Integer(), nullable=True),
            sa.Column('superseded_by', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_by', sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['supersedes'], ['work_day_ledger.id'],
                                    ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['superseded_by'], ['work_day_ledger.id'],
                                    ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            # Re-running a seal at the same version is a no-op, not a second
            # row (§22.1).
            sa.UniqueConstraint('organization_id', 'user_id', 'work_date',
                                'ledger_version',
                                name='uq_work_day_ledger_natural_key'),
            sa.CheckConstraint('ledger_version >= 1',
                               name='ck_work_day_ledger_version_positive'),
        )
        op.create_index(op.f('ix_work_day_ledger_id'), 'work_day_ledger',
                        ['id'], unique=False)
        op.create_index(op.f('ix_work_day_ledger_organization_id'),
                        'work_day_ledger', ['organization_id'], unique=False)
        op.create_index(op.f('ix_work_day_ledger_user_id'), 'work_day_ledger',
                        ['user_id'], unique=False)
        op.create_index(op.f('ix_work_day_ledger_work_date'), 'work_day_ledger',
                        ['work_date'], unique=False)
        op.create_index(op.f('ix_work_day_ledger_workweek_key'),
                        'work_day_ledger', ['workweek_key'], unique=False)
        op.create_index(op.f('ix_work_day_ledger_state'), 'work_day_ledger',
                        ['state'], unique=False)
        op.create_index(op.f('ix_work_day_ledger_superseded_by'),
                        'work_day_ledger', ['superseded_by'], unique=False)
        # The hot read: the current (non-superseded) row for a Worker-day.
        op.create_index('ix_work_day_ledger_current', 'work_day_ledger',
                        ['organization_id', 'user_id', 'work_date',
                         'superseded_by'], unique=False)
        op.create_index('ix_work_day_ledger_org_week', 'work_day_ledger',
                        ['organization_id', 'workweek_key'], unique=False)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 1/2 — work_state_interval
    # ══════════════════════════════════════════════════════════════════════
    if "work_state_interval" not in existing:
        op.create_table(
            'work_state_interval',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('ledger_id', sa.Integer(), nullable=False),
            # Denormalised for tenant-scoped queries that never want to join.
            sa.Column('organization_id', sa.Integer(), nullable=False),
            # Deterministic ordering tiebreaker: two intervals may legitimately
            # share a start instant, and INV-8 forbids order depending on
            # iteration.
            sa.Column('seq', sa.Integer(), nullable=False),
            # WorkState value: WRK / TRV / ONC / BRK_M / BRK_R / OFF / LVE / UNK.
            sa.Column('state', sa.String(length=8), nullable=False),
            sa.Column('start_at_utc', sa.DateTime(timezone=True), nullable=False),
            sa.Column('end_at_utc', sa.DateTime(timezone=True), nullable=False),
            # Integer seconds stored alongside the boundaries so a historical
            # figure is verifiable without re-deriving it from timestamps whose
            # timezone rules may since have changed. No rounding here (§16.2).
            sa.Column('duration_seconds', sa.Integer(), nullable=False),
            # NOT NULL by owner ruling at Gate 6, extended by correction C-02 to
            # every interval. No interval may exist without provenance, and it
            # is immutable — a change occurs only by superseding the ledger
            # version that carries it.
            sa.Column('provenance', sa.String(length=32), nullable=False),
            sa.Column('source_ref', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['ledger_id'], ['work_day_ledger.id'],
                                    ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('ledger_id', 'seq',
                                name='uq_work_state_interval_seq'),
            sa.CheckConstraint('duration_seconds >= 0',
                               name='ck_work_state_interval_duration'),
        )
        op.create_index(op.f('ix_work_state_interval_id'),
                        'work_state_interval', ['id'], unique=False)
        op.create_index(op.f('ix_work_state_interval_ledger_id'),
                        'work_state_interval', ['ledger_id'], unique=False)
        op.create_index(op.f('ix_work_state_interval_organization_id'),
                        'work_state_interval', ['organization_id'], unique=False)
        op.create_index(op.f('ix_work_state_interval_state'),
                        'work_state_interval', ['state'], unique=False)
        op.create_index(op.f('ix_work_state_interval_provenance'),
                        'work_state_interval', ['provenance'], unique=False)
        op.create_index('ix_work_state_interval_order', 'work_state_interval',
                        ['ledger_id', 'start_at_utc', 'seq'], unique=False)
        op.create_index('ix_work_state_interval_org_state',
                        'work_state_interval', ['organization_id', 'state'],
                        unique=False)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 6 — config_version
    # ══════════════════════════════════════════════════════════════════════
    if "config_version" not in existing:
        op.create_table(
            'config_version',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('organization_id', sa.Integer(), nullable=False),
            # Stable public identifier: crosses the Step 5 port and must survive
            # a database rebuild, which the surrogate id would not.
            sa.Column('version_key', sa.String(length=64), nullable=False),
            # Inclusive lower bound. A DATE, not an instant: policy takes effect
            # on a day in the organization's calendar.
            sa.Column('effective_from', sa.Date(), nullable=False),
            # When the capture happened — audit only. Never used to select a
            # version, so two captures made in either order for the same
            # effective date resolve identically (INV-8).
            sa.Column('captured_at_utc', sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column('captured_by_user_id', sa.Integer(), nullable=True),
            # SHA-256 over identity + canonical values, for INV-8 verification.
            sa.Column('content_hash', sa.String(length=64), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'],
                                    ondelete='RESTRICT'),
            # SET NULL: removing a person must never remove configuration
            # history.
            sa.ForeignKeyConstraint(['captured_by_user_id'], ['users.id'],
                                    ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
            # Two versions cannot claim the same start date: "which version
            # governed that day" must have exactly one answer.
            sa.UniqueConstraint('organization_id', 'effective_from',
                                name='uq_config_version_org_effective_from'),
            sa.UniqueConstraint('organization_id', 'version_key',
                                name='uq_config_version_org_version_key'),
            # Redundant as a key (id alone is unique); declared solely as the
            # target of config_value's composite tenant FK, so a value row
            # cannot be attached to another organization's version.
            sa.UniqueConstraint('id', 'organization_id',
                                name='uq_config_version_id_org'),
            sa.CheckConstraint('length(version_key) > 0',
                               name='ck_config_version_key_nonempty'),
            sa.CheckConstraint('length(content_hash) > 0',
                               name='ck_config_version_hash_nonempty'),
        )
        op.create_index(op.f('ix_config_version_id'), 'config_version',
                        ['id'], unique=False)
        op.create_index(op.f('ix_config_version_organization_id'),
                        'config_version', ['organization_id'], unique=False)
        # DESC on effective_from: the only hot read is "latest version at or
        # before this date", i.e. ORDER BY effective_from DESC LIMIT 1. Raw SQL
        # because a declarative Index cannot express column ordering portably;
        # the ORM model declares the ascending equivalent under the same name
        # for create_all parity, and this migration is authoritative.
        op.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_config_version_org_effective "
            "ON config_version (organization_id, effective_from DESC)"
        ))

    # ══════════════════════════════════════════════════════════════════════
    # STEP 6 — config_value
    # ══════════════════════════════════════════════════════════════════════
    if "config_value" not in existing:
        op.create_table(
            'config_value',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('config_version_id', sa.Integer(), nullable=False),
            # Half of the composite FK below — load-bearing, not decorative.
            sa.Column('organization_id', sa.Integer(), nullable=False),
            # NULL = organization-wide.
            sa.Column('scope_user_id', sa.Integer(), nullable=True),
            # §10.1 precedence label, carried VERBATIM. Not renamed to ORG_*,
            # and deliberately not a database enum (§32).
            sa.Column('tier', sa.String(length=64), nullable=False),
            # Precedence order, lowest wins. Stored rather than derived from
            # tier so a version records the precedence actually in force.
            sa.Column('rank', sa.Integer(), nullable=False),
            sa.Column('key', sa.String(length=128), nullable=False),
            # NULL = tier configured but silent, which is different from a tier
            # supplying an invalid value.
            sa.Column('value_minutes', sa.Integer(), nullable=True),
            # Provenance for the explanation right (INV-10).
            sa.Column('source_ref', sa.String(length=255), nullable=True),
            # No created_at: a value is captured with its version, so
            # config_version.captured_at_utc already answers "when".
            # Composite tenant FK. RESTRICT: a version carrying values cannot be
            # deleted out from under them; configuration history is not
            # disposable.
            sa.ForeignKeyConstraint(
                ['config_version_id', 'organization_id'],
                ['config_version.id', 'config_version.organization_id'],
                name='fk_config_value_version_org',
                ondelete='RESTRICT',
            ),
            sa.ForeignKeyConstraint(['scope_user_id'], ['users.id'],
                                    ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            # No plain UniqueConstraint over the scope columns: NULL never
            # equals NULL in a unique index on PostgreSQL OR SQLite, so it
            # would not stop two organization-wide rows for the same tier and
            # key. The COALESCE expression index below is the real rule, and
            # the ORM model declares the same index so create_all-built
            # databases are governed identically.
            # Mirrors the Step 5 resolver bounds, so an out-of-range value
            # cannot be stored rather than only being rejected at read time.
            sa.CheckConstraint(
                'value_minutes IS NULL OR '
                '(value_minutes >= 1 AND value_minutes <= 1440)',
                name='ck_config_value_minutes_range'),
            sa.CheckConstraint('rank >= 0',
                               name='ck_config_value_rank_nonnegative'),
            sa.CheckConstraint('length(tier) > 0',
                               name='ck_config_value_tier_nonempty'),
        )
        op.create_index(op.f('ix_config_value_id'), 'config_value',
                        ['id'], unique=False)
        op.create_index(op.f('ix_config_value_config_version_id'),
                        'config_value', ['config_version_id'], unique=False)
        op.create_index(op.f('ix_config_value_organization_id'),
                        'config_value', ['organization_id'], unique=False)
        op.create_index('ix_config_value_version_scope', 'config_value',
                        ['config_version_id', 'scope_user_id'], unique=False)
        # The authoritative uniqueness rule. COALESCE collapses NULL scope to a
        # single value so two organization-wide rows for one tier/key collide as
        # they should. Portable across PostgreSQL and SQLite (>= 3.9).
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_config_value_scope "
            "ON config_value "
            "(config_version_id, COALESCE(scope_user_id, 0), tier, key)"
        ))


def downgrade() -> None:
    """Drop only what this revision creates. Nothing pre-existing is restored,
    because nothing pre-existing was changed."""
    op.execute(sa.text("DROP INDEX IF EXISTS uq_config_value_scope"))
    op.drop_table('config_value')
    op.execute(sa.text("DROP INDEX IF EXISTS ix_config_version_org_effective"))
    op.drop_table('config_version')
    op.drop_table('work_state_interval')
    op.drop_table('work_day_ledger')
