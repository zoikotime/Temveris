"""add organization_id to users"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision = 'e46ddafd1cbd'
down_revision = 'b6c59f5ccafd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE users ADD CONSTRAINT fk_users_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))


def downgrade() -> None:

    op.drop_constraint(None, 'users', type_='foreignkey')

    op.drop_column('users', 'organization_id')