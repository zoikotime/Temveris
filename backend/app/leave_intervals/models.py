"""
Intraday leave intervals — SQLAlchemy models.
=============================================

STEP 7 of the owner-authorised Phase 2 foundation.

WHAT THESE TABLES ARE
---------------------
The canonical temporal boundaries of leave that has ALREADY been granted. They
record *when* within a day leave was taken. They do not record *that* leave was
granted — ``leave_requests`` remains the authority on that, and every interval
set is bound to one by a NOT NULL foreign key. A set cannot exist without a
request, cannot contradict its request's grant, and cannot be created for a
request that does not exist.

That binding is what stops this becoming a second source of leave truth. The
relationship is the same one the work ledger has to ``clock_sessions``: the
source record grants, the interval layer states the boundaries.

WHY TWO TABLES
--------------
Because withdrawal has to be representable. Intraday leave is append-only — a
correction records a NEW set version rather than editing the old one — and
"this request no longer has intraday intervals" is a set version containing
zero intervals. With only an interval table, that state has no rows and is
therefore indistinguishable from "never recorded". The set row is what makes an
empty version a fact rather than an absence.

APPEND-ONLY, WITH NO MUTATION AT ALL
------------------------------------
Unlike the work-day ledger, which stamps ``superseded_by`` on the prior row,
nothing here is ever written twice. The current version is
``MAX(set_version)`` for a request — a deterministic query — and ``supersedes``
points backwards, written once at insert. Every prior version stays exactly as
it was recorded, so the intraday record as it stood on any past date can be
reproduced without replaying an audit log.

WHAT THESE TABLES DELIBERATELY DO NOT STORE
-------------------------------------------
* Whether the leave is paid, qualifying, or deductible. Those are policy
  interpretations resolved against a versioned policy at evaluation. Freezing
  one into the evidence would break §17 historical reproducibility.
* Any jurisdiction-specific classification. That is Steps 3-4, still blocked.
* Any worked-time figure. This layer never computes what work the leave
  displaced.

TENANCY
-------
``organization_id`` is NOT NULL on both tables, deliberately stricter than
``leave_requests.organization_id``, which is nullable. An interval whose tenant
is unknown cannot be isolated or audited; the service refuses to record one
rather than inheriting a NULL.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, UniqueConstraint, event, inspect as sa_inspect,
)
from sqlalchemy.orm import relationship

from database.database import Base

from .errors import LeaveIntervalImmutable


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Aware because §16.4 prohibits naive arithmetic."""
    return datetime.now(timezone.utc)


