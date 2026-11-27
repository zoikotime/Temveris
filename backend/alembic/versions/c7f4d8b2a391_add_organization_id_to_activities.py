"""add organization_id to activities"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision = 'c7f4d8b2a391'
down_revision = 'e46ddafd1cbd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE activities ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE activities ADD CONSTRAINT fk_activities_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_activities_org_ts ON activities (organization_id, timestamp)"))
    op.execute(sa.text("""
        UPDATE activities SET organization_id = users.organization_id
        FROM users
        WHERE activities.user_id = users.id AND users.organization_id IS NOT NULL
    """))


def downgrade() -> None:
    op.drop_index('ix_activities_org_ts', table_name='activities')
    op.drop_constraint('fk_activities_organization_id', 'activities', type_='foreignkey')
    op.drop_column('activities', 'organization_id')
