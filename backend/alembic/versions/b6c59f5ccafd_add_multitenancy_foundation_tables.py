"""add multitenancy foundation tables

Revision ID: b6c59f5ccafd
Revises: 81c63dee733a
Create Date: 2026-05-17 12:12:56.590500

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b6c59f5ccafd'
down_revision: Union[str, Sequence[str], None] = '81c63dee733a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS organizations (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_organizations_id ON organizations (id)"))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            isolation_mode VARCHAR(50),
            status VARCHAR(50),
            region_code VARCHAR(50),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_tenants_id ON tenants (id)"))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id SERIAL PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_workspaces_id ON workspaces (id)"))

    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS memberships (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            role VARCHAR(100) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_memberships_id ON memberships (id)"))
   