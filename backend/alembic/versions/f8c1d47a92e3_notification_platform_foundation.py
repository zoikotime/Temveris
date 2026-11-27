"""Notification platform foundation — dispatch idempotency + ECO result columns

ZT-SPEC-ECO-002 v2.0 §34; notification-platform owner rulings D-1..D-4;
architecture decisions D-A (internal platform module) and D-B (a distinct
non-suppressible administrator class).

TWO INDEPENDENT PIECES, ONE REVISION
------------------------------------
1. ``notification_dispatch_request`` — the PLATFORM's request-idempotency
   store (D-4). New table, platform-owned, ships empty.
2. ``eco_notification_delivery_attempt`` gains ``reason_code`` and
   ``correlation_id`` — the ECO side of the result contract (D-3, D-5.5).

They land together because they are two halves of one contract: the platform
produces the values and ECO records them. Splitting them would leave a revision
in which one half cannot represent what the other returns.

WHY TWO IDENTITIES, NOT ONE
---------------------------
D-4 is explicit that these must not be collapsed:

    PLATFORM REQUEST IDENTITY    (eco_event_id, recipient_user_id)
    ECO DELIVERY RECORD IDENTITY (threshold_event_id, recipient_id, channel)

The request identity exists BEFORE a channel does — that is why D-4 exists at
all, since ECO may not select the channel it would otherwise need to name. So
the ECO attempt table cannot serve as the platform's idempotency store: it
would need a channel to key a row created before any channel is chosen.

WHY ``correlation_id`` IS ITS OWN COLUMN
----------------------------------------
Forced by two rulings read together, not chosen. D-3 requires a correlation id
on EVERY completed dispatch, including a successful one. D-5.5 permits the
diagnostic to be NULL when the service supplies none, which is the normal case
on success. Both hold only if they are separate columns; composing the
correlation id into ``failure_detail`` would make that column non-NULL on every
success and contradict D-5.5.

WHY ``failure_detail`` IS NOT RETYPED
-------------------------------------
It shipped as TEXT. Narrowing it to VARCHAR(512) would be a table rewrite, and
this revision is additive. A CHECK enforces the same D-5.5 bound at no cost.

CHECK CONSTRAINTS AND SQLITE
----------------------------
The two CHECKs on the existing ECO attempt table are added on PostgreSQL only.
SQLite cannot ``ALTER TABLE ... ADD CONSTRAINT``, and the batch-rebuild
alternative would drop and recreate the table — taking the append-only triggers
(R5) with it, which is a far worse trade than a dialect-conditional constraint.
The model carries both CHECKs in ``__table_args__``, so every ``create_all``
database — which is every test database — gets them regardless.

ADDITIVE AND INERT
------------------
One new table, two new nullable columns, no backfill, no UPDATE, no row read or
rewritten. The new table ships EMPTY and no ECO path calls ``dispatch()``, so
upgrading changes no behaviour at all.

DOWNGRADE
---------
Drops what it added, and REFUSES if the dispatch table holds rows: discarding
the record of which notifications were handed off is a decision, not a schema
step.

Revision ID: f8c1d47a92e3
Revises: e7b3a4d91c26
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa

revision = "f8c1d47a92e3"
down_revision = "e7b3a4d91c26"
branch_labels = None
depends_on = None

_DISPATCH = "notification_dispatch_request"
_ATTEMPT = "eco_notification_delivery_attempt"


def upgrade() -> None:
    # ── 1. the platform's request-idempotency store (D-4) ────────────────────
    op.create_table(
        _DISPATCH,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(length=40), nullable=False,
                  server_default="notification_dispatch"),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("eco_event_id", sa.Integer(), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="in_progress"),
        sa.Column("channel", sa.String(length=32), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("reason_code", sa.String(length=48), nullable=True),
        sa.Column("failure_detail", sa.String(length=512), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True),
                  nullable=True),
        # THE duplicate-prevention guarantee (D-4). Enforced by the database,
        # which is the only way it survives concurrency.
        sa.UniqueConstraint("scope", "idempotency_key",
                            name="uq_notification_dispatch_request_key"),
        sa.CheckConstraint("status IN ('in_progress', 'completed')",
                           name="ck_notification_dispatch_request_status"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('dispatched', 'failed')",
            name="ck_notification_dispatch_request_outcome"),
        sa.CheckConstraint(
            "failure_detail IS NULL OR length(failure_detail) <= 512",
            name="ck_notification_dispatch_request_detail_len"),
    )
    op.create_index(f"ix_{_DISPATCH}_organization_id", _DISPATCH,
                    ["organization_id"])
    op.create_index(f"ix_{_DISPATCH}_event", _DISPATCH,
                    ["eco_event_id", "recipient_user_id"])

    # ── 2. the ECO side of the result contract (D-3, D-5.5) ──────────────────
    op.add_column(_ATTEMPT,
                  sa.Column("reason_code", sa.String(length=48),
                            nullable=True))
    op.add_column(_ATTEMPT,
                  sa.Column("correlation_id", sa.String(length=64),
                            nullable=True))

    # PostgreSQL only — see the module docstring.
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_eco_notification_attempt_outcome", _ATTEMPT,
            "outcome IN ('dispatched', 'failed')")
        op.create_check_constraint(
            "ck_eco_notification_attempt_detail_len", _ATTEMPT,
            "failure_detail IS NULL OR length(failure_detail) <= 512")


def downgrade() -> None:
    bind = op.get_bind()

    held = bind.execute(
        sa.text(f"SELECT count(*) FROM {_DISPATCH}")  # noqa: S608 - fixed name
    ).scalar()
    if held:
        raise RuntimeError(
            f"cannot downgrade: {held} notification dispatch request(s) exist. "
            f"Dropping the table would discard the record of which "
            f"notifications were handed off, and with it the idempotency "
            f"guarantee protecting against duplicate dispatch."
        )

    if bind.dialect.name == "postgresql":
        op.drop_constraint("ck_eco_notification_attempt_detail_len", _ATTEMPT,
                           type_="check")
        op.drop_constraint("ck_eco_notification_attempt_outcome", _ATTEMPT,
                           type_="check")
    op.drop_column(_ATTEMPT, "correlation_id")
    op.drop_column(_ATTEMPT, "reason_code")

    op.drop_index(f"ix_{_DISPATCH}_event", table_name=_DISPATCH)
    op.drop_index(f"ix_{_DISPATCH}_organization_id", table_name=_DISPATCH)
    op.drop_table(_DISPATCH)
