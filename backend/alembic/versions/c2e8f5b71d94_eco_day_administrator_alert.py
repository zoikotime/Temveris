"""ECO day administrator alert — the §24.3 day-scoped primitive (ruling D-C: B)

ZT-SPEC-ECO-002 v2.0 §24.3 and §33.1.

§24.3: a day whose requirement resolves at no rank produces *"no shortfall, no
advisory, no notification to the Worker, and an operational alert to the Tenant
administrator"*. §33.1's matching row marks PRIMARY ADMIN *Always
(operational)* and WORKER *Never*.

WHY TWO NEW TABLES AND NOT A COLUMN ON eco_notification
--------------------------------------------------------
``eco_notification``'s identity is week-shaped —
``(organization_id, user_id, workweek_key, tier, mode, sequence,
notification_type, audience)`` with ``workweek_key``, ``tier`` and ``sequence``
all NOT NULL — and it carries a NOT NULL foreign key to
``eco_threshold_event``.

A requirement-unresolved day produces NO threshold event: EVAL-WEEK blocks the
week and EVAL-EVENT refuses anything that is not COMPLETE. Carrying this alert
in that table would therefore require either fabricating a threshold event to
satisfy the foreign key, or discarding the day identity in favour of a week.
Owner ruling D-C: B forbids the first and the second would lose what §24.3 is
about, so the alert gets its own storage and ``eco_notification`` is untouched.

That isolation is also what keeps GV-07 unaffected by this revision: no shared
table, no shared constraint, no altered column.

THE IDENTITY, AND WHY THE GENERATION IS IN IT
----------------------------------------------
``UNIQUE (organization_id, user_id, work_date, evaluation_generation,
alert_type, audience, mode)``.

``eco_day_record`` is generation-versioned, and a corrected day produces a NEW
generation rather than editing the old one. Pinning the generation makes
re-processing one authoritative evaluation idempotent, and makes a later
corrected evaluation a different fact that this row neither claims nor silently
follows.

THE RECIPIENT CHECKS ENCODE D-1
--------------------------------
``recipient_user_id`` is nullable because an unresolved PRIMARY ADMIN must be
RECORDED, never substituted. A paired CHECK ties ``recipient_outcome`` to the
presence of the id, so the two halves cannot drift apart: RESOLVED implies an
id, RECIPIENT_UNRESOLVED implies NULL, and nothing else is representable.

The audience CHECK admits ``PRIMARY_ADMIN`` alone, so a Worker, manager or HR
row cannot be written at all — §24.3's prohibition in the database rather than
in a comment.

APPEND-ONLY (R5)
----------------
Both tables carry the same trigger protection the notification history does,
emitted explicitly here because ``op.create_table`` builds its own ``Table``
object and the ORM-table listener does not fire for it. Without this a migrated
database would lack the triggers while a test database had them.

ADDITIVE AND INERT
------------------
Two new tables. No existing table is altered, no column added or dropped, no
row read or rewritten. Both ship EMPTY and nothing raises an alert
automatically, so upgrading changes no behaviour.

DOWNGRADE
---------
Drops what it added, and REFUSES if any alert exists: discarding the record of
what a Tenant was told about its own configuration is a decision, not a schema
step.

Revision ID: c2e8f5b71d94
Revises: f8c1d47a92e3
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

from app.eco_day_alert.append_only_ddl import (
    ATTEMPT_TABLE,
    DAY_ALERT_TABLE,
    append_only_statements,
    attempt_append_only_statements,
    attempt_drop_statements,
    drop_statements,
)

revision = "c2e8f5b71d94"
down_revision = "f8c1d47a92e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        DAY_ALERT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("day_record_id", sa.Integer(), nullable=False),
        sa.Column("evaluation_generation", sa.Integer(), nullable=False),
        sa.Column("day_state", sa.String(length=32), nullable=False),
        sa.Column("alert_type", sa.String(length=32), nullable=False),
        sa.Column("audience", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), nullable=True),
        sa.Column("recipient_outcome", sa.String(length=32), nullable=False),
        sa.Column("recipient_snapshot", sa.JSON(), nullable=False),
        sa.Column("rendered_body", sa.Text(), nullable=False),
        sa.Column("rendered_copy_id", sa.String(length=64), nullable=False),
        sa.Column("raised_at_utc", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("raised_by", sa.String(length=64), nullable=False,
                  server_default="ECO_DAY_ALERT"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["day_record_id"], ["eco_day_record.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "organization_id", "user_id", "work_date", "evaluation_generation",
            "alert_type", "audience", "mode",
            name="uq_eco_day_administrator_alert_identity"),
        sa.CheckConstraint("audience IN ('PRIMARY_ADMIN')",
                           name="ck_eco_day_administrator_alert_audience"),
        sa.CheckConstraint("alert_type IN ('REQUIREMENT_UNRESOLVED')",
                           name="ck_eco_day_administrator_alert_type"),
        sa.CheckConstraint("day_state = 'REQUIREMENT_UNRESOLVED'",
                           name="ck_eco_day_administrator_alert_state"),
        sa.CheckConstraint(
            "recipient_outcome IN ('RESOLVED', 'RECIPIENT_UNRESOLVED')",
            name="ck_eco_day_administrator_alert_recipient_outcome"),
        sa.CheckConstraint(
            "(recipient_outcome = 'RESOLVED' AND recipient_user_id IS NOT NULL)"
            " OR (recipient_outcome = 'RECIPIENT_UNRESOLVED' AND "
            "recipient_user_id IS NULL)",
            name="ck_eco_day_administrator_alert_recipient_pair"),
        sa.CheckConstraint("length(rendered_body) > 0",
                           name="ck_eco_day_administrator_alert_body_nonempty"),
    )
    op.create_index(f"ix_{DAY_ALERT_TABLE}_organization_id", DAY_ALERT_TABLE,
                    ["organization_id"])
    op.create_index(f"ix_{DAY_ALERT_TABLE}_user_id", DAY_ALERT_TABLE,
                    ["user_id"])
    op.create_index(f"ix_{DAY_ALERT_TABLE}_work_date", DAY_ALERT_TABLE,
                    ["work_date"])
    op.create_index(f"ix_{DAY_ALERT_TABLE}_day_record_id", DAY_ALERT_TABLE,
                    ["day_record_id"])
    op.create_index(f"ix_{DAY_ALERT_TABLE}_mode", DAY_ALERT_TABLE, ["mode"])
    op.create_index("ix_eco_day_administrator_alert_pending", DAY_ALERT_TABLE,
                    ["organization_id", "mode"])

    op.create_table(
        ATTEMPT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("attempt_seq", sa.Integer(), nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=48), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("attempted_at_utc", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["alert_id"], [f"{DAY_ALERT_TABLE}.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("alert_id", "attempt_seq",
                            name="uq_eco_day_alert_attempt_seq"),
        sa.CheckConstraint("attempt_seq >= 1",
                           name="ck_eco_day_alert_attempt_seq_positive"),
        sa.CheckConstraint("outcome IN ('dispatched', 'failed')",
                           name="ck_eco_day_alert_attempt_outcome"),
        sa.CheckConstraint(
            "failure_detail IS NULL OR length(failure_detail) <= 512",
            name="ck_eco_day_alert_attempt_detail_len"),
    )
    op.create_index(f"ix_{ATTEMPT_TABLE}_alert_id", ATTEMPT_TABLE,
                    ["alert_id"])
    op.create_index(f"ix_{ATTEMPT_TABLE}_mode", ATTEMPT_TABLE, ["mode"])

    # R5, emitted explicitly — see the module docstring.
    dialect = op.get_bind().dialect.name
    for statement in append_only_statements(dialect):
        op.execute(statement)
    for statement in attempt_append_only_statements(dialect):
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()

    held = bind.execute(
        sa.text(f"SELECT count(*) FROM {DAY_ALERT_TABLE}")  # noqa: S608
    ).scalar()
    if held:
        raise RuntimeError(
            f"cannot downgrade: {held} ECO day administrator alert(s) exist. "
            f"Dropping the table would discard the record of what a Tenant was "
            f"told about its own working-time configuration."
        )

    dialect = bind.dialect.name
    for statement in attempt_drop_statements(dialect):
        op.execute(statement)
    for statement in drop_statements(dialect):
        op.execute(statement)

    op.drop_index(f"ix_{ATTEMPT_TABLE}_mode", table_name=ATTEMPT_TABLE)
    op.drop_index(f"ix_{ATTEMPT_TABLE}_alert_id", table_name=ATTEMPT_TABLE)
    op.drop_table(ATTEMPT_TABLE)

    op.drop_index("ix_eco_day_administrator_alert_pending",
                  table_name=DAY_ALERT_TABLE)
    for suffix in ("mode", "day_record_id", "work_date", "user_id",
                   "organization_id"):
        op.drop_index(f"ix_{DAY_ALERT_TABLE}_{suffix}",
                      table_name=DAY_ALERT_TABLE)
    op.drop_table(DAY_ALERT_TABLE)
