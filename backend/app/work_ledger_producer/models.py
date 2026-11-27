"""
Append-only producer cutover history (owner ruling D18-F1).
===========================================================

The forward-only Work Ledger producer needs one fact per tenant: the instant
from which its evidence may be sealed. That fact is the MAXIMUM over this
append-only history.

WHY THIS REPLACED A MUTABLE ROW
-------------------------------
The first implementation stored one mutable row per tenant and defended
monotonicity with a service comparison, a guarded UPDATE and ORM listeners. The
post-implementation audit demonstrated four bypasses that moved the watermark
EARLIER — SQLAlchemy Core UPDATE, ORM bulk ``query.update()``, ORM ``delete()``
then re-set, and Core DELETE then re-set — restoring eligibility for historical
days. Ruling M had already established that ORM listeners are insufficient
wherever Core statements can reach the same rows.

MONOTONIC BY CONSTRUCTION
-------------------------
Because the authoritative value is ``MAX(producer_cutover_at)``:

* inserting an EARLIER row is arithmetically inert — it cannot lower a maximum;
* the only way to lower the watermark is to UPDATE or DELETE the maximum row,
  and the database refuses both (``append_only_ddl``);
* two concurrent inserts both succeed and the later value simply wins, so no
  optimistic token is needed and there is no lost update to lose.

Deleting history is refused outright, so the delete-then-reset route that
defeated the mutable row is structurally unavailable.

FIVE LAYERS, DELIBERATELY REDUNDANT
-----------------------------------
1. ``MAX()`` semantics (an earlier row is inert)
2. service-level comparison (``cutover.set_producer_cutover``)
3. ORM ``before_update`` refusal
4. ORM ``before_delete`` refusal
5. database triggers refusing UPDATE and DELETE

Layers 3-4 are defence in depth, never the guarantee: layer 5 is what stops a
statement the ORM never sees.

Registered on the shared ``database.database.Base`` metadata, following the
``app/work_ledger/models.py`` precedent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String,
    event, inspect as sa_inspect, text,
)

from database.database import Base

from .append_only_ddl import CUTOVER_HISTORY_TABLE, append_only_statements


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProducerCutoverMovedBackward(Exception):
    """An attempt to move a tenant's cutover to an earlier instant.

    Refused by owner ruling D18-F1. Moving it earlier would make historical
    days eligible for sealing that were ineligible when they happened, which is
    the forward-only guarantee itself.
    """


class ProducerCutoverImmutable(Exception):
    """An attempt to UPDATE or DELETE a cutover history row.

    The history is append-only. A correction is a new row with a LATER instant;
    an existing row is never rewritten and never removed.
    """


class WorkLedgerProducerCutoverHistory(Base):
    """One recorded cutover value for one tenant. Insert-only."""

    __tablename__ = CUTOVER_HISTORY_TABLE

    id = Column(Integer, primary_key=True, index=True)

    #: MANY rows per tenant — this is a history, not a current-value row. No
    #: unique constraint on organization_id. RESTRICT because the history
    #: governs whether a tenant's days may be sealed at all, so it must not
    #: vanish below the application as a side effect of another delete.
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: A recorded watermark value. The AUTHORITATIVE watermark is the MAXIMUM
    #: of these for the organization. NOT NULL and no default: an unconfigured
    #: tenant has no rows and is refused, never defaulted.
    producer_cutover_at = Column(DateTime(timezone=True), nullable=False)

    #: Audit. ``set_by`` follows the established durable-attribution
    #: convention — an id where one exists, SYSTEM otherwise
    #: (``RolloutControl.last_transition_by``,
    #: ``ConfigVersion.captured_by_label``) — with NO foreign key, so removing
    #: a person never removes the record of what they set.
    set_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    set_by = Column(String(64), nullable=False)

    __table_args__ = (
        # The authoritative read: MAX(producer_cutover_at) per organization.
        Index("ix_wl_cutover_history_org_at",
              "organization_id", "producer_cutover_at"),
        CheckConstraint("length(set_by) > 0", name="ck_wl_cutover_history_actor"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<WorkLedgerProducerCutoverHistory org={self.organization_id} "
            f"at={self.producer_cutover_at} by={self.set_by}>"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Layer 5 — database-level append-only enforcement
# ═════════════════════════════════════════════════════════════════════════════
# Attached to the TABLE so `Base.metadata.create_all()` carries it. The Alembic
# revision emits the SAME statements explicitly, because `op.create_table`
# builds its own Table object and this listener does not fire for it.

@event.listens_for(WorkLedgerProducerCutoverHistory.__table__, "after_create")
def _install_append_only_guard(target, connection, **kw):
    """Emit the dialect's append-only statements, if the dialect has any.

    A dialect this repository does not support gets no statements rather than a
    wrong guess; ``tests`` assert which dialects are covered.
    """
    for statement in append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


# ═════════════════════════════════════════════════════════════════════════════
# Layers 3 and 4 — ORM defence in depth
# ═════════════════════════════════════════════════════════════════════════════

@event.listens_for(WorkLedgerProducerCutoverHistory, "before_update",
                   propagate=True)
def _reject_history_update(mapper, connection, target):
    raise ProducerCutoverImmutable(
        f"cutover history id={target.id} cannot be updated: the history is "
        f"append-only (D18-F1). Record a LATER value as a new row."
    )


@event.listens_for(WorkLedgerProducerCutoverHistory, "before_delete",
                   propagate=True)
def _reject_history_delete(mapper, connection, target):
    raise ProducerCutoverImmutable(
        f"cutover history id={target.id} cannot be deleted: removing history "
        f"would lower the effective watermark and re-open historical days."
    )


@event.listens_for(WorkLedgerProducerCutoverHistory.producer_cutover_at, "set",
                   active_history=True)
def _reject_value_rewrite(target, value, oldvalue, initiator):
    """Refuse rewriting a recorded value at the attribute level."""
    if isinstance(oldvalue, datetime) and value != oldvalue:
        raise ProducerCutoverImmutable(
            f"a recorded cutover value is immutable: "
            f"{oldvalue!r} -> {value!r} was refused"
        )
    return value


__all__ = [
    "WorkLedgerProducerCutoverHistory",
    "ProducerCutoverMovedBackward",
    "ProducerCutoverImmutable",
    "CUTOVER_HISTORY_TABLE",
]