class LeaveIntervalSet(Base):
    """One recorded version of a leave request's intraday boundaries.

    NATURAL KEY
    -----------
    ``(leave_request_id, set_version)`` — unique. Version 1 is the first
    recording; each correction appends the next.

    CURRENT VERSION
    ---------------
    ``MAX(set_version)`` for the request. No row is ever updated to mark it
    superseded, so "current" is derived rather than maintained, and cannot fall
    out of step with the data.
    """

    __tablename__ = "leave_interval_set"

    # No index=True on the primary key: a PK is already indexed on PostgreSQL
    # and SQLite alike, and the auto-generated name would be
    # ``ix_leave_interval_set_id`` — byte-identical to the one SQLAlchemy
    # derives for ``leave_interval.set_id``. Two tables cannot share an index
    # name, so the redundant index is dropped rather than worked around.
    id = Column(Integer, primary_key=True)

    #: NOT NULL, unlike the parent request's nullable column — see the module
    #: docstring. The service refuses to record for an untenanted request.
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

    #: The grant these boundaries describe. RESTRICT: intraday history must not
    #: disappear because the parent row was removed. Note that the existing
    #: DELETE /leave/{id} endpoint hard-deletes requests — with this constraint
    #: in place, a request carrying recorded intervals can no longer be silently
    #: erased. See section N of the Step 7 report.
    leave_request_id = Column(
        Integer, ForeignKey("leave_requests.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Monotonic per leave_request_id. Never reused, never decremented. Also the
    #: optimistic-concurrency token: a correction states the version it was
    #: computed against, and is rejected if that is no longer current.
    set_version = Column(Integer, nullable=False)

    #: The previous version this one corrects. Written once at insert; never
    #: updated. NULL on version 1.
    supersedes = Column(
        Integer, ForeignKey("leave_interval_set.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: The organization's zone at the moment of recording, persisted so a later
    #: tenant timezone change cannot retroactively move a recorded boundary or
    #: re-attribute its local date (§12.2).
    timezone_iana = Column(String(64), nullable=False)

    recorded_at_utc = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    recorded_by = Column(String(64), nullable=False, default="SYSTEM")

    intervals = relationship(
        "LeaveInterval",
        back_populates="interval_set",
        # save-update only: detaching an interval would delete it, and deletion
        # is refused. A set composes; it does not dismantle.
        cascade="save-update, merge",
        order_by="LeaveInterval.start_at_utc, LeaveInterval.seq",
    )

    __table_args__ = (
        UniqueConstraint(
            "leave_request_id", "set_version",
            name="uq_leave_interval_set_version",
        ),
        # The target of LeaveInterval's composite tenant foreign key.
        UniqueConstraint("id", "organization_id", name="uq_leave_interval_set_id_org"),
        # The hot read: "the current set for this request".
        Index("ix_leave_interval_set_request_version",
              "leave_request_id", "set_version"),
        Index("ix_leave_interval_set_org_user", "organization_id", "user_id"),
        CheckConstraint("set_version >= 1", name="ck_leave_interval_set_version_positive"),
        CheckConstraint("length(timezone_iana) > 0",
                        name="ck_leave_interval_set_timezone_nonempty"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<LeaveIntervalSet request={self.leave_request_id} "
            f"v={self.set_version} org={self.organization_id}>"
        )


class LeaveInterval(Base):
    """One intraday leave interval. The boundaries themselves.

    ``duration_seconds`` is stored alongside the instants so a historical figure
    is verifiable without re-deriving it from timestamps whose timezone rules may
    since have changed. It is computed by the ledger's §16.4 arithmetic, not by a
    second implementation.

    ``work_date`` is the LOCAL date the interval is attributed to, under the
    START_DATE rule already ruled on for the work-day ledger (§12.3). An
    interval crossing local midnight belongs to the date it began on — so leave
    and work never disagree about which day is under discussion.
    """

    __tablename__ = "leave_interval"

    id = Column(Integer, primary_key=True)

    #: No standalone index: ``ix_leave_interval_order`` leads with this column
    #: and serves set-scoped lookups already.
    set_id = Column(Integer, nullable=False)

    #: Denormalised for tenant-scoped queries, and load-bearing: half of the
    #: composite foreign key that makes cross-tenant attachment impossible.
    organization_id = Column(Integer, nullable=False, index=True)

    #: Deterministic ordering tiebreaker. Two intervals may legitimately share a
    #: start instant only in error, but ordering must not depend on iteration.
    seq = Column(Integer, nullable=False)

    #: Local calendar date, START_DATE attribution. A true Date: it is a date in
    #: the organization's calendar, not an instant.
    work_date = Column(Date, nullable=False, index=True)

    start_at_utc = Column(DateTime(timezone=True), nullable=False)
    end_at_utc = Column(DateTime(timezone=True), nullable=False)
    duration_seconds = Column(Integer, nullable=False)

    interval_set = relationship("LeaveIntervalSet", back_populates="intervals")

    __table_args__ = (
        # Composite tenant foreign key. RESTRICT so a set carrying intervals
        # cannot be removed from under them.
        ForeignKeyConstraint(
            ["set_id", "organization_id"],
            ["leave_interval_set.id", "leave_interval_set.organization_id"],
            name="fk_leave_interval_set_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("set_id", "seq", name="uq_leave_interval_seq"),
        Index("ix_leave_interval_order", "set_id", "start_at_utc", "seq"),
        Index("ix_leave_interval_org_date", "organization_id", "work_date"),
        # Strictly positive: unlike a work-state interval, a zero-length leave
        # interval is never meaningful. Enforced at rest as well as in the
        # validator, because an integer comparison is portable where a
        # timestamp comparison across dialects is not.
        CheckConstraint("duration_seconds > 0", name="ck_leave_interval_duration_positive"),
        CheckConstraint("seq >= 1", name="ck_leave_interval_seq_positive"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<LeaveInterval set={self.set_id} seq={self.seq} "
            f"{self.work_date} {self.duration_seconds}s>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Append-only enforcement — ORM level, matching the owner's Step 2/6 precedent
# ══════════════════════════════════════════════════════════════════════════════
# No database triggers, per the standing ruling. The documented limitation is
# unchanged and applies here too: a Core-level bulk UPDATE or DELETE that
# bypasses the ORM entirely is not intercepted.
#
# Both models refuse EVERY update, not a field subset. There is no correctable
# field on a recorded interval: a correction is a new set version, always.

def _refuse(entity: str, target, verb: str):
    raise LeaveIntervalImmutable(
        f"{entity} id={getattr(target, 'id', None)} cannot be {verb}. Intraday "
        f"leave is append-only — record a new set version instead, so the "
        f"record as it stood on any past date stays reproducible."
    )


@event.listens_for(LeaveIntervalSet, "before_update", propagate=True)
def _reject_set_update(mapper, connection, target):
    if sa_inspect(target).modified:
        _refuse("LeaveIntervalSet", target, "modified")


@event.listens_for(LeaveInterval, "before_update", propagate=True)
def _reject_interval_update(mapper, connection, target):
    if sa_inspect(target).modified:
        _refuse("LeaveInterval", target, "modified")


@event.listens_for(LeaveIntervalSet, "before_delete", propagate=True)
def _reject_set_delete(mapper, connection, target):
    _refuse("LeaveIntervalSet", target, "deleted")


@event.listens_for(LeaveInterval, "before_delete", propagate=True)
def _reject_interval_delete(mapper, connection, target):
    _refuse("LeaveInterval", target, "deleted")


__all__ = ["LeaveIntervalSet", "LeaveInterval"]
