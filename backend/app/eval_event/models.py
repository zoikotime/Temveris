"""
The append-only ECO threshold event history (owner ruling D1).
==============================================================

One row per lifecycle fact. Nothing is ever updated or deleted:

* a crossing is a row;
* a withdrawal is a NEW row that references the crossing it withdraws;
* a re-crossing is a NEW row at the next sequence.

WHY THE ORIGINAL CARRIES NO ``withdrawn_by`` POINTER
----------------------------------------------------
Populating a pointer on the crossing when it is later withdrawn would require
an UPDATE of a row D1 declares immutable, and the database triggers below
refuse exactly that. So the reference runs the only direction an append-only
store allows: the WITHDRAWAL row carries ``supersedes_event_id``, naming the
crossing it closes. "Is this crossing still open?" is therefore a question
about whether a withdrawal referencing it exists — a read, not a stored flag,
and one that cannot go stale.

R-21.3.1 is satisfied either way: the original is never deleted, never
back-dated and never hidden.

THE UNIQUENESS BOUNDARY (D6) — AND A DEVIATION THE OWNER MUST CONFIRM
---------------------------------------------------------------------
The ruled boundary is::

    (organization_id, user_id, workweek_key, tier, sequence, mode)

The four-field tuple ``(organization_id, user_id, workweek_key, tier)`` remains
the LOGICAL identity of a crossing episode, but it cannot be the physical
constraint: §21.4 requires a second primary at ``sequence = 2`` after a
withdrawal, and D8 requires shadow and operational lifecycles to be
independently addressable. Both dimensions therefore join the key.

The ruled six-tuple, however, cannot be satisfied as written, and the reason is
arithmetic rather than stylistic. A withdrawal is a NEW ROW in the same episode
(§21.3), so it needs a ``sequence`` value:

* give it one of its own and it consumes 2, forcing the re-crossing to 3 —
  §21.4 says the re-crossing is 2;
* let it share the sequence of the crossing it closes and the six-tuple
  constraint refuses the insert.

So the column below adds ``event_type`` to the constraint, making it::

    (organization_id, user_id, workweek_key, tier, sequence, mode, event_type)

and the withdrawal shares the sequence of the crossing it closes, so one
numbered sequence is one complete lifecycle: crossing 1, withdrawal 1,
re-crossing 2. This is a RELAXATION of what was ruled — a seven-column
constraint permits rows a six-column one refuses — and is therefore reported
for confirmation rather than treated as settled.

What is NOT relaxed is the invariant the ruling actually protects: "at most one
open, non-withdrawn primary per (org, user, workweek_key, tier, mode)". That
still holds, and still holds in the DATABASE rather than in application
discipline — but only because of how the sequence is derived, which is worth
stating precisely because the obvious derivation does NOT work.

Taking the new sequence as ``max(sequence) + 1`` leaves a real hole: a run that
reads "nothing open" before a competitor commits, but reads ``max(sequence)``
after, computes a HIGHER number, lands on a different identity and succeeds —
two open primaries with no constraint violated. So the sequence is instead one
past the count of WITHDRAWALS in the episode (``service._next_sequence``). With
W withdrawals, a crossing may exist at each of sequences 1..W+1 at most once
(this constraint), and exactly W of them are withdrawn — each withdrawal naming
a distinct crossing, enforced by ``uq_eco_threshold_event_withdrawal_target``,
which also makes a double withdrawal impossible. At most one crossing is
therefore left open, and two racing crossings compute the SAME sequence and
collide here.

The database is the authority. A concurrent replay loses the insert race and
re-reads the winner; there is no in-memory lock and no "has notified" boolean.

WHAT THIS MODEL DOES NOT HAVE
-----------------------------
* No ``updated_at`` — there is no update.
* No delivery, read-receipt or dispatch column. D7: EVAL-EVENT generates
  events; NOTIF delivers them, later, and owns its own state.
* No reviewer recipient, and no recipient RESOLUTION. ``recipient_snapshot``
  now freezes all four §33.1 audiences (ruling D-3), but the resolving is
  ``app.recipient_authority``'s: this module stores the frozen answer and
  never re-derives it. No reviewer model, role, permission or configuration is
  created here, and an audience that resolved to nobody is recorded with its
  reason rather than dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint, event, text,
)

from database.database import Base

from .append_only_ddl import EVENT_TABLE, append_only_statements


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ThresholdEventImmutable(Exception):
    """An attempt to UPDATE or DELETE a recorded threshold event.

    The history is append-only (D1). A withdrawal is a new row; a re-crossing
    is a new row. An event store that silently accepts a rewrite has stopped
    being evidence.
    """


class EcoThresholdEvent(Base):
    """One recorded ECO threshold lifecycle fact. Insert-only."""

    __tablename__ = EVENT_TABLE

    id = Column(Integer, primary_key=True, index=True)

    # ── identity ────────────────────────────────────────────────────────────
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    #: The canonical W4 workweek key: the local week START DATE, ISO-formatted.
    workweek_key = Column(String(16), nullable=False, index=True)
    #: ``PRIMARY`` for this increment. Text, not a DB enum: §32 warns a value
    #: surface cannot be corrected later without a breaking change.
    tier = Column(String(16), nullable=False)
    #: 1 for a first crossing; 2+ after a withdrawal (§21.4).
    sequence = Column(Integer, nullable=False)
    #: OPERATIONAL or SHADOW (D8). Shadow rows are logically isolated.
    mode = Column(String(16), nullable=False, index=True)

    # ── what happened ───────────────────────────────────────────────────────
    event_type = Column(String(32), nullable=False)

    #: Set ONLY on a withdrawal: the crossing it closes. The reverse pointer
    #: deliberately does not exist — see the module docstring.
    supersedes_event_id = Column(
        Integer, ForeignKey(f"{EVENT_TABLE}.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # ── the comparison, frozen ──────────────────────────────────────────────
    weekly_total_minutes = Column(Integer, nullable=False)
    threshold_minutes = Column(Integer, nullable=False)
    threshold_config_version = Column(String(64), nullable=True)

    # ── retroactivity (D4) ──────────────────────────────────────────────────
    #: True when the crossing rests on corrected evidence. Determined from
    #: persisted lineage, never from the current time.
    is_retroactive = Column(Boolean, nullable=False, default=False)
    #: The lineage that justifies the flag, so the determination is auditable
    #: rather than merely asserted.
    caused_by = Column(JSON, nullable=False, default=dict)

    # ── provenance (D2) ─────────────────────────────────────────────────────
    #: The full WeekEvaluation provenance, frozen at creation. Never
    #: recomputed: a later day-record supersession must not change what this
    #: event says it was built from.
    week_provenance = Column(JSON, nullable=False, default=dict)

    # ── recipients (D5, increment-scoped: the Worker only) ──────────────────
    recipient_snapshot = Column(JSON, nullable=False, default=dict)
    worker_recipient = Column(Integer, nullable=False)

    # ── audit ───────────────────────────────────────────────────────────────
    created_at_utc = Column(DateTime(timezone=True), nullable=False,
                            default=_utcnow)
    created_by = Column(String(64), nullable=False)
    detail = Column(Text, nullable=True)

    __table_args__ = (
        # D6: the database-authoritative uniqueness boundary, plus
        # ``event_type`` — see the module docstring for why the ruled
        # six-column form cannot hold a withdrawal row.
        UniqueConstraint(
            "organization_id", "user_id", "workweek_key", "tier", "sequence",
            "mode", "event_type", name="uq_eco_threshold_event_identity",
        ),
        # At most one row may supersede any given row: a crossing is withdrawn
        # once, and a withdrawal is followed by one re-crossing. NULLs are
        # distinct in both supported dialects, so the many rows that supersede
        # nothing are unaffected.
        UniqueConstraint(
            "supersedes_event_id",
            name="uq_eco_threshold_event_withdrawal_target",
        ),
        # The lifecycle read: "is there an open crossing for this episode?"
        Index("ix_eco_threshold_event_episode",
              "organization_id", "user_id", "workweek_key", "tier", "mode"),
        Index("ix_eco_threshold_event_supersedes", "supersedes_event_id"),
        CheckConstraint("sequence >= 1",
                        name="ck_eco_threshold_event_sequence_positive"),
        CheckConstraint("weekly_total_minutes >= 0",
                        name="ck_eco_threshold_event_total_non_negative"),
        CheckConstraint("threshold_minutes >= 0",
                        name="ck_eco_threshold_event_threshold_non_negative"),
        CheckConstraint("length(created_by) > 0",
                        name="ck_eco_threshold_event_actor"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<EcoThresholdEvent org={self.organization_id} "
            f"user={self.user_id} {self.workweek_key} {self.event_type} "
            f"seq={self.sequence} {self.mode}>"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Database-level append-only enforcement — the authority
# ═════════════════════════════════════════════════════════════════════════════
# Attached to the TABLE so `Base.metadata.create_all()` carries it. The Alembic
# revision emits the SAME statements explicitly, because `op.create_table`
# builds its own Table object and this listener does not fire for it.

@event.listens_for(EcoThresholdEvent.__table__, "after_create")
def _install_append_only_guard(target, connection, **kw):
    """Emit the dialect's append-only statements, if the dialect has any."""
    for statement in append_only_statements(connection.dialect.name):
        connection.execute(text(statement))


# ═════════════════════════════════════════════════════════════════════════════
# ORM refusal — defence in depth, never the guarantee
# ═════════════════════════════════════════════════════════════════════════════

@event.listens_for(EcoThresholdEvent, "before_update", propagate=True)
def _reject_event_update(mapper, connection, target):
    raise ThresholdEventImmutable(
        f"threshold event id={target.id} cannot be updated: the history is "
        f"append-only (D1). A withdrawal or a re-crossing is a NEW row."
    )


@event.listens_for(EcoThresholdEvent, "before_delete", propagate=True)
def _reject_event_delete(mapper, connection, target):
    raise ThresholdEventImmutable(
        f"threshold event id={target.id} cannot be deleted: R-21.3.1 requires "
        f"the original to be retained, never deleted, hidden or back-dated."
    )


__all__ = [
    "EcoThresholdEvent",
    "ThresholdEventImmutable",
    "EVENT_TABLE",
]
