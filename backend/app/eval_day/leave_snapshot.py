"""
The immutable leave input snapshot — Owner Ruling 2.
====================================================

What leave an ECO day evaluation actually used, frozen at evaluation time, so a
historical figure can be re-derived **without re-reading mutable leave state**.

WHY THIS EXISTS
---------------
MODEL-EXCLUSION made approved-leave overlap an authoritative input to a day
evaluation, but two of its components carry no version of their own:

* a day's leave can come from SEVERAL leave requests, so the input is a
  collection, not a scalar;
* ``LeaveRequest.status`` is mutated in place — ``approved -> rejected`` changes
  whether leave contributes, and increments nothing.

So re-reading leave later cannot reproduce an older evaluation. R2's claim that
*"every authoritative input is in the key"* had stopped being true. This
snapshot restores it: the resolved input is frozen, content-addressed, and
referenced by the day record's identity.

CONTENT-ADDRESSED (ruling A)
----------------------------
``snapshot_key == content_hash``. Nothing allocates a key, so it is
deterministic, survives a database rebuild by construction — the property
``ConfigVersion.version_key`` exists to provide — and an exact replay finds the
same row instead of writing a second one.

SOURCE EVIDENCE **AND** THE RESOLVED RESULT (ruling Q)
------------------------------------------------------
``overlap_seconds`` and each contribution's ``contributed_seconds`` are stored,
so the historical arithmetic is independently checkable. A digest alone would
prove *that* the inputs differed while leaving the number unverifiable.

Interval CONTENTS are not copied — a fingerprint is stored instead. Intervals
live in an append-only table under an immutable set version, so they cannot
change; the fingerprint proves which they were. And a contributing request
always owns intervals, so the `RESTRICT` foreign key on ``leave_interval_set``
already prevents its deletion.

NOT EFFECTIVE-DATED (ruling R)
------------------------------
``ConfigVersion`` answers "what governed date X" across a shared timeline. This
answers "what leave did THIS evaluation of THIS Worker-day use". There is no
timeline, so there is no ``effective_from`` — inventing one would create a
second resolution rule competing with the ruled one.

NO FOREIGN KEY TO ``leave_requests`` (ruling P)
-----------------------------------------------
``leave_request_id`` is a historical value. An FK would either block a deletion
the existing leave rules permit — changing leave semantics, which is out of
scope — or ``SET NULL`` and destroy the provenance at exactly the moment it
matters. Same reasoning as ``captured_by_label`` beside a ``SET NULL`` FK in
Step 6. Tenancy is still enforced, by a composite foreign key to the snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional, Sequence, Tuple

from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, UniqueConstraint, event, select, text,
    inspect as sa_inspect,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, relationship

from database.database import Base

from .errors import EvalDayError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """Normalise to a true UTC instant.

    Mirrors the leave service's own normalisation deliberately: SQLite discards
    ``tzinfo`` on write, so an instant read back is naive even though it was
    stored aware. A canonical hash that treated those as different values would
    make the same leave hash two ways.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


from .append_only_ddl import snapshot_append_only_statements

class LeaveSnapshotImmutable(EvalDayError):
    """A persisted leave input snapshot would be altered or removed.

    The snapshot is the frozen input of an evaluation that already happened.
    Rewriting it would change what a historical day is said to have used.
    """


# ══════════════════════════════════════════════════════════════════════════════
# Canonicalisation and hashing — pure (ruling N)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class LeaveContribution:
    """One leave request/set-version considered by a day's leave resolution.

    ``contributed_seconds`` is 0 for a request whose status is not ECO-approved
    (ruling C). Recording it is what keeps "leave existed but did not qualify"
    distinguishable from "no leave existed".
    """

    leave_request_id: int
    set_version: int
    status: str
    interval_fingerprint: str
    interval_count: int
    contributed_seconds: int

    def __post_init__(self) -> None:
        if self.set_version < 1:
            raise ValueError(
                f"set_version must be >= 1, got {self.set_version!r}")
        if self.interval_count < 0 or self.contributed_seconds < 0:
            raise ValueError("counts and seconds are never negative")
        if not (self.status or "").strip():
            raise ValueError("status_at_evaluation is required")


