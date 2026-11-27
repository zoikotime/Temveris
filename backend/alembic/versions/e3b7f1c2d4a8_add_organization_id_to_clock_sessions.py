"""add organization_id to clock_sessions"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision = 'e3b7f1c2d4a8'
down_revision = 'c7f4d8b2a391'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE clock_sessions ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE clock_sessions ADD CONSTRAINT fk_clock_sessions_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_clock_sessions_org_clockin ON clock_sessions (organization_id, clock_in_time)"))
    op.execute(sa.text("""
        UPDATE clock_sessions SET organization_id = users.organization_id
        FROM users
        WHERE clock_sessions.user_id = users.id AND users.organization_id IS NOT NULL
    """))


def downgrade() -> None:
    op.drop_index('ix_clock_sessions_org_clockin', table_name='clock_sessions')
    op.drop_constraint('fk_clock_sessions_organization_id', 'clock_sessions', type_='foreignkey')
    op.drop_column('clock_sessions', 'organization_id')
