"""add_operational_org_id_to_commercial_organizations

Revision ID: c37cc72db12d
Revises: p0r3s6t9u2v5
Create Date: 2026-05-19 16:21:04.054754

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c37cc72db12d'
down_revision: Union[str, Sequence[str], None] = 'q1r4s7t0u3v6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(
        "ALTER TABLE commercial_organizations ADD COLUMN IF NOT EXISTS operational_org_id INTEGER NULL"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_commercial_organizations_operational_org_id "
        "ON commercial_organizations (operational_org_id)"
    ))


def downgrade() -> None:
    op.drop_index('ix_commercial_organizations_operational_org_id', table_name='commercial_organizations')
    op.drop_column('commercial_organizations', 'operational_org_id')
