"""app_settings scope tiers

Revision ID: e7ebc3f1fdb5
Revises: d1c8b4a2f6e3
Create Date: 2026-07-13 13:07:21.993015

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7ebc3f1fdb5'
down_revision: Union[str, Sequence[str], None] = 'd1c8b4a2f6e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Extend the org-scoped app_settings key/value store into a 3-tier cascade:
    employee (user_id) > team (team_id) > org (both NULL) > system default.

    Real uniqueness uses a COALESCE expression index — Postgres treats NULLs as
    distinct in a plain UNIQUE, which would let two org-level rows for the same
    key slip through.
    """
    # Drop a legacy GLOBAL unique index on key alone — it forbade two rows with
    # the same key across the whole table (even across orgs), which both broke
    # multi-tenancy and blocks per-scope override rows. The model always intended
    # a plain (non-unique) index on key.
    op.execute(sa.text("DROP INDEX IF EXISTS ix_app_settings_key"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_app_settings_key ON app_settings (key)"))
    # Team membership is the User.team name string (no teams.id FK on users), so
    # team scope keys on that string, not a numeric team id.
    op.execute(sa.text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS team_key VARCHAR(100) NULL"))
    op.execute(sa.text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS user_id INTEGER NULL"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE app_settings ADD CONSTRAINT fk_app_settings_user_id
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("ALTER TABLE app_settings DROP CONSTRAINT IF EXISTS uq_app_settings_org_key"))
    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_settings_scope
        ON app_settings (organization_id, COALESCE(team_key, ''), COALESCE(user_id, 0), key)
    """))
    op.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS ix_app_settings_scope_lookup
        ON app_settings (organization_id, team_key, user_id, key)
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_app_settings_scope_lookup"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_app_settings_scope"))
    op.execute(sa.text("ALTER TABLE app_settings DROP CONSTRAINT IF EXISTS fk_app_settings_user_id"))
    op.execute(sa.text("ALTER TABLE app_settings DROP COLUMN IF EXISTS user_id"))
    op.execute(sa.text("ALTER TABLE app_settings DROP COLUMN IF EXISTS team_key"))
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE app_settings ADD CONSTRAINT uq_app_settings_org_key
            UNIQUE (organization_id, key);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))
