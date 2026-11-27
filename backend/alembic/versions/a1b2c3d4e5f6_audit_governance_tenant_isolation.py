"""Add organization_id to audit governance tables for tenant isolation.

Adds organization_id FK to:
  - audit_policy_versions
  - audit_data_exports
  - audit_evidence_packages
  - audit_legal_holds
  - audit_retention_rules

Replaces platform-wide unique constraints with per-org composite unique constraints:
  - audit_evidence_packages.package_code  → (organization_id, package_code)
  - audit_legal_holds.matter_ref          → (organization_id, matter_ref)
  - audit_retention_rules.rule_key        → (organization_id, rule_key)

All DROP operations use IF EXISTS raw SQL so the migration is idempotent and
never poisons the PostgreSQL transaction regardless of the existing schema state.

Revision ID: a1b2c3d4e5f6
Revises: f2a1b3c4d5e6
Create Date: 2026-06-02
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f2a1b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── audit_policy_versions ───────────────────────────────────────────
    conn.execute(sa.text(
        "ALTER TABLE audit_policy_versions "
        "ADD COLUMN IF NOT EXISTS organization_id INTEGER "
        "REFERENCES organizations(id) ON DELETE SET NULL"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_audit_policy_versions_org "
        "ON audit_policy_versions (organization_id)"
    ))

    # ── audit_data_exports ───────────────────────────────────────────────
    conn.execute(sa.text(
        "ALTER TABLE audit_data_exports "
        "ADD COLUMN IF NOT EXISTS organization_id INTEGER "
        "REFERENCES organizations(id) ON DELETE SET NULL"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_audit_data_exports_org "
        "ON audit_data_exports (organization_id)"
    ))

    # ── audit_evidence_packages ──────────────────────────────────────────
    conn.execute(sa.text(
        "ALTER TABLE audit_evidence_packages "
        "ADD COLUMN IF NOT EXISTS organization_id INTEGER "
        "REFERENCES organizations(id) ON DELETE SET NULL"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_audit_evidence_packages_org "
        "ON audit_evidence_packages (organization_id)"
    ))
    # Drop any existing unique constraint on package_code (by any auto-generated name).
    conn.execute(sa.text("""
        DO $$ DECLARE r RECORD;
        BEGIN
          FOR r IN SELECT constraint_name FROM information_schema.table_constraints
                   WHERE table_name='audit_evidence_packages'
                   AND constraint_type='UNIQUE'
                   AND constraint_name NOT IN ('uq_evidence_org_code')
          LOOP
            EXECUTE 'ALTER TABLE audit_evidence_packages DROP CONSTRAINT IF EXISTS ' || r.constraint_name;
          END LOOP;
        END $$
    """))
    conn.execute(sa.text(
        "DROP INDEX IF EXISTS ix_audit_evidence_packages_package_code"
    ))
    conn.execute(sa.text(
        "ALTER TABLE audit_evidence_packages "
        "DROP CONSTRAINT IF EXISTS uq_evidence_org_code"
    ))
    conn.execute(sa.text(
        "ALTER TABLE audit_evidence_packages "
        "ADD CONSTRAINT uq_evidence_org_code "
        "UNIQUE (organization_id, package_code)"
    ))

    # ── audit_legal_holds ────────────────────────────────────────────────
    conn.execute(sa.text(
        "ALTER TABLE audit_legal_holds "
        "ADD COLUMN IF NOT EXISTS organization_id INTEGER "
        "REFERENCES organizations(id) ON DELETE SET NULL"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_audit_legal_holds_org "
        "ON audit_legal_holds (organization_id)"
    ))
    conn.execute(sa.text("""
        DO $$ DECLARE r RECORD;
        BEGIN
          FOR r IN SELECT constraint_name FROM information_schema.table_constraints
                   WHERE table_name='audit_legal_holds'
                   AND constraint_type='UNIQUE'
                   AND constraint_name NOT IN ('uq_legal_hold_org_matter')
          LOOP
            EXECUTE 'ALTER TABLE audit_legal_holds DROP CONSTRAINT IF EXISTS ' || r.constraint_name;
          END LOOP;
        END $$
    """))
    conn.execute(sa.text(
        "DROP INDEX IF EXISTS ix_audit_legal_holds_matter_ref"
    ))
    conn.execute(sa.text(
        "ALTER TABLE audit_legal_holds "
        "DROP CONSTRAINT IF EXISTS uq_legal_hold_org_matter"
    ))
    conn.execute(sa.text(
        "ALTER TABLE audit_legal_holds "
        "ADD CONSTRAINT uq_legal_hold_org_matter "
        "UNIQUE (organization_id, matter_ref)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_audit_legal_holds_matter_ref "
        "ON audit_legal_holds (matter_ref)"
    ))

    # ── audit_retention_rules ────────────────────────────────────────────
    conn.execute(sa.text(
        "ALTER TABLE audit_retention_rules "
        "ADD COLUMN IF NOT EXISTS organization_id INTEGER "
        "REFERENCES organizations(id) ON DELETE SET NULL"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_audit_retention_rules_org "
        "ON audit_retention_rules (organization_id)"
    ))
    conn.execute(sa.text("""
        DO $$ DECLARE r RECORD;
        BEGIN
          FOR r IN SELECT constraint_name FROM information_schema.table_constraints
                   WHERE table_name='audit_retention_rules'
                   AND constraint_type='UNIQUE'
                   AND constraint_name NOT IN ('uq_retention_org_key')
          LOOP
            EXECUTE 'ALTER TABLE audit_retention_rules DROP CONSTRAINT IF EXISTS ' || r.constraint_name;
          END LOOP;
        END $$
    """))
    conn.execute(sa.text(
        "DROP INDEX IF EXISTS ix_audit_retention_rules_rule_key"
    ))
    conn.execute(sa.text(
        "ALTER TABLE audit_retention_rules "
        "DROP CONSTRAINT IF EXISTS uq_retention_org_key"
    ))
    conn.execute(sa.text(
        "ALTER TABLE audit_retention_rules "
        "ADD CONSTRAINT uq_retention_org_key "
        "UNIQUE (organization_id, rule_key)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_audit_retention_rules_rule_key "
        "ON audit_retention_rules (rule_key)"
    ))


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("ALTER TABLE audit_retention_rules DROP CONSTRAINT IF EXISTS uq_retention_org_key"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_audit_retention_rules_rule_key"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_audit_retention_rules_org"))
    conn.execute(sa.text("ALTER TABLE audit_retention_rules DROP COLUMN IF EXISTS organization_id"))
    conn.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_retention_rules_rule_key ON audit_retention_rules (rule_key)"))

    conn.execute(sa.text("ALTER TABLE audit_legal_holds DROP CONSTRAINT IF EXISTS uq_legal_hold_org_matter"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_audit_legal_holds_matter_ref"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_audit_legal_holds_org"))
    conn.execute(sa.text("ALTER TABLE audit_legal_holds DROP COLUMN IF EXISTS organization_id"))
    conn.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_legal_holds_matter_ref ON audit_legal_holds (matter_ref)"))

    conn.execute(sa.text("ALTER TABLE audit_evidence_packages DROP CONSTRAINT IF EXISTS uq_evidence_org_code"))
    conn.execute(sa.text("DROP INDEX IF EXISTS ix_audit_evidence_packages_org"))
    conn.execute(sa.text("ALTER TABLE audit_evidence_packages DROP COLUMN IF EXISTS organization_id"))
    conn.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_evidence_packages_package_code ON audit_evidence_packages (package_code)"))

    conn.execute(sa.text("DROP INDEX IF EXISTS ix_audit_data_exports_org"))
    conn.execute(sa.text("ALTER TABLE audit_data_exports DROP COLUMN IF EXISTS organization_id"))

    conn.execute(sa.text("DROP INDEX IF EXISTS ix_audit_policy_versions_org"))
    conn.execute(sa.text("ALTER TABLE audit_policy_versions DROP COLUMN IF EXISTS organization_id"))
