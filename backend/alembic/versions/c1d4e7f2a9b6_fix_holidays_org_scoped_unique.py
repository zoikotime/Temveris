"""holidays: replace global unique(date) with per-org unique(organization_id, date)

Some databases were materialised by `Base.metadata.create_all` from an OLD
Holiday model where `date` was `unique=True, index=True`, producing a GLOBAL
unique index `ix_holidays_date`. The alembic version was then stamped ahead of
migration a4c8f2e1b7d9 (which was meant to swap that for a per-org constraint),
so that migration never executed on those DBs.

The consequence: holidays are unique platform-wide. A second org can never add a
holiday on a date any other org already uses, and the INSERT raises an
IntegrityError that surfaces as a plain-text 500 — which the admin "Add Holiday"
dialog shows as "Network error".

This migration reconciles the schema idempotently regardless of the starting
state: it adopts orphan (org-less) holidays, drops the legacy global index, and
adds the intended per-org unique constraint. It is a safe no-op where the schema
is already correct.

Revision ID: c1d4e7f2a9b6
Revises: f4a9c2e7b1d8
Create Date: 2026-06-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d4e7f2a9b6"
down_revision: Union[str, Sequence[str], None] = "f4a9c2e7b1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Adopt orphan (NULL-org) holidays into the first org so they become
    #    visible to a tenant — but only where that date is still free, to avoid
    #    colliding with holidays the org may already have (e.g. seeded defaults).
    op.execute(sa.text("""
        UPDATE holidays h
        SET organization_id = (SELECT id FROM organizations ORDER BY id LIMIT 1)
        WHERE h.organization_id IS NULL
          AND EXISTS (SELECT 1 FROM organizations)
          AND NOT EXISTS (
              SELECT 1 FROM holidays h2
              WHERE h2.organization_id = (SELECT id FROM organizations ORDER BY id LIMIT 1)
                AND h2.date = h.date
          )
    """))

    # 2. Drop any remaining orphan rows: they are invisible to every tenant
    #    (reads are org-scoped) and would block the per-org unique constraint.
    op.execute(sa.text("DELETE FROM holidays WHERE organization_id IS NULL"))

    # 3. Drop the legacy GLOBAL unique index on date (the root cause).
    op.execute(sa.text("DROP INDEX IF EXISTS ix_holidays_date"))

    # 4. Add the intended per-org unique constraint (no-op if already present).
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE holidays ADD CONSTRAINT uq_holidays_org_date UNIQUE (organization_id, date);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """))

    # 5. Ensure the organization_id lookup index exists.
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_holidays_organization_id ON holidays (organization_id)"))


def downgrade() -> None:
    # Drop only what this migration added. The legacy global unique index is NOT
    # recreated: by now distinct orgs may legitimately share a date, which that
    # index would reject.
    op.execute(sa.text("""
        DO $$ BEGIN
            ALTER TABLE holidays DROP CONSTRAINT uq_holidays_org_date;
        EXCEPTION WHEN undefined_object THEN NULL;
        END $$
    """))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_holidays_organization_id"))
