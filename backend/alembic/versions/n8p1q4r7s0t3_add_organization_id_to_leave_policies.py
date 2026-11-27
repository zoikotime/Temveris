"""add organization_id to leave_policies"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'n8p1q4r7s0t3'
down_revision: Union[str, None] = 'm7h0i3j6k9l2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE leave_policies ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE leave_policies ADD CONSTRAINT fk_leave_policies_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE leave_policies
        SET organization_id = (SELECT id FROM organizations ORDER BY id ASC LIMIT 1)
        WHERE organization_id IS NULL
    """))
    op.execute(sa.text("ALTER TABLE leave_policies DROP CONSTRAINT IF EXISTS leave_policies_leave_type_key"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE leave_policies ADD CONSTRAINT uq_leave_policies_org_type
            UNIQUE (organization_id, leave_type);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_leave_policies_organization_id ON leave_policies (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_leave_policies_organization_id', table_name='leave_policies')
    op.drop_constraint('uq_leave_policies_org_type', 'leave_policies', type_='unique')
    op.create_unique_constraint('leave_policies_leave_type_key', 'leave_policies', ['leave_type'])
    op.drop_constraint('fk_leave_policies_organization_id', 'leave_policies', type_='foreignkey')
    op.drop_column('leave_policies', 'organization_id')
