"""
Session-boundary provenance — append-only SQLAlchemy model (§45.D-15).
======================================================================

Owner ruling §45.D-15, closed through decisions D15-1-B, D15-2-B, D15-3-A and
D15-4-A. Every writer that sets ``clock_sessions.clock_in_time`` or
``clock_sessions.clock_out_time`` records, immutably and in the same
transaction as the boundary write, which of the five owner-approved provenance
values that boundary represents.

WHY A SEPARATE TABLE AND NOT A COLUMN
-------------------------------------
``POST /employees/time-adjustments`` rewrites ``clock_in_time`` and
``clock_out_time`` IN PLACE on a session that may already have been closed by
the Worker. A single mutable column on ``clock_sessions`` cannot satisfy the
ruling, because every available value of it is wrong:

* leaving it at ``WORKER`` after an admin rewrite attributes an
  admin-authored timestamp to the Worker — the exact failure §45.D-15 names,
  "a timestamp's value is NOT evidence of its origin";
* overwriting it to ``ADMIN_OVERRIDE`` makes the column mutable, so the
  required immutability is not delivered, and erases the fact that a genuine
  Worker close ever existed.

So provenance lives here, one immutable row per boundary write, and
``clock_sessions`` keeps NO provenance column at all. The session row remains
the projection of the current timestamps, which is what it already was.

WHY THE ORIGIN CANNOT BE DERIVED INSTEAD
----------------------------------------
The abandoned-session sweep closes at ``max(last_heartbeat, clock_in)``, so a
synthesized closure can land exactly on the clock-in instant. A session whose
``clock_out_time`` equals its ``clock_in_time`` is therefore indistinguishable,
by value alone, from a Worker who clocked out instantly. Nothing in the schema
recorded which it was. That is why §45.D-15 requires the producer to record the
origin and forbids reconstructing it, and why no backfill of pre-existing rows
is possible: those sessions are permanently unattributable.

VOCABULARY
----------
The five approved values come from the existing Work Ledger ``Provenance``
enum, reused verbatim. No new enum, no duplicate enum, no sixth value. The
three interval-level values (``CLASSIFIER_DERIVED``, ``BREAK_REPAIR``,
``BREAK_DERIVED``) belong to the classifier and are not boundary origins, so
they are rejected here.

``SYSTEM`` never appears as a provenance. It is an ACTOR label, meaning "no
human performed this operation"; the provenance of an unattended closure is the
specific ``STALE_AUTOCLOSE`` / ``ABANDONED_AUTOCLOSE`` / ``OFFBOARDING``.

Registered on the shared ``database.database.Base`` metadata, following the
``app/work_ledger/models.py`` and ``app/commercial/models.py`` precedent — a
domain-local model module rather than an edit to the shared
``database/models.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, FrozenSet

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String,
    UniqueConstraint, event, text,
)

from database.database import Base

#: The five approved boundary origins are members of this enum, reused
#: verbatim. The two frozensets in that module
#: (``VERIFIED_DEPARTURE_PROVENANCE`` / ``SYNTHESIZED_CLOSURE_PROVENANCE``) are
#: deliberately NOT imported: the sets below are spelled out because they
#: describe boundary admissibility, not departure verification, and
#: ``tests/test_clock_session_boundary.py`` pins the relationship between them
#: so it cannot drift unnoticed.
from app.work_ledger.states import Provenance

from .errors import BoundaryEventImmutable


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Aware because every instant column here is
    ``timezone=True`` and §16.4 prohibits naive instants."""
    return datetime.now(timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# D15-2-B — the two boundaries, and what each may carry
# ═════════════════════════════════════════════════════════════════════════════

#: The instant a session opened.
BOUNDARY_CLOCK_IN = "clock_in"

#: The instant a session closed.
BOUNDARY_CLOCK_OUT = "clock_out"

BOUNDARIES: FrozenSet[str] = frozenset({BOUNDARY_CLOCK_IN, BOUNDARY_CLOCK_OUT})

#: Provenance admissible on the OPEN boundary.
#:
#: A clock-in has no closure at the moment it is written, so the three
#: closure-only values are structurally unreachable here. Only a Worker's own
#: clock-in and an administrative correction can author a session's start.
#:
#: This set is identical to ``VERIFIED_DEPARTURE_PROVENANCE`` — both describe
#: "a person did this deliberately" — but it is spelled out rather than aliased,
#: because the Work Ledger set is about DEPARTURE and reusing its name for an
#: arrival would make the vocabulary lie. ``tests`` pin the equality so the
#: relationship cannot drift unnoticed.
OPEN_BOUNDARY_PROVENANCE: FrozenSet[Provenance] = frozenset({
    Provenance.WORKER,
    Provenance.ADMIN_OVERRIDE,
})

#: Provenance admissible on the CLOSE boundary: all five approved values.
CLOSE_BOUNDARY_PROVENANCE: FrozenSet[Provenance] = frozenset({
    Provenance.WORKER,
    Provenance.ADMIN_OVERRIDE,
    Provenance.STALE_AUTOCLOSE,
    Provenance.ABANDONED_AUTOCLOSE,
    Provenance.OFFBOARDING,
})

#: The lookup the service validates against, and the CHECK constraint mirrors.
ALLOWED_PROVENANCE: Dict[str, FrozenSet[Provenance]] = {
    BOUNDARY_CLOCK_IN: OPEN_BOUNDARY_PROVENANCE,
    BOUNDARY_CLOCK_OUT: CLOSE_BOUNDARY_PROVENANCE,
}


def _sql_literal_set(values) -> str:
    """Render provenance values as a SQL ``IN`` list.

    Generated from the enum rather than typed out, so the database constraint
    and the Python validation cannot disagree about the vocabulary.
    """
    return ", ".join(f"'{v.value}'" for v in sorted(values, key=lambda p: p.value))


#: One constraint enforcing all three of: a valid boundary, an approved
#: provenance, and the boundary/provenance pairing. A ``clock_in`` row carrying
#: ``STALE_AUTOCLOSE`` cannot satisfy either branch, so it cannot exist —
#: structurally, not by convention. SQLite and PostgreSQL both enforce CHECK.
_PAIRING_SQL = (
    f"(boundary = '{BOUNDARY_CLOCK_IN}' AND provenance IN "
    f"({_sql_literal_set(OPEN_BOUNDARY_PROVENANCE)}))"
    " OR "
    f"(boundary = '{BOUNDARY_CLOCK_OUT}' AND provenance IN "
    f"({_sql_literal_set(CLOSE_BOUNDARY_PROVENANCE)}))"
)

_BOUNDARY_SQL = (
    f"boundary IN ('{BOUNDARY_CLOCK_IN}', '{BOUNDARY_CLOCK_OUT}')"
)


# ═════════════════════════════════════════════════════════════════════════════
# D15-3-A — the append-only table
# ═════════════════════════════════════════════════════════════════════════════

from .append_only_ddl import boundary_append_only_statements

class ClockSessionBoundaryEvent(Base):
    """One immutable record of one boundary write.

    Append-only: there is no update path and no delete path. A later write to
    the same ``(session_id, boundary)`` appends a new row with the next
    ``sequence``; the earlier row is never touched.
    """

    __tablename__ = "clock_session_boundary_event"

    id = Column(Integer, primary_key=True, index=True)

    #: The session whose boundary was written.
    #:
    #: RESTRICT by owner ruling D15-4-A. ``clock_sessions`` children are
    #: ordinarily CASCADE (``break_sessions``, ``offline_intervals``), but those
    #: are operational detail; this is protected historical evidence, and a
    #: cascade would let an ordinary deletion destroy the provenance the whole
    #: increment exists to preserve.
    session_id = Column(
        Integer,
        ForeignKey("clock_sessions.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: The SUBJECT Worker — whose attendance this boundary belongs to. NOT the
    #: actor: an admin correcting Bob's session produces a row with
    #: ``user_id`` = Bob and ``actor_label`` = the admin.
    #:
    #: Denormalised from ``clock_sessions.user_id`` (itself NOT NULL) because
    #: ``employees.router._protected_history`` probes each protected table with
    #: a direct, join-free ``SELECT 1 FROM <table> WHERE <column> = :uid``.
    #: Every one of the five existing protected tables carries ``user_id`` for
    #: exactly this reason. RESTRICT, per D15-4-A.
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: ``clock_in`` or ``clock_out``.
    boundary = Column(String(16), nullable=False)

    #: The timestamp that was written to the session row. Kept here as well as
    #: on the session so the history remains legible after a later rewrite
    #: replaces the projection.
    boundary_at = Column(DateTime(timezone=True), nullable=False)

    #: Which of the five approved origins this boundary represents. Never
    #: defaulted, never inferred — the writer states it or the write is refused.
    provenance = Column(String(32), nullable=False, index=True)

    #: Durable actor attribution, following ruling F's
    #: ``config_version.captured_by_label`` and its cited precedent
    #: ``rollout.RolloutControl.last_transition_by`` — "an id where one exists,
    #: SYSTEM otherwise".
    #:
    #: DELIBERATELY NOT A FOREIGN KEY (owner ruling D15-4-A). A FK would break
    #: the ruling whichever action it carried: CASCADE deletes the event,
    #: RESTRICT makes every admin who ever corrected a timestamp permanently
    #: undeletable, and SET NULL *writes to this row* when the actor is deleted
    #: — mutating an event the ruling requires to be immutable. With no FK,
    #: deleting an actor touches this table in no way at all.
    #:
    #: An identifying label and nothing more: no name, no role history.
    actor_label = Column(String(64), nullable=False)

    #: When the provenance event itself was recorded. Distinct from
    #: ``boundary_at`` so an administrative correction of an earlier timestamp
    #: is representable without either field lying.
    written_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    #: Monotonic per ``(session_id, boundary)``, starting at 1. Ordering does
    #: not rely on ``id``, which is a global surrogate and would interleave
    #: sessions. See ``service.append_boundary_event`` for the allocation
    #: strategy and its concurrency guarantees.
    sequence = Column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(_BOUNDARY_SQL, name="ck_csbe_boundary_vocabulary"),
        CheckConstraint(_PAIRING_SQL, name="ck_csbe_boundary_provenance_pairing"),
        CheckConstraint("sequence >= 1", name="ck_csbe_sequence_positive"),
        # The authority for sequence allocation under concurrency: two writers
        # that compute the same next sequence cannot both persist it.
        UniqueConstraint(
            "session_id", "boundary", "sequence",
            name="uq_csbe_session_boundary_sequence",
        ),
        # Latest-event lookup: leftmost prefix (session_id, boundary) with
        # sequence ordered, which the unique index above also serves; named
        # explicitly for the (user_id, boundary) access path.
        Index("ix_csbe_user_boundary", "user_id", "boundary"),
        Index("ix_csbe_session_boundary_seq", "session_id", "boundary", "sequence"),
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<ClockSessionBoundaryEvent session={self.session_id} "
            f"{self.boundary}#{self.sequence} prov={self.provenance} "
            f"actor={self.actor_label}>"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Append-only, enforced rather than documented
# ═════════════════════════════════════════════════════════════════════════════
# Follows the ``app/config_versioning/models.py`` precedent. Rejection is
# wholesale rather than per-field: unlike a ConfigVersion, a boundary event has
# no field that may legitimately change, so there is nothing to allow through.

@event.listens_for(ClockSessionBoundaryEvent, "before_update", propagate=True)
def _reject_boundary_event_update(mapper, connection, target):
    raise BoundaryEventImmutable(
        f"ClockSessionBoundaryEvent id={target.id} cannot be updated. Boundary "
        f"provenance is append-only: record the later boundary write as a NEW "
        f"event, never by rewriting an earlier one."
    )


@event.listens_for(ClockSessionBoundaryEvent, "before_delete", propagate=True)
def _reject_boundary_event_delete(mapper, connection, target):
    raise BoundaryEventImmutable(
        f"ClockSessionBoundaryEvent id={target.id} cannot be deleted. Boundary "
        f"provenance is permanent historical evidence; a session whose origin "
        f"is unrecorded must be refused, not re-attributed."
    )



# ===========================================================================
# Database-level append-only enforcement -- the authority (Owner Decision 1A)
# ===========================================================================
# Attached to the TABLE so `Base.metadata.create_all()` carries it. The
# Alembic revision emits the SAME statements, because `op.create_table`
# builds its own Table object and this listener does not fire for it.
#
# The ORM guard above stays exactly as it was. It is not redundant: it
# raises a typed, named exception the application can reason about, where
# the trigger raises a database error. The trigger is the backstop for the
# statements the ORM never sees (Ruling M / D18-F1).

@event.listens_for(ClockSessionBoundaryEvent.__table__, "after_create")
def _install_boundary_append_only_guard(target, connection, **kw):
    for statement in boundary_append_only_statements(
            connection.dialect.name):
        connection.execute(text(statement))

__all__ = [
    "ClockSessionBoundaryEvent",
    "Provenance",
    "BOUNDARY_CLOCK_IN",
    "BOUNDARY_CLOCK_OUT",
    "BOUNDARIES",
    "OPEN_BOUNDARY_PROVENANCE",
    "CLOSE_BOUNDARY_PROVENANCE",
    "ALLOWED_PROVENANCE",
]
