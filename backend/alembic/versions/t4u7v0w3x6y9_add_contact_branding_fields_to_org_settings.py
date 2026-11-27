"""add_contact_branding_fields_to_org_settings

Adds hr_email, support_email, company_address, company_phone,
company_website, company_description, primary_brand_color,
secondary_brand_color to org_settings.

Revision ID: t4u7v0w3x6y9
Revises: s3t6u9v2w5x8
Create Date: 2026-05-28 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 't4u7v0w3x6y9'
down_revision: Union[str, Sequence[str], None] = 's3t6u9v2w5x8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS hr_email VARCHAR(255) NULL"))
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS support_email VARCHAR(255) NULL"))
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS company_address TEXT NULL"))
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS company_phone VARCHAR(50) NULL"))
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS company_website VARCHAR(255) NULL"))
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS company_description TEXT NULL"))
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS primary_brand_color VARCHAR(7) NULL"))
    op.execute(sa.text("ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS secondary_brand_color VARCHAR(7) NULL"))


def downgrade() -> None:
    op.drop_column("org_settings", "secondary_brand_color")
    op.drop_column("org_settings", "primary_brand_color")
    op.drop_column("org_settings", "company_description")
    op.drop_column("org_settings", "company_website")
    op.drop_column("org_settings", "company_phone")
    op.drop_column("org_settings", "company_address")
    op.drop_column("org_settings", "support_email")
    op.drop_column("org_settings", "hr_email")
