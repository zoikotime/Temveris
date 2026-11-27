"""Performance indexes: organization-scoped user and audit log queries.

Adds four indexes identified in the performance audit as missing:

  users
  ─────
  ix_users_org_id           (organization_id)
      Covers the common single-predicate org scope used across security
      center, workspace settings, governance overview, and metrics endpoints.
      Currently a full-table scan on every multi-tenant user query.

  ix_users_org_role         (organization_id, role)
      Covers role-count GROUP BY used in _user_role_counts(), access
      governance overview, and security center admin-count tiles.
      Leading column satisfies pure organization_id filters too, but the
      standalone ix_users_org_id is retained for queries where the optimizer
      prefers a narrower index.

  ix_users_org_active       (organization_id, is_active)
      Covers active-user counts in _org_operational_metrics(), security
      summary stale-admin count, and governance assignment listing.

  audit_logs
  ──────────
  ix_audit_logs_org_created (organization_id, created_at)
      Covers the dominant audit query shape:
          WHERE organization_id = :x AND created_at >= :cutoff
      used in audit_governance.overview(), audit_feed(), security_center
      events, and access governance audit trail. The existing single-column
      indexes on each column are used separately today, paying two B-tree
      lookups + a bitmap AND. The composite resolves the full predicate
      in one range scan.

Skipped (already exist):
  audit_logs.organization_id      — index=True on column definition
  audit_logs.created_at           — index=True on column definition
  audit_logs(entity, action, created_at) — ix_audit_entity_action_created
  users.role                      — index=True on column definition

All CREATE INDEX statements use IF NOT EXISTS for idempotency — safe to
re-run if the migration was partially applied.

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-02
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ensure organization_id exists on users and audit_logs before indexing.
    # Production was set up manually before multitenancy columns were added.
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))
    op.execute(sa.text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS organization_id INTEGER NULL"))

    # ── users ────────────────────────────────────────────────────────────────

    # Single-column: general org-scoped user lookups and joins.
    op.create_index(
        "ix_users_org_id",
        "users",
        ["organization_id"],
        unique=False,
        if_not_exists=True,
    )

    # Composite: role-count aggregations (GROUP BY role WHERE org = X)
    # and role-filtered counts (admin/hr tile in security center overview).
    op.create_index(
        "ix_users_org_role",
        "users",
        ["organization_id", "role"],
        unique=False,
        if_not_exists=True,
    )

    # Composite: active-user counts (WHERE org = X AND is_active = true/false).
    op.create_index(
        "ix_users_org_active",
        "users",
        ["organization_id", "is_active"],
        unique=False,
        if_not_exists=True,
    )

    # ── audit_logs ───────────────────────────────────────────────────────────

    # Composite: time-windowed org audit queries
    # (WHERE organization_id = X AND created_at >= cutoff).
    op.create_index(
        "ix_audit_logs_org_created",
        "audit_logs",
        ["organization_id", "created_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_org_created", table_name="audit_logs", if_exists=True)
    op.drop_index("ix_users_org_active",       table_name="users",      if_exists=True)
    op.drop_index("ix_users_org_role",          table_name="users",      if_exists=True)
    op.drop_index("ix_users_org_id",            table_name="users",      if_exists=True)
