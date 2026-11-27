"""add organization_id to payroll_configs, payroll_runs, payroll_items"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2f4a8c3d1b6'
down_revision: Union[str, None] = 'd3e5f7a2c1b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── payroll_configs ───────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE payroll_configs ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE payroll_configs ADD CONSTRAINT fk_payroll_configs_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE payroll_configs SET organization_id = users.organization_id
        FROM users
        WHERE payroll_configs.user_id = users.id AND payroll_configs.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_payroll_configs_organization_id ON payroll_configs (organization_id)"))

    # ── payroll_runs ──────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE payroll_runs ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE payroll_runs ADD CONSTRAINT fk_payroll_runs_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE payroll_runs SET organization_id = users.organization_id
        FROM users
        WHERE payroll_runs.created_by = users.id AND payroll_runs.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_payroll_runs_organization_id ON payroll_runs (organization_id)"))

    # ── payroll_items ─────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE payroll_items ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE payroll_items ADD CONSTRAINT fk_payroll_items_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE payroll_items SET organization_id = users.organization_id
        FROM users
        WHERE payroll_items.user_id = users.id AND payroll_items.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_payroll_items_organization_id ON payroll_items (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_payroll_items_organization_id',  table_name='payroll_items')
    op.drop_constraint('fk_payroll_items_organization_id',  'payroll_items',  type_='foreignkey')
    op.drop_column('payroll_items', 'organization_id')

    op.drop_index('ix_payroll_runs_organization_id',   table_name='payroll_runs')
    op.drop_constraint('fk_payroll_runs_organization_id',   'payroll_runs',   type_='foreignkey')
    op.drop_column('payroll_runs', 'organization_id')

    op.drop_index('ix_payroll_configs_organization_id', table_name='payroll_configs')
    op.drop_constraint('fk_payroll_configs_organization_id', 'payroll_configs', type_='foreignkey')
    op.drop_column('payroll_configs', 'organization_id')