def leave_interval_fingerprint(intervals: Iterable) -> str:
    """A deterministic digest of a set of leave intervals.

    Ordered by the boundaries themselves, normalised to UTC, so the order rows
    were returned in cannot change the digest — the same order-independence the
    leave service's own replay fingerprint relies on (§22.1). Duration is
    included so a boundary pair cannot be silently re-timed.
    """
    rows = sorted(
        (
            _utc(i.start_at_utc).isoformat(),
            _utc(i.end_at_utc).isoformat(),
            int(i.duration_seconds),
        )
        for i in intervals
    )
    encoded = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_leave_snapshot_hash(
    *,
    organization_id: int,
    user_id: int,
    work_date: date,
    overlap_seconds: int,
    contributions: Sequence[LeaveContribution],
) -> str:
    """SHA-256 over the canonical serialisation of a day's leave input.

    Deterministic across processes and platforms:

    * contributions sorted by ``leave_request_id``, which is total because ids
      are unique — so the order they were resolved in cannot change the digest;
    * JSON with sorted keys, no insignificant whitespace and ``ensure_ascii``,
      so the byte sequence does not depend on the platform's encoding;
    * integer seconds only. No floats, no decimal hours;
    * ``captured_at_utc`` and every surrogate id are EXCLUDED — they are
      provenance about the capture, not the input, and including them would
      make identical leave hash differently each time it was recorded.

    An empty input hashes deterministically too, which is what lets one empty
    snapshot per Worker-day be reused forever (ruling E).
    """
    payload = {
        "organization_id": int(organization_id),
        "user_id": int(user_id),
        "work_date": work_date.isoformat(),
        "overlap_seconds": int(overlap_seconds),
        "contributions": [
            {
                "leave_request_id": int(c.leave_request_id),
                "set_version": int(c.set_version),
                "status": c.status,
                "interval_fingerprint": c.interval_fingerprint,
                "interval_count": int(c.interval_count),
                "contributed_seconds": int(c.contributed_seconds),
            }
            for c in sorted(contributions, key=lambda c: int(c.leave_request_id))
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════════════════════
class LeaveInputSnapshot(Base):
    """The frozen leave input of one ECO day evaluation.

    Scope is ``(organization_id, user_id, work_date)`` plus contents. Identity
    is the content hash, so two evaluations that resolved identical leave share
    one row and an exact replay writes nothing.
    """

    __tablename__ = "leave_input_snapshot"

    id = Column(Integer, primary_key=True)

    #: The public identity. Equals ``content_hash`` — derived, never allocated,
    #: so it survives a database rebuild without a generator or registry.
    snapshot_key = Column(String(64), nullable=False)

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

    #: The local work date this input was resolved for, under the same
    #: START_DATE attribution the ledger and the leave intervals use.
    work_date = Column(Date, nullable=False)

    content_hash = Column(String(64), nullable=False)

    #: The resolved total. Stored so the historical exclusion is checkable
    #: without re-deriving it from intervals (ruling Q).
    overlap_seconds = Column(Integer, nullable=False)

    #: How many requests the day's leave resolution considered — including any
    #: that contributed nothing. Provenance; excluded from the hash.
    contribution_count = Column(Integer, nullable=False)

    #: When the input was frozen. Provenance, never identity.
    captured_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)

    contributions = relationship(
        "LeaveInputContribution",
        back_populates="snapshot",
        # save-update only: detaching a contribution would delete it, and
        # deletion is refused. A snapshot composes; it does not dismantle.
        cascade="save-update, merge",
        order_by="LeaveInputContribution.leave_request_id",
    )

    __table_args__ = (
        # Content addressing, tenant-scoped: one row per distinct input per
        # organization, which is what makes get-or-create idempotent.
        UniqueConstraint("organization_id", "snapshot_key",
                         name="uq_leave_input_snapshot_key"),
        # The target of the contribution's composite tenant FK. Redundant as a
        # key and declared solely so a contribution cannot be attached to
        # another organization's snapshot.
        UniqueConstraint("id", "organization_id",
                         name="uq_leave_input_snapshot_id_org"),
        Index("ix_leave_input_snapshot_day",
              "organization_id", "user_id", "work_date"),
        CheckConstraint("length(snapshot_key) > 0",
                        name="ck_leave_input_snapshot_key_nonempty"),
        CheckConstraint("length(content_hash) > 0",
                        name="ck_leave_input_snapshot_hash_nonempty"),
        # Ruling A at the schema: the public key IS the content hash. A
        # column-to-column comparison, so the database enforces the identity
        # relationship without knowing anything about how the hash is computed
        # — nothing here re-implements or depends on application hashing.
        CheckConstraint("snapshot_key = content_hash",
                        name="ck_leave_input_snapshot_key_is_hash"),
        CheckConstraint("overlap_seconds >= 0",
                        name="ck_leave_input_snapshot_seconds_nonnegative"),
        CheckConstraint("contribution_count >= 0",
                        name="ck_leave_input_snapshot_count_nonnegative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<LeaveInputSnapshot org={self.organization_id} "
            f"user={self.user_id} {self.work_date} "
            f"seconds={self.overlap_seconds}>"
        )


class LeaveInputContribution(Base):
    """One leave request/set-version the day's resolution considered.

    One row per REQUEST, never per interval: status and set version are
    per-request facts, and the intervals themselves are already immutable
    evidence identified by ``interval_fingerprint``.
    """

    __tablename__ = "leave_input_contribution"

    id = Column(Integer, primary_key=True)

    snapshot_id = Column(Integer, nullable=False)

    #: Denormalised for tenant-scoped queries, and load-bearing: half of the
    #: composite foreign key that makes cross-tenant attachment impossible.
    organization_id = Column(Integer, nullable=False, index=True)

    #: The grant this contribution came from — a HISTORICAL VALUE, deliberately
    #: NOT a foreign key (ruling P), so ECO evidence survives a deletion the
    #: existing leave rules permit.
    leave_request_id = Column(Integer, nullable=False)

    #: The immutable leave-set version selected at evaluation time. Recording
    #: it is what lets a corrected set be distinguished from the one used.
    set_version = Column(Integer, nullable=False)

    #: The raw ``LeaveRequest.status`` as it stood. NOT normalised to a boolean
    #: (ruling D): ``approved`` and ``locked_for_payroll`` are equivalent for
    #: ECO classification but remain distinct historical facts, and normalising
    #: would destroy that irreversibly.
    status_at_evaluation = Column(String(50), nullable=False)

    interval_fingerprint = Column(String(64), nullable=False)
    interval_count = Column(Integer, nullable=False)

    #: 0 when the status was not ECO-approved. See ``LeaveContribution``.
    contributed_seconds = Column(Integer, nullable=False)

    snapshot = relationship("LeaveInputSnapshot", back_populates="contributions")

    __table_args__ = (
        # Composite tenant FK. RESTRICT: a snapshot carrying contributions
        # cannot be deleted out from under them.
        ForeignKeyConstraint(
            ["snapshot_id", "organization_id"],
            ["leave_input_snapshot.id", "leave_input_snapshot.organization_id"],
            name="fk_leave_input_contribution_snapshot_org",
            ondelete="RESTRICT",
        ),
        # One contribution per request per snapshot: a request cannot be counted
        # twice in one evaluation.
        UniqueConstraint("snapshot_id", "leave_request_id",
                         name="uq_leave_input_contribution_request"),
        Index("ix_leave_input_contribution_snapshot", "snapshot_id"),
        CheckConstraint("set_version >= 1",
                        name="ck_leave_input_contribution_version_positive"),
        CheckConstraint("length(status_at_evaluation) > 0",
                        name="ck_leave_input_contribution_status_nonempty"),
        CheckConstraint("length(interval_fingerprint) > 0",
                        name="ck_leave_input_contribution_fingerprint_nonempty"),
        CheckConstraint(
            "interval_count >= 0 AND contributed_seconds >= 0",
            name="ck_leave_input_contribution_nonnegative",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<LeaveInputContribution request={self.leave_request_id} "
            f"v={self.set_version} {self.status_at_evaluation} "
            f"seconds={self.contributed_seconds}>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Immutability (ruling O) — ORM level, matching the ConfigVersion precedent
# ══════════════════════════════════════════════════════════════════════════════
# The same documented limitation applies as everywhere else in this programme:
# a Core-level bulk UPDATE or DELETE that bypasses the ORM is not intercepted.
def _refuse(target, verb: str):
    raise LeaveSnapshotImmutable(
        f"{type(target).__name__} id={getattr(target, 'id', None)} cannot be "
        f"{verb}. A leave input snapshot is the frozen input of an evaluation "
        f"that already happened; record a NEW snapshot instead."
    )


@event.listens_for(LeaveInputSnapshot, "before_update", propagate=True)
def _reject_snapshot_update(mapper, connection, target):
    if sa_inspect(target).modified:
        _refuse(target, "modified")


@event.listens_for(LeaveInputSnapshot, "before_delete", propagate=True)
def _reject_snapshot_delete(mapper, connection, target):
    _refuse(target, "deleted")


@event.listens_for(LeaveInputContribution, "before_update", propagate=True)
def _reject_contribution_update(mapper, connection, target):
    if sa_inspect(target).modified:
        _refuse(target, "modified")


@event.listens_for(LeaveInputContribution, "before_delete", propagate=True)
def _reject_contribution_delete(mapper, connection, target):
    _refuse(target, "deleted")


# ══════════════════════════════════════════════════════════════════════════════
# The writer — content-addressed get-or-create (ruling A, step 4)
# ══════════════════════════════════════════════════════════════════════════════
def get_or_create_leave_snapshot(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date,
    overlap_seconds: int,
    contributions: Sequence[LeaveContribution] = (),
) -> LeaveInputSnapshot:
    """The snapshot for this resolved leave input, creating it only if new.

    Content-addressed, so identical input reuses the existing row: an exact
    replay writes nothing, and no duplicate snapshot accumulates. Different
    input produces a different key and therefore a different row.

    CONCURRENT IDENTICAL CREATION
    -----------------------------
    Two writers resolving the same leave both miss on the read and both insert.
    ``uq_leave_input_snapshot_key`` rejects one, and because the snapshot is
    content-addressed the loser wants precisely what the winner just wrote — so
    it re-reads and returns that row instead of surfacing a duplicate-key
    error. The parent and its children are inserted inside one SAVEPOINT, so a
    lost race leaves nothing half-written and the caller's transaction intact.

    Nothing is ever updated or merged here: the row the loser returns is the
    winner's, byte-identical by construction, which is what content addressing
    buys. Immutability is therefore untouched.

    The caller owns the transaction: this flushes and never commits.
    """
    ordered = sorted(contributions, key=lambda c: int(c.leave_request_id))
    digest = compute_leave_snapshot_hash(
        organization_id=organization_id,
        user_id=user_id,
        work_date=work_date,
        overlap_seconds=overlap_seconds,
        contributions=ordered,
    )

    def _read() -> Optional[LeaveInputSnapshot]:
        return db.execute(
            select(LeaveInputSnapshot).where(
                LeaveInputSnapshot.organization_id == organization_id,
                LeaveInputSnapshot.snapshot_key == digest,
            )
        ).scalars().first()

    existing = _read()
    if existing is not None:
        return existing

    snapshot = LeaveInputSnapshot(
        snapshot_key=digest,
        organization_id=organization_id,
        user_id=user_id,
        work_date=work_date,
        content_hash=digest,
        overlap_seconds=int(overlap_seconds),
        contribution_count=len(ordered),
    )
    children = []
    try:
        with db.begin_nested():
            db.add(snapshot)
            db.flush()

            for contribution in ordered:
                child = LeaveInputContribution(
                    snapshot_id=snapshot.id,
                    organization_id=organization_id,
                    leave_request_id=int(contribution.leave_request_id),
                    set_version=int(contribution.set_version),
                    status_at_evaluation=contribution.status,
                    interval_fingerprint=contribution.interval_fingerprint,
                    interval_count=int(contribution.interval_count),
                    contributed_seconds=int(contribution.contributed_seconds),
                )
                children.append(child)
                db.add(child)
            db.flush()
    except IntegrityError:
        # Discard the rejected rows, or the next flush retries them.
        for pending in (snapshot, *children):
            if pending in db:
                db.expunge(pending)

        raced = _read()
        if raced is None:
            # Not the duplicate-content race, so a different constraint
            # failed and the caller must see the original error.
            raise
        return raced

    return snapshot


def read_leave_snapshot(
    db: Session, *, organization_id: int, snapshot_key: str
) -> Optional[LeaveInputSnapshot]:
    """One historical snapshot, exactly as it was frozen."""
    return db.execute(
        select(LeaveInputSnapshot).where(
            LeaveInputSnapshot.organization_id == organization_id,
            LeaveInputSnapshot.snapshot_key == snapshot_key,
        )
    ).scalars().first()


def read_contributions(
    db: Session, *, snapshot: LeaveInputSnapshot
) -> Tuple[LeaveInputContribution, ...]:
    """A snapshot's contributions, deterministically ordered."""
    return tuple(db.execute(
        select(LeaveInputContribution)
        .where(
            LeaveInputContribution.snapshot_id == snapshot.id,
            LeaveInputContribution.organization_id == snapshot.organization_id,
        )
        .order_by(LeaveInputContribution.leave_request_id)
    ).scalars().all())



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

@event.listens_for(LeaveInputSnapshot.__table__, "after_create")
def _install_snapshot_append_only_guard(target, connection, **kw):
    """Emit the dialect's append-only statements for the snapshot."""
    for statement in snapshot_append_only_statements(
            connection.dialect.name):
        connection.execute(text(statement))

__all__ = [
    "LeaveContribution",
    "LeaveInputSnapshot",
    "LeaveInputContribution",
    "LeaveSnapshotImmutable",
    "compute_leave_snapshot_hash",
    "leave_interval_fingerprint",
    "get_or_create_leave_snapshot",
    "read_leave_snapshot",
    "read_contributions",
]
