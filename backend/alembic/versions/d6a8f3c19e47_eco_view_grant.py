"""ECO view authority — the persisted per-tenant view grant (owner ruling B2)

§24.7 requires a deferred day to be visible to both Worker and administrator.
The ALERT/visibility audit established that the repository had no authoritative
way to say who the administrator is: no capability, permission, scope or ACL
model exists, and authorization is hand-rolled per router against a nullable
free-text ``User.role``. The owner refused to let ECO adopt that and authorized
a dedicated view-authority primitive; this is its storage.

ADDITIVE AND INERT
------------------
One new table. No existing table is altered, no column is added or dropped,
no row is read, rewritten or deleted anywhere else. The table ships EMPTY and
no application path writes to it, so upgrading changes no behaviour at all —
absence of a grant is a denial, which is what the resolver already returns for
every caller today.

WHAT THE SHAPE ENCODES
----------------------
``UNIQUE (organization_id, user_id)`` is the authority contract: one grant per
user per tenant. It is also what makes a GLOBAL grant unrepresentable, since
``organization_id`` is NOT NULL and names exactly one organization — there is
no row shape meaning "every tenant".

Both foreign keys are RESTRICT. An organization or user holding live authority
records is not silently deletable; removing one is a decision that has to be
made explicitly, after the grant is withdrawn.

There is no ``revoked_at`` column, deliberately. Revocation is deletion of the
row, so the read path has exactly one question to ask — does a row exist — and
cannot be fooled by a second representation of "not granted".

DOWNGRADE
---------
Drops the table. Safe by construction: nothing references it by foreign key,
and losing grant rows removes authority rather than granting it. The downgrade
still REFUSES if rows exist, because silently deleting somebody's authority
record is a decision, not a schema step.

Revision ID: d6a8f3c19e47
Revises: c3f7a1e58d2b
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa

revision = "d6a8f3c19e47"
down_revision = "c3f7a1e58d2b"
branch_labels = None
depends_on = None

_TABLE = "eco_view_grant"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.String(length=64), nullable=False,
                  server_default="SYSTEM"),
        sa.Column("granted_at_utc", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "user_id",
                            name="uq_eco_view_grant_org_user"),
    )
    op.create_index(f"ix_{_TABLE}_organization_id", _TABLE, ["organization_id"])
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE}")  # noqa: S608 - fixed name
    ).scalar()
    if held:
        raise RuntimeError(
            f"cannot downgrade: {held} ECO view grant(s) exist. Dropping the "
            f"table would delete records of authority somebody explicitly "
            f"granted. Withdraw the grants first, then downgrade."
        )
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_organization_id", table_name=_TABLE)
    op.drop_table(_TABLE)
