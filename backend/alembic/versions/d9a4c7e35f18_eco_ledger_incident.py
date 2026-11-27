"""ECO platform incident - 24.15's third obligation (owner ruling D-9.1)

ZT-SPEC-ECO-002 v2.0 §24.15.

§24.15 is one sentence with three obligations: refuse to evaluate the day,
record the state ``LEDGER_INTEGRITY_FAULT``, and *"raise a platform incident"*.
Detection, the refusal and the state were already built. Nothing in this
repository persisted the incident, so the third obligation could be claimed but
never demonstrated. This revision adds the one table that makes it true.

WHY A NEW TABLE AND NOT AN EXISTING ONE
----------------------------------------
``eco_threshold_event``
    A faulted day is never evaluated ("evaluate nothing"), so it produces no
    threshold crossing. There is no event to hang this on.

``eco_notification`` / ``eco_notification_delivery_attempt``
    Week-shaped, with a NOT NULL foreign key to ``eco_threshold_event`` that a
    faulted day cannot satisfy - and a notification besides. §33.1 registers
    four audiences and §33.2/§33.3 register exactly two notification
    identifiers, both for "threshold reached"; neither registers anything for a
    ledger defect.

``eco_day_administrator_alert``
    §24.3's alert, about a Tenant CONFIGURATION defect and addressed to an
    administrator. A ledger defect is a DATA defect and is addressed to nobody.

``work_evidence_chain`` / ``audit_log``
    The first records the evidence that produced a day; this incident is about
    that evidence being inadmissible. The second records who did what, and no
    actor did this.

So the incident gets its own storage, ``eco_notification`` and
``eco_day_administrator_alert`` are untouched, and no notification table gains
a column. That isolation is what keeps GV-07 and GV-18 unaffected by this
revision: no shared table, no shared constraint, no altered column.

THE IDENTITY
------------
``UNIQUE (ledger_id)`` - the authoritative ledger-fault identity, and the whole
idempotency rule.

``work_day_ledger`` is version-keyed on ``(organization_id, user_id, work_date,
ledger_version)`` and its writer appends one immutable version per fault it
establishes. Re-running the producer over an already-faulted day writes NO new
version, so a repeated observation of the same authoritative fault finds the
same ``ledger_id`` and raises nothing. A genuinely new authoritative fact IS a
new version, so it carries a different ``ledger_id`` and stays distinguishable.

A day-shaped key would have been too coarse - a day that faults again on
genuinely different evidence is a second defect, and a day key would report it
as one. ``eco_day_record``'s generation key would have been worse: a faulted
day has no evaluation generation at all, so using it would mean inventing one.

WHAT THE TABLE DOES NOT HAVE
-----------------------------
No audience, recipient, recipient snapshot, channel, mode, delivery attempt,
outcome, reason code, suppression, correlation identifier or rendered copy.
Every one of those belongs to a notification. This is operational evidence that
a Tenant's ledger holds a defect.

APPEND-ONLY (R5)
----------------
The same trigger protection the notification history carries, emitted
explicitly here because ``op.create_table`` builds its own ``Table`` object and
the ORM-table listener does not fire for it. Without this a migrated database
would lack the triggers while a test database had them, and every below-ORM
test would pass vacuously against production's real shape.

ADDITIVE AND INERT
------------------
One new table. No existing table is altered, no column added or dropped, no row
read or rewritten. It ships EMPTY, and it fills only when a ledger fault is
newly established - which is a pre-existing, already-detected condition.
Upgrading changes no behaviour on a healthy Tenant.

DOWNGRADE
---------
Drops what it added, and REFUSES if any incident exists: discarding the
evidence that a Tenant's ledger holds a defect is a decision, not a schema
step.

Revision ID: d9a4c7e35f18
Revises: c2e8f5b71d94
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

from app.eco_ledger_incident.append_only_ddl import (
    INCIDENT_TABLE,
    append_only_statements,
    drop_statements,
)

revision = "d9a4c7e35f18"
down_revision = "c2e8f5b71d94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        INCIDENT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ledger_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_date", sa.DateTime(timezone=False), nullable=False),
        sa.Column("ledger_version", sa.Integer(), nullable=False),
        sa.Column("incident_type", sa.String(length=48), nullable=False),
        sa.Column("ledger_state", sa.String(length=32), nullable=False),
        sa.Column("integrity_fault_reason", sa.Text(), nullable=False),
        sa.Column("raised_by", sa.String(length=64), nullable=False,
                  server_default="SYSTEM"),
        sa.Column("raised_at_utc", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["ledger_id"], ["work_day_ledger.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("ledger_id", name="uq_eco_ledger_incident_ledger"),
        sa.CheckConstraint("incident_type IN ('LEDGER_INTEGRITY_FAULT')",
                           name="ck_eco_ledger_incident_type"),
        sa.CheckConstraint("ledger_state = 'LEDGER_INTEGRITY_FAULT'",
                           name="ck_eco_ledger_incident_state"),
        sa.CheckConstraint("ledger_version >= 1",
                           name="ck_eco_ledger_incident_version_positive"),
        sa.CheckConstraint("length(integrity_fault_reason) > 0",
                           name="ck_eco_ledger_incident_reason_nonempty"),
    )
    op.create_index("ix_%s_organization_id" % INCIDENT_TABLE, INCIDENT_TABLE,
                    ["organization_id"])
    op.create_index("ix_%s_user_id" % INCIDENT_TABLE, INCIDENT_TABLE,
                    ["user_id"])
    op.create_index("ix_%s_work_date" % INCIDENT_TABLE, INCIDENT_TABLE,
                    ["work_date"])
    op.create_index("ix_eco_ledger_incident_org_raised", INCIDENT_TABLE,
                    ["organization_id", "raised_at_utc"])

    # R5, emitted explicitly - see the module docstring.
    for statement in append_only_statements(op.get_bind().dialect.name):
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()

    held = bind.execute(
        sa.text("SELECT count(*) FROM %s" % INCIDENT_TABLE)  # noqa: S608
    ).scalar()
    if held:
        raise RuntimeError(
            "cannot downgrade: %s ECO platform incident(s) exist. Dropping "
            "the table would discard the evidence that a Tenant's work ledger "
            "holds an integrity defect." % held
        )

    for statement in drop_statements(bind.dialect.name):
        op.execute(statement)

    op.drop_index("ix_eco_ledger_incident_org_raised",
                  table_name=INCIDENT_TABLE)
    for suffix in ("work_date", "user_id", "organization_id"):
        op.drop_index("ix_%s_%s" % (INCIDENT_TABLE, suffix),
                      table_name=INCIDENT_TABLE)
    op.drop_table(INCIDENT_TABLE)
