"""
The single append operation for session-boundary provenance (§45.D-15).
=======================================================================

One narrow function, ``append_boundary_event``. There is deliberately no
update, no delete, no upsert and no "record if missing" convenience: boundary
provenance is append-only by owner ruling D15-3-A, and a helper that quietly
filled in a missing origin would be the inference §45.D-15 forbids.

WHAT THE CALLER MUST STATE
--------------------------
Everything. ``provenance`` has no default, ``actor_label`` has no default, and
neither is derived from the endpoint, the timestamp, the session state or the
caller's role. A writer that cannot say where a boundary came from is refused.

ATOMICITY
---------
This function never commits. It flushes inside a SAVEPOINT and leaves the
outer transaction to the caller, so the boundary write and its provenance event
commit together or not at all:

* boundary write commits  -> event commits;
* boundary write rolls back -> event is gone with it;
* event insertion fails    -> the exception propagates and the caller's
  ``db.commit()`` is never reached.

CONCURRENCY
-----------
``sequence`` is monotonic per ``(session_id, boundary)``. Three mechanisms:

1. **Allocation is a SINGLE statement, not a read followed by a write.**
   The sequence is computed inside the INSERT itself::

       INSERT INTO clock_session_boundary_event (..., sequence)
       SELECT ..., COALESCE(MAX(sequence), 0) + 1
         FROM clock_session_boundary_event
        WHERE session_id = :sid AND boundary = :boundary

   There is therefore no window between reading the maximum and using it, in
   which a value read in Python could go stale. This is what makes
   ``MAX(sequence) + 1`` safe here; the unsafe form is the one that reads the
   maximum into the application and inserts it in a later statement.

2. **A row lock on the parent session, on PostgreSQL.**
   ``SELECT id FROM clock_sessions WHERE id = :sid FOR UPDATE`` serialises
   concurrent boundary writes for the same session. Every production writer
   also UPDATEs that same session row in the same transaction, so it is a lock
   the writer effectively holds anyway; taking it explicitly means it is held
   *before* the allocation rather than after. Skipped on SQLite, which has no
   row-level locking and serialises writers on the database write lock anyway.

3. **The unique constraint is the final authority.**
   ``uq_csbe_session_boundary_sequence`` makes a duplicate sequence
   unrepresentable whatever happens above. Under a concurrent-snapshot
   isolation level two transactions could still compute the same value; the
   constraint rejects one of them.

Nothing here catches ``IntegrityError``. It propagates, so the caller's
``db.commit()`` is never reached and the boundary write cannot land without its
provenance — failing closed, as §45.D-15 requires.

WHY NOT A SAVEPOINT-AND-RETRY
-----------------------------
An earlier revision of this module allocated the sequence in Python and
retried inside ``Session.begin_nested()``. It was withdrawn: the pysqlite
driver does not open a driver-level transaction for a SELECT, so at the point
the SAVEPOINT was issued there was frequently no enclosing transaction — and
in SQLite, RELEASE of an outermost savepoint COMMITS. The retry mechanism was
therefore silently committing the event ahead of the caller's transaction,
breaking the one guarantee this module exists to provide. Single-statement
allocation needs no savepoint and behaves identically on both dialects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.work_ledger.states import Provenance

from .errors import (
    InvalidBoundary,
    InvalidBoundaryProvenance,
    MissingActorLabel,
    NaiveBoundaryTimestamp,
)
from .models import (
    ALLOWED_PROVENANCE,
    BOUNDARIES,
    ClockSessionBoundaryEvent,
)

#: Length of ``actor_label``; matches the column and ruling F's precedent.
ACTOR_LABEL_MAX = 64


# ═════════════════════════════════════════════════════════════════════════════
# Actor labels — the repository's established rule, NOT a new identity model
# ═════════════════════════════════════════════════════════════════════════════
# The rule is ruling E/F's: a stable identifier that stays meaningful once the
# row it came from is gone — an id where one exists, ``SYSTEM`` otherwise.
# Precedents: ``ConfigVersion.captured_by_label``,
# ``RolloutControl.last_transition_by`` and
# ``eco_threshold_capture.ActorContext.for_user``.
#
# ``ActorContext`` is NOT imported here, deliberately. It lives in
# ``app.eco_threshold_capture``, and that package is held to a not-wired scope
# guard (``tests/test_eco_threshold_capture.py``) which importing it from this
# package — which IS wired into the production attendance path — would break.
# So the rule is applied here and ``tests/test_clock_session_boundary.py``
# asserts, user by user, that this function and ``ActorContext.for_user`` agree,
# and that the two ``SYSTEM`` constants are identical. The equivalence is
# proven in the test suite rather than created by a production import that
# would violate an earlier owner-ruled boundary.

#: The explicit label for an unattended write. Pinned equal to
#: ``eco_threshold_capture.outcomes.SYSTEM_ACTOR_LABEL`` by test.
SYSTEM_ACTOR_LABEL = "SYSTEM"


def actor_label_for_user(user) -> str:
    """Durable label for an authenticated user record.

    Prefers a stable identifier over a display name, and holds an identifying
    label only — no name, no role history.
    """
    email = (getattr(user, "email", None) or "").strip()
    if email:
        return email[:ACTOR_LABEL_MAX]
    user_id = getattr(user, "id", None)
    if user_id is not None:
        return f"user:{user_id}"[:ACTOR_LABEL_MAX]
    raise MissingActorLabel(
        f"cannot derive an actor label for {user!r}: no email and no id. A "
        f"write with no actor at all is refused, never labelled SYSTEM on the "
        f"caller's behalf — SYSTEM means 'no person did this', which would be "
        f"a false statement about an authenticated request."
    )


def actor_label_for_user_id(user_id: Optional[int]) -> str:
    """Durable label when only an id is in hand, not a user row.

    Returns the explicit ``SYSTEM`` label when there is genuinely no acting
    person — an unattended sweep — which is a statement, not a guess.
    """
    if user_id is None:
        return SYSTEM_ACTOR_LABEL
    return f"user:{user_id}"[:ACTOR_LABEL_MAX]


# ═════════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════════

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise NaiveBoundaryTimestamp(f"{field} must be a datetime, got {value!r}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveBoundaryTimestamp(
            f"{field} must be timezone-aware; a naive instant would be read "
            f"back as UTC and could move the day this session is attributed to"
        )
    return value


def _normalize_provenance(provenance: Union[Provenance, str]) -> Provenance:
    """Accept the enum member or its exact value; reject anything else.

    No case-folding, no aliasing, no nearest match. An unrecognised origin is a
    refusal, because guessing which of five origins was meant is precisely the
    inference §45.D-15 prohibits.
    """
    if isinstance(provenance, Provenance):
        return provenance
    if isinstance(provenance, str):
        try:
            return Provenance(provenance)
        except ValueError:
            pass
    raise InvalidBoundaryProvenance(
        f"{provenance!r} is not an approved provenance value"
    )


def _require_actor_label(actor_label: Optional[str]) -> str:
    if not isinstance(actor_label, str) or not actor_label.strip():
        raise MissingActorLabel(
            "an actor label is required; pass SYSTEM_ACTOR_LABEL explicitly for "
            "an unattended closure — a blank label is an actorless write"
        )
    label = actor_label.strip()
    if len(label) > ACTOR_LABEL_MAX:
        raise MissingActorLabel(
            f"actor label exceeds {ACTOR_LABEL_MAX} characters: {label!r}"
        )
    return label


# ═════════════════════════════════════════════════════════════════════════════
# Sequence allocation
# ═════════════════════════════════════════════════════════════════════════════

def _lock_parent_session(db: Session, session_id: int) -> None:
    """Serialise same-session boundary writes where the dialect can (see 2)."""
    try:
        dialect = db.get_bind().dialect.name
    except Exception:
        return
    if dialect != "postgresql":
        return
    db.execute(
        text("SELECT id FROM clock_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )


def _next_sequence_expression(session_id: int, boundary: str):
    """``COALESCE(MAX(sequence), 0) + 1`` as a scalar subquery.

    Evaluated by the database as part of the INSERT, so the value cannot go
    stale between being computed and being stored.
    """
    table = ClockSessionBoundaryEvent.__table__
    return (
        select(func.coalesce(func.max(table.c.sequence), 0) + 1)
        .where(
            table.c.session_id == session_id,
            table.c.boundary == boundary,
        )
        .scalar_subquery()
    )


# ═════════════════════════════════════════════════════════════════════════════
# The append operation
# ═════════════════════════════════════════════════════════════════════════════

def append_boundary_event(
    db: Session,
    *,
    session_id: int,
    user_id: int,
    boundary: str,
    boundary_at: datetime,
    provenance: Union[Provenance, str],
    actor_label: str,
    written_at: Optional[datetime] = None,
) -> ClockSessionBoundaryEvent:
    """Record one boundary write, immutably, in the caller's transaction.

    ``session_id`` must already exist — flush a newly created session first, so
    the event references a real row. ``user_id`` is the SUBJECT Worker, not the
    actor. Does not commit; see the module docstring on atomicity.
    """
    if boundary not in BOUNDARIES:
        raise InvalidBoundary(
            f"{boundary!r} is not a boundary; expected one of "
            f"{sorted(BOUNDARIES)}"
        )

    prov = _normalize_provenance(provenance)
    if prov not in ALLOWED_PROVENANCE[boundary]:
        allowed = sorted(p.value for p in ALLOWED_PROVENANCE[boundary])
        raise InvalidBoundaryProvenance(
            f"{prov.value} is not admissible on the {boundary} boundary; "
            f"allowed: {allowed}. A clock-in is not a closure, so a closure "
            f"origin there would attribute an event that has not happened."
        )

    label = _require_actor_label(actor_label)
    _require_aware(boundary_at, "boundary_at")
    written = _require_aware(written_at or _utcnow(), "written_at")

    if session_id is None:
        raise InvalidBoundary("session_id is required; flush a new session first")
    if user_id is None:
        raise InvalidBoundary("user_id (the subject Worker) is required")

    _lock_parent_session(db, session_id)

    table = ClockSessionBoundaryEvent.__table__
    # Core INSERT rather than `db.add()`, because the sequence has to be a SQL
    # expression evaluated by the database. Any of the caller's pending ORM
    # work is flushed first, so the boundary write and this insert are ordered
    # inside the one transaction the caller will commit.
    db.flush()
    result = db.execute(
        table.insert().values(
            session_id=session_id,
            user_id=user_id,
            boundary=boundary,
            boundary_at=boundary_at,
            provenance=prov.value,
            actor_label=label,
            written_at=written,
            sequence=_next_sequence_expression(session_id, boundary),
        )
    )
    # No `except IntegrityError`: a violation must reach the caller so its
    # transaction cannot commit a boundary write with no recorded origin.
    return db.get(ClockSessionBoundaryEvent, result.inserted_primary_key[0])


__all__ = [
    "append_boundary_event",
    "actor_label_for_user",
    "actor_label_for_user_id",
    "SYSTEM_ACTOR_LABEL",
    "ACTOR_LABEL_MAX",
]
