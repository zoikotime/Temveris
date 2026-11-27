"""Add organization_id to security center tables for tenant isolation.

Adds organization_id FK to:
  - security_trusted_domains
  - security_exceptions
  - security_break_glass_accounts

Replaces the platform-wide unique constraints on:
  - security_trusted_domains.domain       → (organization_id, domain)
  - security_break_glass_accounts.email   → (organization_id, email)

Existing rows receive organization_id=NULL so super_admin can still
see legacy data; new rows always carry an org_id from the endpoint.

Revision ID: f2a1b3c4d5e6
Revises: e1f7a2c4d093
Create Date: 2026-06-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f2a1b3c4d5e6"
down_revision: Union[str, None] = "e1f7a2c4d093"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── security_trusted_domains ─────────────────────────────────────────
    op.execute(sa.text(
        "ALTER TABLE security_trusted_domains ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_security_trusted_domains_org ON security_trusted_domains (organization_id)"
    ))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_security_trusted_domains_domain"))
    op.execute(sa.text(
        "ALTER TABLE security_trusted_domains DROP CONSTRAINT IF EXISTS uq_security_trusted_domains_domain"
    ))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE security_trusted_domains
                ADD CONSTRAINT uq_trusted_domain_org_domain UNIQUE (organization_id, domain);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    # ── security_exceptions ───────────────────────────────────────────────
    op.execute(sa.text(
        "ALTER TABLE security_exceptions ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_security_exceptions_org ON security_exceptions (organization_id)"
    ))

    # ── security_break_glass_accounts ─────────────────────────────────────
    op.execute(sa.text(
        "ALTER TABLE security_break_glass_accounts ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_security_break_glass_accounts_org ON security_break_glass_accounts (organization_id)"
    ))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_security_break_glass_accounts_email"))
    op.execute(sa.text(
        "ALTER TABLE security_break_glass_accounts DROP CONSTRAINT IF EXISTS uq_security_break_glass_accounts_email"
    ))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE security_break_glass_accounts
                ADD CONSTRAINT uq_break_glass_org_email UNIQUE (organization_id, email);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))


def downgrade() -> None:
    # ── security_break_glass_accounts ───────────────────────────────────
    op.drop_constraint("uq_break_glass_org_email", "security_break_glass_accounts", type_="unique")
    op.drop_index("ix_security_break_glass_accounts_org", table_name="security_break_glass_accounts")
    with op.batch_alter_table("security_break_glass_accounts") as batch:
        batch.drop_column("organization_id")
    op.create_index(
        "ix_security_break_glass_accounts_email",
        "security_break_glass_accounts",
        ["email"],
        unique=True,
    )

    # ── security_exceptions ─────────────────────────────────────────────
    op.drop_index("ix_security_exceptions_org", table_name="security_exceptions")
    with op.batch_alter_table("security_exceptions") as batch:
        batch.drop_column("organization_id")

    # ── security_trusted_domains ────────────────────────────────────────
    op.drop_constraint("uq_trusted_domain_org_domain", "security_trusted_domains", type_="unique")
    op.drop_index("ix_security_trusted_domains_org", table_name="security_trusted_domains")
    with op.batch_alter_table("security_trusted_domains") as batch:
        batch.drop_column("organization_id")
    op.create_index(
        "ix_security_trusted_domains_domain",
        "security_trusted_domains",
        ["domain"],
        unique=True,
    )
