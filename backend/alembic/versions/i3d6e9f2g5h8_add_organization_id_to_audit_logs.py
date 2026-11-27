"""add organization_id to audit_logs for per-tenant notification isolation"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'i3d6e9f2g5h8'
down_revision: Union[str, None] = 'h2c5d8e1f4a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE audit_logs ADD CONSTRAINT fk_audit_logs_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE audit_logs SET organization_id = users.organization_id
        FROM users
        WHERE audit_logs.actor_id = users.id AND audit_logs.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_audit_logs_organization_id ON audit_logs (organization_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_audit_logs_org_entity ON audit_logs (organization_id, entity)"))


def downgrade() -> None:
    op.drop_index('ix_audit_logs_org_entity',     table_name='audit_logs')
    op.drop_index('ix_audit_logs_organization_id', table_name='audit_logs')
    op.drop_constraint('fk_audit_logs_organization_id', 'audit_logs', type_='foreignkey')
    op.drop_column('audit_logs', 'organization_id')
