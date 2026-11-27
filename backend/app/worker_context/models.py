"""
Worker-context history — SQLAlchemy model.
==========================================

WORKER CONTEXT HISTORY foundation, approved by owner ruling.

ONE FACT, TWO ACCESS PATHS, ONE WRITER
--------------------------------------
    "What is this Worker's classification NOW?"    -> User.employment_type
    "What was it on 14 March?"                     -> worker_context_as_of(...)

``User.employment_type`` remains the current-state projection and is unchanged;
all eight of its existing consumers keep reading it. This table is the temporal
authority. They are not competing sources: one service writes both in one
transaction, and ``worker_context_as_of(today)`` must equal the projection.

WHY THE WHOLE TIMELINE IS VERSIONED
-----------------------------------
A genuine correction is "we recorded the change as the 3rd; it was actually the
5th." With one row per period and a unique effective date, that is unfixable
without mutation — and mutation is exactly what destroyed the history this table
exists to restore. So a correction appends a COMPLETE new timeline: every period
re-recorded under ``timeline_version + 1``, with the prior version left
byte-identical. Retroactive correction becomes possible with no row ever
updated.

Resolution is therefore two steps: the latest ``timeline_version``, then within
it the latest ``effective_from <= as_of``.

WHY ONE TABLE AND NOT TWO
-------------------------
Step 7 needed a parent row because an EMPTY interval set is a meaningful state
(leave intervals withdrawn). Worker context has no equivalent — a captured
Worker always has some classification, and there is no "withdraw the
classification" operation. A parent row would carry no information.

WHAT THIS TABLE DELIBERATELY DOES NOT STORE
-------------------------------------------
* Any legal worker category. ``context_vocabulary`` keeps vocabularies separate;
  D11 is open and nothing here maps product employment type onto a legal one.
* Any requirement, any minutes, any worked time. This records WHAT the Worker
  was, never what they owed.
* Any reconstruction of destroyed history. H-1: unrecoverable stays UNKNOWN.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer,
    String, UniqueConstraint, event, inspect as sa_inspect,
)

from database.database import Base

from .errors import WorkerContextImmutable


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Aware because §16.4 prohibits naive arithmetic."""
    return datetime.now(timezone.utc)


class WorkerContextTimeline(Base):
    """One period within one immutable version of a Worker's context timeline."""

    __tablename__ = "worker_context_timeline"

    # No index=True on the primary key: it is already indexed on both backends,
    # and the auto-generated name is a collision risk (the Step 7 lesson, where
    # leave_interval_set.id and leave_interval.set_id produced the same name).
    id = Column(Integer, primary_key=True)

    #: NOT NULL, deliberately stricter than the nullable User.organization_id.
    #: The service refuses to record for an untenanted Worker rather than
    #: inheriting a NULL that cannot be isolated or audited.
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    #: RESTRICT, not CASCADE (Owner Ruling M). Ruling M forbids deleting or
    #: overwriting historical records, and a cascade from ``users`` would do
    #: exactly that — below the ORM listeners, which a Core bulk delete never
    #: fires. The database now refuses it instead of relying on application
    #: discipline, matching the standing precedent that "removing a person
    #: must never remove configuration history"
    #: (``config_version.captured_by_user_id``).
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Monotonic per Worker. Also the optimistic-concurrency token: a correction
    #: states the version it was computed against and is rejected if stale.
    timeline_version = Column(Integer, nullable=False)

    #: Inclusive lower bound of this period. A DATE, not an instant: a
    #: classification takes effect on a day in the organization's calendar.
    effective_from = Column(Date, nullable=False)

    #: The classification value, carried VERBATIM. Not normalised, not mapped.
    worker_context = Column(String(64), nullable=False)

    #: Which vocabulary the value belongs to. Keeps a product employment type
    #: and any future legal worker category from ever being read as each other.
    context_vocabulary = Column(String(64), nullable=False)

    #: CAPTURED | CORRECTION | CREATION_DEFAULT. A defaulted value is never
    #: recorded as CAPTURED — see vocabulary.py.
    provenance = Column(String(32), nullable=False)

    #: When the capture happened — audit only, never used to select a period.
    #: Distinct from effective_from, so a change recorded late for an earlier
    #: date is representable without either field lying.
    recorded_at_utc = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    #: NULL for a system capture. SET NULL on user deletion: removing a person
    #: must never remove classification history.
    recorded_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    #: The timeline version this one corrects. A plain integer, not a foreign
    #: key: a version spans several rows, so a reference to one row would be
    #: arbitrary. Written once at insert; never updated. NULL on version 1.
    supersedes_version = Column(Integer, nullable=True)

    __table_args__ = (
        # One value per period per timeline version.
        UniqueConstraint(
            "user_id", "timeline_version", "effective_from",
            name="uq_worker_context_timeline_period",
        ),
        # The hot read: current version for a Worker, then latest applicable
        # period within it.
        Index(
            "ix_worker_context_timeline_lookup",
            "organization_id", "user_id", "timeline_version", "effective_from",
        ),
        CheckConstraint("timeline_version >= 1",
                        name="ck_worker_context_timeline_version_positive"),
        CheckConstraint("length(worker_context) > 0",
                        name="ck_worker_context_timeline_context_nonempty"),
        CheckConstraint("length(context_vocabulary) > 0",
                        name="ck_worker_context_timeline_vocabulary_nonempty"),
        CheckConstraint("length(provenance) > 0",
                        name="ck_worker_context_timeline_provenance_nonempty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<WorkerContextTimeline user={self.user_id} v={self.timeline_version} "
            f"from={self.effective_from} {self.worker_context!r}>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Append-only enforcement — ORM level, per the owner's standing ruling
# ══════════════════════════════════════════════════════════════════════════════
# No database trigger, and no listener pretending to enforce transactional
# coupling (the owner ruled option 1: a single service function plus an AST
# guard). These two listeners enforce only what a listener honestly can — that a
# recorded period is never rewritten or removed.
#
# The documented limitation is unchanged and applies here as in Steps 2, 6 and
# 7: a Core-level bulk UPDATE or DELETE that bypasses the ORM is not intercepted.

def _refuse(target, verb: str):
    raise WorkerContextImmutable(
        f"WorkerContextTimeline id={getattr(target, 'id', None)} cannot be "
        f"{verb}. Worker context is append-only — record a new timeline version "
        f"instead, so the classification as it stood on any past date stays "
        f"reproducible."
    )


@event.listens_for(WorkerContextTimeline, "before_update", propagate=True)
def _reject_update(mapper, connection, target):
    if sa_inspect(target).modified:
        _refuse(target, "modified")


@event.listens_for(WorkerContextTimeline, "before_delete", propagate=True)
def _reject_delete(mapper, connection, target):
    _refuse(target, "deleted")


__all__ = ["WorkerContextTimeline"]
