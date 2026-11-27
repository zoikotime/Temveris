"""add organization_id to app_categories, replace app_name PK with serial id"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'h2c5d8e1f4a7'
down_revision: Union[str, None] = 'g1b4c7d2e5f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Step 1: add organization_id column (nullable first) ───────────────────
    op.execute(sa.text("ALTER TABLE app_categories ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE app_categories ADD CONSTRAINT fk_app_categories_organization_id
            FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("""
        UPDATE app_categories SET organization_id = users.organization_id
        FROM users WHERE app_categories.updated_by_id = users.id AND app_categories.organization_id IS NULL
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_categories_organization_id ON app_categories (organization_id)"))

    # ── Step 2: swap primary key — app_name (varchar) → serial id (integer) ──
    op.execute(sa.text("ALTER TABLE app_categories ADD COLUMN IF NOT EXISTS id INTEGER"))
    op.execute(sa.text("CREATE SEQUENCE IF NOT EXISTS app_categories_id_seq"))
    op.execute(sa.text("UPDATE app_categories SET id = nextval('app_categories_id_seq') WHERE id IS NULL"))
    op.execute(sa.text("ALTER TABLE app_categories ALTER COLUMN id SET NOT NULL"))
    op.execute(sa.text("ALTER TABLE app_categories ALTER COLUMN id SET DEFAULT nextval('app_categories_id_seq')"))
    op.execute(sa.text("ALTER SEQUENCE app_categories_id_seq OWNED BY app_categories.id"))
    op.execute(sa.text("ALTER TABLE app_categories DROP CONSTRAINT IF EXISTS app_categories_pkey"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE app_categories ADD PRIMARY KEY (id);
        EXCEPTION WHEN invalid_table_definition THEN NULL;
        END $$
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_categories_app_name ON app_categories (app_name)"))


def downgrade() -> None:
    op.drop_index('ix_app_categories_app_name', table_name='app_categories')

    # Restore app_name as primary key (destructive if duplicate app_names exist
    # across orgs — only safe if the table was single-tenant when downgraded)
    op.execute("ALTER TABLE app_categories DROP CONSTRAINT IF EXISTS app_categories_pkey")
    op.execute("ALTER TABLE app_categories DROP COLUMN IF EXISTS id")
    op.execute("DROP SEQUENCE IF EXISTS app_categories_id_seq")
    op.execute("ALTER TABLE app_categories ADD PRIMARY KEY (app_name)")

    op.drop_index('ix_app_categories_organization_id', table_name='app_categories')
    op.drop_constraint('fk_app_categories_organization_id', 'app_categories', type_='foreignkey')
    op.drop_column('app_categories', 'organization_id')
