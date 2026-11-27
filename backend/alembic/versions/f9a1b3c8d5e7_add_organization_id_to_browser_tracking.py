"""add organization_id to live_statuses, browser_activities, domain_categories"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f9a1b3c8d5e7'
down_revision: Union[str, None] = 'e2f4a8c3d1b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── live_statuses ──────────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE live_statuses ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE live_statuses ADD CONSTRAINT fk_live_statuses_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE live_statuses SET organization_id = users.organization_id
        FROM users WHERE live_statuses.user_id = users.id AND live_statuses.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_live_statuses_organization_id ON live_statuses (organization_id)"))

    # ── browser_activities ─────────────────────────────────────────────────────
    op.execute(sa.text("ALTER TABLE browser_activities ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE browser_activities ADD CONSTRAINT fk_browser_activities_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE browser_activities SET organization_id = users.organization_id
        FROM users WHERE browser_activities.user_id = users.id AND browser_activities.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_browser_activities_organization_id ON browser_activities (organization_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_browser_activities_org_started ON browser_activities (organization_id, started_at)"))

    # ── domain_categories: replace string PK with serial integer PK ───────────
    op.execute(sa.text("ALTER TABLE domain_categories ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE domain_categories ADD CONSTRAINT fk_domain_categories_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE domain_categories SET organization_id = users.organization_id
        FROM users WHERE domain_categories.updated_by_id = users.id AND domain_categories.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_domain_categories_organization_id ON domain_categories (organization_id)"))
    # Swap primary key: add serial id, drop domain PK, promote id.
    op.execute(sa.text("ALTER TABLE domain_categories ADD COLUMN IF NOT EXISTS id INTEGER"))
    op.execute(sa.text("CREATE SEQUENCE IF NOT EXISTS domain_categories_id_seq"))
    op.execute(sa.text("UPDATE domain_categories SET id = nextval('domain_categories_id_seq') WHERE id IS NULL"))
    op.execute(sa.text("ALTER TABLE domain_categories ALTER COLUMN id SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE domain_categories ALTER COLUMN id SET DEFAULT nextval('domain_categories_id_seq')"))
    op.execute(sa.text("ALTER SEQUENCE domain_categories_id_seq OWNED BY domain_categories.id"))
    op.execute(sa.text("ALTER TABLE domain_categories DROP CONSTRAINT IF EXISTS domain_categories_pkey"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE domain_categories ADD PRIMARY KEY (id);
        EXCEPTION WHEN invalid_table_definition THEN NULL;
        END $$
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_domain_categories_domain ON domain_categories (domain)"))


def downgrade() -> None:
    # Restore domain as primary key
    op.drop_index('ix_domain_categories_domain', table_name='domain_categories')
    op.execute("ALTER TABLE domain_categories DROP CONSTRAINT IF EXISTS domain_categories_pkey")
    op.execute("ALTER TABLE domain_categories DROP COLUMN IF EXISTS id")
    op.execute("DROP SEQUENCE IF EXISTS domain_categories_id_seq")
    op.execute("ALTER TABLE domain_categories ADD PRIMARY KEY (domain)")
    op.drop_index('ix_domain_categories_organization_id', table_name='domain_categories')
    op.drop_constraint('fk_domain_categories_organization_id', 'domain_categories', type_='foreignkey')
    op.drop_column('domain_categories', 'organization_id')

    op.drop_index('ix_browser_activities_org_started', table_name='browser_activities')
    op.drop_index('ix_browser_activities_organization_id', table_name='browser_activities')
    op.drop_constraint('fk_browser_activities_organization_id', 'browser_activities', type_='foreignkey')
    op.drop_column('browser_activities', 'organization_id')

    op.drop_index('ix_live_statuses_organization_id', table_name='live_statuses')
    op.drop_constraint('fk_live_statuses_organization_id', 'live_statuses', type_='foreignkey')
    op.drop_column('live_statuses', 'organization_id')
