"""organizations.billing_type — INTERNAL vs CUSTOMER tenant classification

The vendor's own workspace (the internal organization — organization 1 on legacy
data) runs the product it sells. It must
receive full plan entitlements and must never be charged through Stripe. Every
other organization — existing test tenants and all future clients — pays normally.

Until now that distinction existed nowhere. "Internal tenant" safety was an
*implicit* invariant: the Stripe write paths only refuse when
``stripe_subscription_id`` / ``stripe_customer_id`` happen to be NULL. Organization
1 has both populated, so it was fully wired to Stripe with nothing recording that
it should never be billed.

Why this column, and not an existing field:

* ``billing_subscriptions.enterprise_contract_flag`` is a dead column, but
  "enterprise" is a real *paying customer* concept here (``BillingPlan.is_enterprise``,
  ``/enterprise-quote``, ``BillingEnterpriseQuote``). Reusing it would conflate a
  contracted client with a non-paying owner.
* ``subscription_status`` is mutable lifecycle state — rewritten by webhooks and
  plan changes — so a classification stored there would be destroyed by the next
  Stripe event.
* The classification must exist *before* any ``billing_subscriptions`` row does
  (``ensure_subscription`` creates rows lazily) and must survive that row being
  replaced. That makes it a property of the organization, not of a subscription.

``DEFAULT 'customer'`` is deliberate and is the revenue-safe direction: a new
organization bills unless someone explicitly exempts it. Forgetting the flag can
never hand a paying client free service.

Only organization 1 is set to 'internal' — and only on legacy data: on a fresh
database the UPDATE matches no row and every organization keeps the 'customer'
default. Organizations 3, 6 and 7 are internal
*test* tenants that will be deleted once multi-tenancy testing finishes, so they
keep the 'customer' default per the recorded business decision — they hold no
Stripe subscription and the account is in TEST mode.

Adds one column and updates one row. No billing table is touched. Reversible.

Revision ID: b6a3d9e52f14
Revises: a5f2c8d41e79
Create Date: 2026-08-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b6a3d9e52f14'
down_revision: Union[str, Sequence[str], None] = 'a5f2c8d41e79'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INTERNAL = "internal"
CUSTOMER = "customer"

# The internal organization (the vendor's own workspace) is never charged.
# Org 1 is only internal on legacy data; a fresh database has no row 1 yet when
# this runs, so nothing is marked internal there.
_INTERNAL_ORG_IDS = (1,)


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("billing_type", sa.String(length=20), nullable=False,
                  server_default=sa.text(f"'{CUSTOMER}'")),
    )
    # Queried on every Stripe write path, so it is worth an index even though the
    # table is small — it will not stay small.
    op.create_index("ix_organizations_billing_type", "organizations", ["billing_type"])

    conn = op.get_bind()
    for org_id in _INTERNAL_ORG_IDS:
        # Guarded so the migration is environment-portable: an id absent in DEV
        # simply updates nothing rather than failing the deploy.
        conn.execute(
            sa.text("UPDATE organizations SET billing_type = :t WHERE id = :id"),
            {"t": INTERNAL, "id": org_id},
        )


def downgrade() -> None:
    op.drop_index("ix_organizations_billing_type", table_name="organizations")
    op.drop_column("organizations", "billing_type")
