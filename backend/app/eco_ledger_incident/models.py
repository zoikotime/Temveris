"""
The persisted ECO platform incident.
====================================

ZT-SPEC-ECO-002 v2.0 §24.15, owner ruling D-9.1.

ONE ROW PER AUTHORITATIVE LEDGER FAULT VERSION.

THE IDENTITY IS THE LEDGER FAULT ROW, NOT THE DAY
-------------------------------------------------
``ledger_id`` is UNIQUE, and that single constraint is the whole idempotency
rule. It was chosen over the day-shaped keys this repository already uses, and
the difference is not cosmetic:

``(organization_id, user_id, work_date)``
    Too coarse. A day whose fault is corrected, and which then faults again on
    genuinely different evidence, is a SECOND authoritative fact. This key
    would silently swallow it, and an operator would see one incident where
    two defects occurred.

``(organization_id, user_id, work_date, evaluation_generation)``
    ``EcoDayRecord``'s key, and wrong here for a simpler reason: a faulted day
    is never evaluated at all (§24.15, "evaluate nothing"), so it has no
    evaluation generation. Copying that key would mean inventing one.

``work_day_ledger.id``
    Exactly the authoritative ledger-fault identity. ``record_integrity_fault``
    writes one immutable version per fault it establishes, keyed by
    ``uq_work_day_ledger_natural_key (organization_id, user_id, work_date,
    ledger_version)``. Re-running the producer over an already-faulted day
    writes NO new version -- so a repeated observation of the same fault finds
    the same ``ledger_id`` and raises nothing. A genuinely new authoritative
    fact IS a new version, so it has a different ``ledger_id`` and stays
    distinguishable.

``organization_id``, ``user_id``, ``work_date`` and ``ledger_version`` are
carried alongside it. They are functionally determined by ``ledger_id``, and
they are stored anyway so that an organization-scoped read of its own open
defects needs no join into the ledger -- the same reason the ledger itself
freezes its context rather than re-deriving it.

WHAT IS DELIBERATELY ABSENT
---------------------------
No audience. No recipient, and no recipient snapshot. No channel, no mode, no
delivery attempt, no outcome, no reason code, no suppression, no correlation
identifier, no dispatch-pass bookkeeping, no rendered copy. Every one of those
belongs to a notification, and §24.15 does not describe a notification: it
describes evidence that a ledger is defective. See ``outcomes.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint, event, text,
)

from database.database import Base

from .append_only_ddl import INCIDENT_TABLE, append_only_statements
from .outcomes import LEDGER_INTEGRITY_FAULT, IncidentType

_TYPE_SQL = ", ".join(f"'{member.value}'" for member in IncidentType)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EcoLedgerIncident(Base):
    """One immutable record that one authoritative ledger fault was raised."""

    __tablename__ = INCIDENT_TABLE

    id = Column(Integer, primary_key=True)

    #: The authoritative ledger fault this incident is about, and the whole of
    #: its identity. RESTRICT because an incident that outlived its evidence,
    #: or evidence that could be deleted out from under an incident, would
    #: leave an unverifiable claim behind.
    ledger_id = Column(
        Integer, ForeignKey("work_day_ledger.id", ondelete="RESTRICT"),
        nullable=False,
    )

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    #: RESTRICT for the reason ``work_day_ledger.user_id`` gives: removing a
    #: person must never remove history.
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Mirrors ``work_day_ledger.work_date`` exactly, including its type: a
    #: calendar label stored as a naive midnight datetime, never an instant.
    work_date = Column(DateTime(timezone=False), nullable=False, index=True)

    #: The ledger version that established the fault. Part of the authoritative
    #: fact, not a derived convenience: it is what makes "which fault was this"
    #: answerable from the incident alone.
    ledger_version = Column(Integer, nullable=False)

    incident_type = Column(String(48), nullable=False,
                           default=IncidentType.LEDGER_INTEGRITY_FAULT.value)

    #: The ledger state asserted at raise time. Stored so the incident states
    #: its own premise rather than depending on the ledger still saying so.
    ledger_state = Column(String(32), nullable=False,
                          default=LEDGER_INTEGRITY_FAULT)

    #: ``work_day_ledger.integrity_fault_reason``, verbatim. NOT NULL here
    #: although it is nullable there, because an incident with no reason is an
    #: assertion that something is wrong with no statement of what.
    integrity_fault_reason = Column(Text, nullable=False)

    #: The actor string the ledger writer ran under. Operational provenance for
    #: the incident record, not an audience and not a recipient.
    raised_by = Column(String(64), nullable=False)

    raised_at_utc = Column(DateTime(timezone=True), nullable=False,
                           default=_utcnow)

    __table_args__ = (
        # The whole idempotency rule. See the module docstring.
        UniqueConstraint("ledger_id", name="uq_eco_ledger_incident_ledger"),
        # The organization-scoped operational read: "what is broken here, and
        # when did we find out".
        Index("ix_eco_ledger_incident_org_raised",
              "organization_id", "raised_at_utc"),
        CheckConstraint("incident_type IN (%s)" % _TYPE_SQL,
                        name="ck_eco_ledger_incident_type"),
        # §24.15 attaches this incident to one ledger state. A row asserting
        # any other state is unrepresentable rather than merely discouraged.
        CheckConstraint(f"ledger_state = '{LEDGER_INTEGRITY_FAULT}'",
                        name="ck_eco_ledger_incident_state"),
        CheckConstraint("ledger_version >= 1",
                        name="ck_eco_ledger_incident_version_positive"),
        CheckConstraint("length(integrity_fault_reason) > 0",
                        name="ck_eco_ledger_incident_reason_nonempty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"<EcoLedgerIncident org={self.organization_id} "
                f"user={self.user_id} {self.work_date} "
                f"ledger={self.ledger_id} v{self.ledger_version}>")


# ===========================================================================
# Database-level append-only enforcement -- the authority (R5)
# ===========================================================================
# Attached to the TABLE so `Base.metadata.create_all()` carries it. The Alembic
# revision emits the SAME statements explicitly, because `op.create_table`
# builds its own Table object and this listener does not fire for it.

@event.listens_for(EcoLedgerIncident.__table__, "after_create")
def _install_append_only_guard(target, connection, **kw):
    """Emit the dialect's append-only statements, if the dialect has any."""
    for statement in append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


__all__ = ["EcoLedgerIncident"]
