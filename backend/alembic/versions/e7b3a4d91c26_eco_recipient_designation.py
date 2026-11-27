"""ECO recipient authority — the persisted per-tenant audience designation

ZT-SPEC-ECO-002 v2.0 §33.1, owner ruling D-3 (approved 2026-09-21).

§33.1's notification matrix names four audiences — WORKER, PRIMARY ADMIN,
DIRECT MANAGER, HR ADMIN — and ruling D-3 requires the authority behind them to
be IDENTITY-based and tenant-scoped, not an email-address lookup. Nothing in
the repository could supply that. ``app.notifications.recipients`` returns
lower-cased address strings from a free-text settings cascade; ``User.role`` is
a nullable free-text VARCHAR(50) read through eight hand-rolled tuples;
``org_settings.owner_user_id`` is the commercial owner, nullable and
``ON DELETE SET NULL``, and the specification maps no owner/PRIMARY-ADMIN
equivalence; ``teams.head_user_id`` is reachable only by joining a Worker's
free-text ``User.team`` string to ``Team.name``, which is inference from team
membership. This table is the authority those four candidates could not be.

ADDITIVE AND INERT
------------------
One new table. No existing table is altered, no column added or dropped, no row
read, rewritten or deleted anywhere else. The table ships EMPTY and no
application path writes to it, so upgrading changes no behaviour: with zero
rows every tenant resolves to the Worker alone, which is byte-for-byte what
``eco_threshold_event.recipient_snapshot`` already recorded before this
existed.

WHY THE AUDIENCE CHECK LISTS ONLY TWO VALUES
--------------------------------------------
The table is organisation-scoped, so it can only express audiences that ARE
organisation-scoped. WORKER is the event's subject — a designation row for it
would let a tenant redirect a Worker's own notification to somebody else, which
is a disclosure rather than a configuration. DIRECT MANAGER is a per-Worker
relationship, and an org-wide row for it would be an invented hierarchy, which
ruling D-3 forbids. Both are resolved elsewhere and neither is designatable, so
the CHECK constraint says so in the database rather than in a comment.

WHAT THE SHAPE ENCODES
----------------------
``UNIQUE (organization_id, audience, user_id)`` permits SEVERAL holders of one
audience and forbids the same holder twice. That is deliberate: §33.1 says
PRIMARY ADMIN is notified, not that exactly one person holds the seat, and the
resolver returns all holders in user-id order. There is no single-holder
column, which is what makes "silently choose the first user" unrepresentable.

A global designation is likewise unrepresentable: ``organization_id`` is NOT
NULL and names exactly one tenant, so no row shape means "every organization".
Whether the named USER belongs to that tenant is a different question that no
foreign key can express; the resolver checks it and REFUSES on a mismatch.

Both foreign keys are RESTRICT, the protected-history strategy the rest of ECO
uses. A user holding a live designation is not silently deletable. This does
not affect already-frozen history either way: the event snapshot stores the
identity, not a reference, and ruling D-3.7 makes it authoritative permanently.

There is no effective dating (ruling D-3.6) and no ``revoked_at``. The only
historical boundary that matters is EVENT CREATION -> SNAPSHOT, and revocation
is the deletion of the row, so the read path has exactly one question to ask.

DOWNGRADE
---------
Drops the table. Safe by construction: nothing references it by foreign key,
and losing designation rows removes authority rather than granting it. The
downgrade still REFUSES if rows exist, because silently deleting somebody's
recipient authority is a decision, not a schema step.

Revision ID: e7b3a4d91c26
Revises: b6d4f8a12c93
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa

revision = "e7b3a4d91c26"
down_revision = "b6d4f8a12c93"
branch_labels = None
depends_on = None

_TABLE = "eco_recipient_designation"

#: Mirrors ``app.recipient_authority.outcomes.DESIGNATABLE_AUDIENCES``. A test
#: asserts the two agree, so the constraint cannot drift away from the code
#: that resolves against it.
_DESIGNATABLE = ("HR_ADMIN", "PRIMARY_ADMIN")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("designated_by", sa.String(length=64), nullable=False,
                  server_default="SYSTEM"),
        sa.Column("designated_at_utc", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "audience", "user_id",
                            name="uq_eco_recipient_designation_identity"),
        sa.CheckConstraint(
            "audience IN (%s)" % ", ".join("'%s'" % a for a in _DESIGNATABLE),
            name="ck_eco_recipient_designation_audience"),
    )
    op.create_index(f"ix_{_TABLE}_organization_id", _TABLE, ["organization_id"])
    op.create_index(f"ix_{_TABLE}_user_id", _TABLE, ["user_id"])
    op.create_index(f"ix_{_TABLE}_lookup", _TABLE,
                    ["organization_id", "audience"])


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE}")  # noqa: S608 - fixed name
    ).scalar()
    if held:
        raise RuntimeError(
            f"cannot downgrade: {held} ECO recipient designation(s) exist. "
            f"Dropping the table would delete records of authority somebody "
            f"explicitly designated. Remove the designations first, then "
            f"downgrade."
        )
    op.drop_index(f"ix_{_TABLE}_lookup", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_user_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_organization_id", table_name=_TABLE)
    op.drop_table(_TABLE)
