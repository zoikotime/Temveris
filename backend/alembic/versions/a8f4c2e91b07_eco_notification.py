"""eco notification record and delivery-attempt history (ALERT)

Revision ID: a8f4c2e91b07
Revises: f3d8a1c65b29
Create Date: 2026-09-11 00:00:00.000000

Creates the durable ECO notification layer:

    eco_notification                     one IMMUTABLE artifact per audience
    eco_notification_delivery_attempt    append-only attempt history (INERT)

WHY A SCHEMA CHANGE IS REQUIRED
-------------------------------
Owner ruling R5 requires dedicated durable ECO notification persistence and
explicitly forbids the alternatives that already exist: ``AuditLog`` (which the
attendance-alert path uses for de-duplication), ``risk_alerts``, the legacy
attendance tables, and ephemeral websocket state. None of them can carry an
immutable rendered body, a frozen recipient identity, an eligibility record or
a per-audience identity, and ``AuditLog`` is not a uniqueness surface at all.

THE IDENTITY — EIGHT COLUMNS, AND WHY EACH IS THERE
----------------------------------------------------
    (organization_id, user_id, workweek_key, tier, mode, sequence,
     notification_type, audience)

The first six are owner ruling R3's LOGICAL EPISODE identity, carried verbatim
and unchanged. ``notification_type`` is PURPOSE: without it a withdrawal would
collide with the crossing it closes, because EVAL-EVENT gives a withdrawal the
SAME sequence as that crossing. ``audience`` is WHO: without it a second
audience would read as a duplicate of the first.

Neither of the two extra columns is folded into the episode. A re-crossing at
``sequence = 2`` is a new episode; rendering the same episode for a second
audience is not.

APPEND-ONLY, ENFORCED BY THE DATABASE (R5)
------------------------------------------
Both tables refuse UPDATE and DELETE. ``app/alert/append_only_ddl.py`` is the
single definition of the trigger SQL, imported by both this revision and the
ORM models; that module imports nothing — no ``Base``, no models, no SQLAlchemy
— precisely so a revision may import it safely. The emission is EXPLICIT here
because ``op.create_table`` builds its own ``Table`` object, so the models'
``after_create`` listeners do not fire for it; without this, a migrated
database would lack the triggers while a create_all database had them, and
every below-ORM test would pass vacuously against a shape production does not
have.

THE ATTEMPT TABLE IS DELIBERATELY INERT
----------------------------------------
No application path writes to it in this increment and nothing is delivered. It
exists because R5 defines the target architecture, and because its existence is
what lets the notification row stay immutable: delivery state lives here, so
nothing is ever written back onto the notification. Creating it now avoids a
second migration over the same domain.

PURELY ADDITIVE
---------------
No existing table is altered, renamed or dropped. ``eco_threshold_event``,
``eco_day_record``, ``work_day_ledger``, ``config_version``, ``users``,
``organizations`` and every notification, alert and audit table are untouched.
NO ROWS CREATED — no backfill, no rollout seed. ALERT has not run, is not
wired, and the ECO v2 rollout remains OFF, under which this layer records
nothing.

MIGRATION PARENT
----------------
``down_revision = 'f3d8a1c65b29'`` — the current single head, verified rather
than assumed. Ordinary single-parent child: no new head, no merge revision, no
history modified.

NOT EXECUTED AGAINST ANY REAL DATABASE
--------------------------------------
Not applied to production, staging or any shared database. The repository
``.env`` points at production, so no command executing ``env.py`` was run.
Verified against a private in-memory SQLite database and a fresh disposable
PostgreSQL cluster (upgrade, downgrade, upgrade).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.alert.append_only_ddl import (
    ATTEMPT_TABLE,
    NOTIFICATION_TABLE,
    append_only_statements,
    attempt_append_only_statements,
    attempt_drop_statements,
    drop_statements,
    guard_drop_statements,
)

# revision identifiers, used by Alembic.
revision: str = 'a8f4c2e91b07'
down_revision: Union[str, Sequence[str], None] = 'f3d8a1c65b29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    existing = set(sa.inspect(bind).get_table_names())

    if NOTIFICATION_TABLE not in existing:
        op.create_table(
            NOTIFICATION_TABLE,
            sa.Column("id", sa.Integer(), nullable=False),

            # ── R3 logical episode identity, carried verbatim ─────────────
            # RESTRICT, never CASCADE: removing a person must never remove the
            # record that a notification was issued about them, exactly as
            # ``eco_threshold_event`` and ``config_version`` establish.
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("workweek_key", sa.String(length=16), nullable=False),
            sa.Column("tier", sa.String(length=16), nullable=False),
            sa.Column("mode", sa.String(length=16), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),

            # ── the two separate axes ─────────────────────────────────────
            sa.Column("notification_type", sa.String(length=32),
                      nullable=False),
            # Open text, not a DB enum: a future authorized audience must not
            # need a migration, and §32 warns a value surface cannot be
            # corrected later without a breaking change.
            sa.Column("audience", sa.String(length=32), nullable=False),

            # ── the source fact ───────────────────────────────────────────
            sa.Column("originating_event_id", sa.Integer(), nullable=False),
            sa.Column("originating_event_type", sa.String(length=32),
                      nullable=False),
            sa.Column("event_provenance", sa.JSON(), nullable=False),

            # ── recipient: identity, never an address ─────────────────────
            sa.Column("recipient_kind", sa.String(length=16), nullable=False),
            sa.Column("recipient_user_id", sa.Integer(), nullable=False),
            sa.Column("recipient_snapshot", sa.JSON(), nullable=False),

            # ── rendered content, frozen at creation ──────────────────────
            sa.Column("rendered_body", sa.Text(), nullable=False),
            sa.Column("rendered_copy_keys", sa.JSON(), nullable=False),

            # ── eligibility ───────────────────────────────────────────────
            sa.Column("eligibility_outcome", sa.String(length=32),
                      nullable=False),
            sa.Column("eligibility_reason", sa.Text(), nullable=True),
            sa.Column("rollout_state", sa.String(length=16), nullable=False),

            # ── audit. No ``updated_at``: there is no update. ─────────────
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at_utc", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=False),

            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                    ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["originating_event_id"],
                                    ["eco_threshold_event.id"],
                                    ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "organization_id", "user_id", "workweek_key", "tier", "mode",
                "sequence", "notification_type", "audience",
                name="uq_eco_notification_identity"),
            sa.CheckConstraint("sequence >= 1",
                               name="ck_eco_notification_sequence_positive"),
            sa.CheckConstraint("length(created_by) > 0",
                               name="ck_eco_notification_actor"),
            sa.CheckConstraint("length(rendered_body) > 0",
                               name="ck_eco_notification_body_nonempty"),
            sa.CheckConstraint("length(content_hash) = 64",
                               name="ck_eco_notification_content_hash"),
        )

        op.create_index(f"ix_{NOTIFICATION_TABLE}_id", NOTIFICATION_TABLE,
                        ["id"])
        op.create_index(f"ix_{NOTIFICATION_TABLE}_organization_id",
                        NOTIFICATION_TABLE, ["organization_id"])
        op.create_index(f"ix_{NOTIFICATION_TABLE}_user_id",
                        NOTIFICATION_TABLE, ["user_id"])
        op.create_index(f"ix_{NOTIFICATION_TABLE}_workweek_key",
                        NOTIFICATION_TABLE, ["workweek_key"])
        op.create_index(f"ix_{NOTIFICATION_TABLE}_mode", NOTIFICATION_TABLE,
                        ["mode"])
        op.create_index(f"ix_{NOTIFICATION_TABLE}_audience",
                        NOTIFICATION_TABLE, ["audience"])
        op.create_index(f"ix_{NOTIFICATION_TABLE}_originating_event_id",
                        NOTIFICATION_TABLE, ["originating_event_id"])
        # The episode read: "what has been notified for this Worker-week?"
        op.create_index("ix_eco_notification_episode", NOTIFICATION_TABLE,
                        ["organization_id", "user_id", "workweek_key", "tier",
                         "mode"])

        for statement in append_only_statements(dialect):
            op.execute(statement)

    if ATTEMPT_TABLE not in existing:
        op.create_table(
            ATTEMPT_TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("notification_id", sa.Integer(), nullable=False),
            sa.Column("attempt_seq", sa.Integer(), nullable=False),
            sa.Column("recipient_kind", sa.String(length=16), nullable=False),
            sa.Column("recipient_user_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False),
            # The address USED for this attempt — history, not identity.
            sa.Column("destination", sa.String(length=320), nullable=True),
            sa.Column("mode", sa.String(length=16), nullable=False),
            sa.Column("attempted_at_utc", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("failure_detail", sa.Text(), nullable=True),

            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["notification_id"],
                                    [f"{NOTIFICATION_TABLE}.id"],
                                    ondelete="RESTRICT"),
            # Two concurrent workers cannot mint the same attempt number.
            sa.UniqueConstraint("notification_id", "attempt_seq",
                                name="uq_eco_notification_attempt_seq"),
            sa.CheckConstraint(
                "attempt_seq >= 1",
                name="ck_eco_notification_attempt_seq_positive"),
        )
        op.create_index(f"ix_{ATTEMPT_TABLE}_id", ATTEMPT_TABLE, ["id"])
        op.create_index(f"ix_{ATTEMPT_TABLE}_notification_id", ATTEMPT_TABLE,
                        ["notification_id"])
        op.create_index(f"ix_{ATTEMPT_TABLE}_mode", ATTEMPT_TABLE, ["mode"])

        for statement in attempt_append_only_statements(dialect):
            op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    existing = set(sa.inspect(bind).get_table_names())

    # Attempts first: they reference the notification table.
    if ATTEMPT_TABLE in existing:
        for statement in attempt_drop_statements(dialect):
            op.execute(statement)
        for index in (f"ix_{ATTEMPT_TABLE}_mode",
                      f"ix_{ATTEMPT_TABLE}_notification_id",
                      f"ix_{ATTEMPT_TABLE}_id"):
            op.drop_index(index, table_name=ATTEMPT_TABLE)
        op.drop_table(ATTEMPT_TABLE)

    if NOTIFICATION_TABLE in existing:
        for statement in drop_statements(dialect):
            op.execute(statement)
        for index in (
            "ix_eco_notification_episode",
            f"ix_{NOTIFICATION_TABLE}_originating_event_id",
            f"ix_{NOTIFICATION_TABLE}_audience",
            f"ix_{NOTIFICATION_TABLE}_mode",
            f"ix_{NOTIFICATION_TABLE}_workweek_key",
            f"ix_{NOTIFICATION_TABLE}_user_id",
            f"ix_{NOTIFICATION_TABLE}_organization_id",
            f"ix_{NOTIFICATION_TABLE}_id",
        ):
            op.drop_index(index, table_name=NOTIFICATION_TABLE)
        op.drop_table(NOTIFICATION_TABLE)

    # The SHARED guard function LAST: PostgreSQL refuses to drop it while any
    # trigger still depends on it, and one function serves both tables. A
    # PostgreSQL run is what surfaced this — SQLite has no function to drop.
    for statement in guard_drop_statements(dialect):
        op.execute(statement)
