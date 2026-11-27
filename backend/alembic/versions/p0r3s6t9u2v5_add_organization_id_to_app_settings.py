"""add organization_id to app_settings"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'p0r3s6t9u2v5'
down_revision: Union[str, None] = 'o9q2r5s8t1u4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_app_settings_key"))
    op.execute(sa.text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE app_settings ADD CONSTRAINT fk_app_settings_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE app_settings
        SET organization_id = (SELECT id FROM organizations ORDER BY id ASC LIMIT 1)
        WHERE organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_settings_organization_id ON app_settings (organization_id)"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE app_settings ADD CONSTRAINT uq_app_settings_org_key UNIQUE (organization_id, key);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))


def downgrade() -> None:
    op.drop_constraint('uq_app_settings_org_key', 'app_settings', type_='unique')
    op.drop_index('ix_app_settings_organization_id', table_name='app_settings')
    op.drop_constraint('fk_app_settings_organization_id', 'app_settings', type_='foreignkey')
    op.drop_column('app_settings', 'organization_id')
    op.create_index(op.f('ix_app_settings_key'), 'app_settings', ['key'], unique=True)
