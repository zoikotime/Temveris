"""add organization_id to org_settings"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'm7h0i3j6k9l2'
down_revision: Union[str, None] = 'l6g9h2i5j8k1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE org_settings ADD CONSTRAINT fk_org_settings_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE org_settings SET organization_id = users.organization_id
        FROM users WHERE org_settings.owner_user_id = users.id AND org_settings.organization_id IS NULL
    """))
    op.execute(sa.text("""
        UPDATE org_settings
        SET organization_id = (SELECT id FROM organizations ORDER BY id ASC LIMIT 1)
        WHERE organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_org_settings_organization_id ON org_settings (organization_id)"))


def downgrade() -> None:
    op.drop_index('ix_org_settings_organization_id', table_name='org_settings')
    op.drop_constraint('fk_org_settings_organization_id', 'org_settings', type_='foreignkey')
    op.drop_column('org_settings', 'organization_id')
