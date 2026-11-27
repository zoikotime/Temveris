"""add organization_id to holidays"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a4c8f2e1b7d9'
down_revision: Union[str, None] = 'e3b7f1c2d4a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE holidays ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE holidays ADD CONSTRAINT fk_holidays_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE holidays
        SET organization_id = (SELECT id FROM organizations ORDER BY id LIMIT 1)
        WHERE organization_id IS NULL
    """))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_holidays_date"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE holidays ADD CONSTRAINT uq_holidays_org_date UNIQUE (organization_id, date);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_holidays_organization_id ON holidays (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_holidays_organization_id', table_name='holidays')
    op.drop_constraint('uq_holidays_org_date', 'holidays', type_='unique')
    # Restore the original platform-wide unique index on date
    op.create_index('ix_holidays_date', 'holidays', ['date'], unique=True)
    op.drop_constraint('fk_holidays_organization_id', 'holidays', type_='foreignkey')
    op.drop_column('holidays', 'organization_id')
