"""backfill missing commercial_organizations (operational org 1 -> internal organization)

Data-only backfill. Some operational organizations predate the commercial
(billing) layer and therefore have no row in `commercial_organizations`. The
Super Admin dashboard derives its tenant count and Organizations list solely
from that table (`backend/app/super_admin/router.py`), so those orgs are
invisible there until a commercial row exists.

Audited state at authoring time (2026-06-29, read-only against prod):
  • organizations:            id=1 (the internal organization), id=3 (an internal test tenant)
  • commercial_organizations: ONE row, operational_org_id=3 (already linked)
                              -> operational_org_id=1 is MISSING and needs a row
                              -> no duplicate / no NULL operational_org_id rows
  • commercial_organizations has NO unique constraint on operational_org_id,
    so duplicate protection MUST live in this migration (the WHERE NOT EXISTS
    guard below), not in the schema.

Properties:
  • INSERT-only. No UPDATE / DELETE / DDL on upgrade. Existing rows (incl. the
    org-3 row) are never touched.
  • Idempotent. Re-running inserts nothing once the org-1 row exists; it also
    skips any operational org that already has a commercial row.
  • Safe for production. `commercial_organizations` is an additive billing-layer
    table; employee time tracking, screenshots, attendance, payroll, leave and
    tenant data live in other tables and are unaffected.
  • Required NOT NULL columns supplied explicitly (id, name, commercial_state) —
    the model's Python-side defaults do NOT fire for raw SQL, and the columns
    have no DB server defaults.

Revision ID: d1c8b4a2f6e3
Revises: b7d2e1a9c3f4
Create Date: 2026-06-29
"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1c8b4a2f6e3"
down_revision: Union[str, Sequence[str], None] = "b7d2e1a9c3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Operational org IDs that need a backfilled commercial row. Kept as a list so
# the same migration can cover more orgs later without changing the logic.
# Org 1 is the internal organization only on legacy data; on a fresh database
# no org 1 exists when this runs, so the INSERT ... SELECT inserts nothing.
BACKFILL_OPERATIONAL_ORG_IDS = [1]


def upgrade() -> None:
    conn = op.get_bind()
    for op_org_id in BACKFILL_OPERATIONAL_ORG_IDS:
        # New PK matches the app convention: f"org_{uuid4.hex[:20]}".
        # If the row already exists, the INSERT...SELECT inserts 0 rows and this
        # generated id is simply discarded — the migration stays idempotent.
        new_id = f"org_{uuid.uuid4().hex[:20]}"
        conn.execute(
            sa.text(
                """
                INSERT INTO commercial_organizations
                    (id, name, operational_org_id, owner_user_id,
                     commercial_state, created_at, updated_at)
                SELECT
                    :new_id, o.name, o.id, NULL,
                    'active', now(), now()
                FROM organizations o
                WHERE o.id = :op_org_id
                  AND NOT EXISTS (
                      SELECT 1 FROM commercial_organizations c
                      WHERE c.operational_org_id = o.id
                  )
                """
            ),
            {"new_id": new_id, "op_org_id": op_org_id},
        )


def downgrade() -> None:
    # Narrowly scoped: remove only the commercial rows this migration backfills,
    # matched by operational_org_id. Does NOT touch any other tenant's row.
    conn = op.get_bind()
    for op_org_id in BACKFILL_OPERATIONAL_ORG_IDS:
        conn.execute(
            sa.text(
                "DELETE FROM commercial_organizations "
                "WHERE operational_org_id = :op_org_id"
            ),
            {"op_org_id": op_org_id},
        )
