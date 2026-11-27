"""add organization_id to teams"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'o9q2r5s8t1u4'
down_revision: Union[str, None] = 'n8p1q4r7s0t3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_teams_name"))
    op.execute(sa.text("ALTER TABLE teams ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE teams ADD CONSTRAINT fk_teams_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE teams
        SET organization_id = (SELECT id FROM organizations ORDER BY id ASC LIMIT 1)
        WHERE organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_teams_organization_id ON teams (organization_id)"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE teams ADD CONSTRAINT uq_teams_org_name UNIQUE (organization_id, name);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))


def downgrade() -> None:
    op.drop_constraint('uq_teams_org_name', 'teams', type_='unique')
    op.drop_index('ix_teams_organization_id', table_name='teams')
    op.drop_constraint('fk_teams_organization_id', 'teams', type_='foreignkey')
    op.drop_column('teams', 'organization_id')
    op.create_index(op.f('ix_teams_name'), 'teams', ['name'], unique=True)
