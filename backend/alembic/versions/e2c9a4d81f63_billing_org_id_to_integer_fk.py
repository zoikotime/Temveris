"""billing_*.organization_id -> INTEGER FK organizations.id (ZT-COM-BILL-001 §4)

Removes the shared-tenant assumption from the billing schema. Every billing
table carried ``organization_id VARCHAR(80) DEFAULT 'default'`` with no foreign
key, so all tenants shared one logical subscription: one org was billed for
every user on the platform and the other orgs could not be billed at all.

Mapping of the legacy sentinel:

    'default' -> organizations.id = 1 (the internal organization on legacy data)

Evidence for that mapping (verified before writing this migration):
  * 38 of 38 billing_audit_events actors belong to organization 1
  * 21 of 21 billing_downgrade_requests requesters belong to organization 1
  * the single subscription row held the internal organization's Stripe customer
    (id intentionally not recorded here), created by an organization-1 admin

The migration refuses to run if it finds an organization_id it cannot map, or a
value with no matching organizations row, rather than silently dropping tenancy.

No rows are deleted. Column widened in place with an explicit USING cast.

Revision ID: e2c9a4d81f63
Revises: d1b8f3a67c25
Create Date: 2026-08-09 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e2c9a4d81f63'
down_revision: Union[str, Sequence[str], None] = 'd1b8f3a67c25'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = (
    "billing_subscriptions",
    "billing_invoices",
    "billing_contacts",
    "billing_tax_profiles",
    "billing_payment_methods",
    "billing_audit_events",
    "billing_downgrade_requests",
    "billing_enterprise_quotes",
)

# The legacy sentinel resolves to organization 1, the internal organization on
# legacy data (a fresh database has no 'default' rows). See the module docstring
# for the evidence trail — this is a mapping decision, not an assumption.
_LEGACY_SENTINEL = "default"
_LEGACY_SENTINEL_ORG_ID = 1


def upgrade() -> None:
    conn = op.get_bind()

    # ── Guard 1: every value must be the sentinel or a plain integer ──────────
    for table in _TABLES:
        unmappable = conn.execute(
            sa.text(
                f"""
                SELECT DISTINCT organization_id FROM {table}
                 WHERE organization_id IS NOT NULL
                   AND organization_id <> :sentinel
                   AND organization_id !~ '^[0-9]+$'
                """
            ),
            {"sentinel": _LEGACY_SENTINEL},
        ).scalars().all()
        if unmappable:
            raise RuntimeError(
                f"{table}.organization_id holds values that cannot be mapped to "
                f"organizations.id: {unmappable!r}. Resolve these rows before "
                f"re-running this migration."
            )

    # ── Rewrite the sentinel to the real organization id ─────────────────────
    for table in _TABLES:
        conn.execute(
            sa.text(
                f"UPDATE {table} SET organization_id = :new WHERE organization_id = :sentinel"
            ),
            {"new": str(_LEGACY_SENTINEL_ORG_ID), "sentinel": _LEGACY_SENTINEL},
        )

    # ── Guard 2: every remaining value must reference a real organization ────
    for table in _TABLES:
        orphans = conn.execute(
            sa.text(
                f"""
                SELECT DISTINCT t.organization_id FROM {table} t
                 WHERE t.organization_id IS NOT NULL
                   AND NOT EXISTS (
                        SELECT 1 FROM organizations o
                         WHERE o.id = t.organization_id::integer)
                """
            )
        ).scalars().all()
        if orphans:
            raise RuntimeError(
                f"{table}.organization_id references organizations that do not "
                f"exist: {orphans!r}. A foreign key cannot be added until these "
                f"rows are corrected."
            )

    # ── Convert type and attach the foreign key ─────────────────────────────
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN organization_id DROP DEFAULT")
        op.alter_column(
            table,
            "organization_id",
            existing_type=sa.String(length=80),
            type_=sa.Integer(),
            existing_nullable=True,
            postgresql_using="organization_id::integer",
        )
        op.create_foreign_key(
            f"fk_{table}_organization_id",
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Reverse the type change. The sentinel is NOT restored.

    Collapsing real organization ids back to a shared 'default' would re-create
    the cross-tenant billing defect this migration removes. Organization 1's
    rows keep the value '1'; any other tenant's rows keep their own id.
    """
    for table in _TABLES:
        op.drop_constraint(f"fk_{table}_organization_id", table, type_="foreignkey")
        op.alter_column(
            table,
            "organization_id",
            existing_type=sa.Integer(),
            type_=sa.String(length=80),
            existing_nullable=True,
            postgresql_using="organization_id::text",
        )
