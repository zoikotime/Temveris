"""add organization_id to leave_requests"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd3e5f7a2c1b8'
down_revision: Union[str, None] = 'a4c8f2e1b7d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE leave_requests ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE leave_requests ADD CONSTRAINT fk_leave_requests_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE leave_requests SET organization_id = users.organization_id
        FROM users
        WHERE leave_requests.user_id = users.id AND leave_requests.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_leave_requests_organization_id ON leave_requests (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_leave_requests_organization_id', table_name='leave_requests')
    op.drop_constraint('fk_leave_requests_organization_id', 'leave_requests', type_='foreignkey')
    op.drop_column('leave_requests', 'organization_id')
