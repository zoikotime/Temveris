"""add organization_id to break_sessions"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'g1b4c7d2e5f8'
down_revision: Union[str, None] = 'f9a1b3c8d5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE break_sessions ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE break_sessions ADD CONSTRAINT fk_break_sessions_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE break_sessions SET organization_id = users.organization_id
        FROM users
        WHERE break_sessions.user_id = users.id AND break_sessions.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_break_sessions_organization_id ON break_sessions (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_break_sessions_organization_id', table_name='break_sessions')
    op.drop_constraint('fk_break_sessions_organization_id', 'break_sessions', type_='foreignkey')
    op.drop_column('break_sessions', 'organization_id')
