"""
Canonical sealed work-state interval ledger — SQLAlchemy models.
================================================================

STEP 1 of the owner-authorised Phase 2 foundation (D-03 Option A).

These tables are ADDITIVE. They coexist with `clock_sessions`, `break_sessions`
and `offline_intervals` and change none of them. Nothing reads or writes them
yet: wiring the clock-out path into the ledger is Step 9/10, not Step 1, and
existing clock-out behaviour is preserved exactly until then.

Registered on the shared ``database.database.Base`` metadata, following the
``app/commercial/models.py`` precedent — a domain-local model module rather
than an edit to the 2,156-line shared `database/models.py`.

WHAT THIS LEDGER IS
-------------------
The intended canonical attendance truth. Per owner ruling D-03 Option A,
``app/_worked.py::session_worked_seconds`` will be re-expressed as a
deterministic fold over these intervals so that exactly one answer to
"how much did this Worker work?" exists. It is not a second source of truth;
it is THE source, and the fold is its only reader.

WHAT IT DELIBERATELY DOES NOT STORE
-----------------------------------
Qualification and paid/unpaid treatment. Those are policy interpretations
resolved at evaluation against the versioned break-treatment profile. Storing
them here would freeze one policy version into the evidence and break §17
historical reproducibility. The ledger records observed state; policy decides
what that state is worth.

VOCABULARY
----------
Identifiers follow owner ruling D-09 Option C: ``organization_id`` and the
existing user model are canonical for technical identifiers, columns, API
fields and event names. ``Tenant`` / ``Worker`` remain Worker-facing copy.

TENANCY
-------
``organization_id`` on both tables. Every query must filter on it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Integer, String, DateTime, Time, ForeignKey, Text,
    Index, UniqueConstraint, CheckConstraint, event, select, text,
    inspect as sa_inspect,
)
from sqlalchemy.orm import relationship

from database.database import Base
from .append_only_ddl import append_only_statements
from .attribution import (
    SHIFT_ATTRIBUTION_START_DATE as _ATTRIBUTION_START_DATE,
)
from .states import (
    SYNTHESIZED_CLOSURE_PROVENANCE,
    VERIFIED_DEPARTURE_PROVENANCE,
    LedgerState,
    ProvenanceImmutable,
)


def _utcnow() -> datetime:
    """Timezone-aware UTC now. Aware because every instant column here is
    ``timezone=True`` and §16.4 prohibits naive duration arithmetic."""
    return datetime.now(timezone.utc)


#: Departure provenance a sealed row may carry, rendered for the D18-F2 CHECK.
#: Generated from the enums so the database constraint and the vocabulary
#: cannot drift apart.
#:
#: D18-F2 originally admitted VERIFIED values only — "only a VERIFIED departure
#: may be recorded here". The owner narrowly superseded that for §24.7: an
#: Unverified Exit is an exit that EXISTS and was not performed by the Worker
#: or an administrator, and the ledger must be able to record it as evidence in
#: order to defer the day (§19 step 6). Refusing to store it forced the choice
#: between discarding the day and calling it a missing clock-out, and §19.1
#: forbids the second.
#:
#: ADMISSIBLE IS NOT VERIFIED. The two sets stay separate in code and every
#: verified-departure test still reads ``VERIFIED_DEPARTURE_PROVENANCE``; only
#: the storage constraint widened. A synthesized close still cannot satisfy a
#: verified-departure requirement anywhere.
ADMISSIBLE_DEPARTURE_PROVENANCE = (
    VERIFIED_DEPARTURE_PROVENANCE | SYNTHESIZED_CLOSURE_PROVENANCE
)

_ADMISSIBLE_DEPARTURE_SQL = ", ".join(
    f"'{p.value}'"
    for p in sorted(ADMISSIBLE_DEPARTURE_PROVENANCE, key=lambda x: x.value)
)



class WorkDayLedger(Base):
    """One sealable Worker-day. The unit of sealing, versioning and correction.

    NATURAL KEY
    -----------
    ``(organization_id, user_id, work_date, ledger_version)`` — unique. Re-running
    a seal with the same version is a no-op rather than a second row (§22.1).

    CURRENT ROW
    -----------
    ``superseded_by IS NULL``. Corrections never mutate: a correction writes a
    new row at ``ledger_version + 1`` and points the prior row's
    ``superseded_by`` at it. Every prior version stays permanently readable
    (INV-3, §23).

    CONCURRENCY
    -----------
    ``ledger_version`` is the optimistic-concurrency token. A write against a
    stale version is rejected and the evaluation re-run — never merged (§22.2).
    """

    __tablename__ = "work_day_ledger"

    id = Column(Integer, primary_key=True, index=True)

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

    #: The local work date the day is attributed to, after the tenant shift
    #: attribution rule. Owner ruling: attribution = START_DATE.
    work_date = Column(DateTime(timezone=False), nullable=False, index=True)

    #: The rule under which ``work_date`` was chosen, persisted because R-12.3.4
    #: and §35 require it on the day record: "Changing it MUST NOT retroactively
    #: re-attribute sealed weeks". A row that does not carry its own rule cannot
    #: answer which one produced it, so replay would have to assume today's.
    #:
    #: Always ``START_DATE`` today — the spec's default and the only supported
    #: rule (see ``attribution.py``). Stamped in ``_write_version``, never taken
    #: from a caller, so no writer can disagree with another.
    shift_attribution_rule = Column(
        String(32), nullable=False,
        server_default=_ATTRIBUTION_START_DATE, index=True,
    )

    #: The local workweek START DATE, ISO formatted — "2026-08-31" for a
    #: Monday-start organization, "2026-08-30" for a Sunday-start one in the
    #: same physical week (W4-3). Derived once from the tenant workweek start
    #: (`schedule.week_start_day`) and permanent thereafter.
    #:
    #: NOT ISO week notation: "2026-W36" is defined only for a Monday boundary,
    #: and ``eval_week.membership.canonical_dates`` actively REFUSES that form.
    #: The start date also carries its own boundary provenance — its weekday IS
    #: the regime that minted it — which an ISO week number cannot express.
    workweek_key = Column(String(16), nullable=False, index=True)

    #: Monotonic per (organization_id, user_id, work_date). Increments on every
    #: correction. Never reused, never decremented.
    ledger_version = Column(Integer, nullable=False, default=1)

    #: LedgerState value. Not a DB enum: the platform's existing convention is
    #: String + application-level validation, and a DB enum would need a
    #: migration for every future state.
    state = Column(String(32), nullable=False, default="OPEN", index=True)

    #: Populated only in LEDGER_INTEGRITY_FAULT. §24.15 requires the fault be
    #: recorded and the day refused, never repaired.
    integrity_fault_reason = Column(Text, nullable=True)

    sealed_at = Column(DateTime(timezone=True), nullable=True)

    #: The zone in force for this working day, persisted so a later tenant
    #: timezone change cannot retroactively re-attribute a sealed day (§12.2).
    timezone_iana = Column(String(64), nullable=False)
    utc_offset_seconds = Column(Integer, nullable=False, default=0)

    #: ── D-18D: the working-day result, frozen at seal ───────────────────────
    #: Whether this local calendar date was a working day for this Worker,
    #: as resolved at seal time. Frozen because the schedule is mutable: a
    #: later roster edit must not silently change what a sealed day was
    #: evaluated against (the §17 historical-reproducibility rule that also
    #: motivates `timezone_iana`).
    #:
    #: NOT NULL with NO default, by owner ruling D-18D. Every writer states it
    #: or the write fails — a default would let an unresolved day acquire a
    #: working-day status nobody determined.
    is_working_day = Column(Boolean, nullable=False)

    #: WHICH configuration tier produced `is_working_day`, carried verbatim
    #: from the resolver's own `EffectiveSchedule.sources` label:
    #: assignment | shift | employee | legacy_user | team | org | system.
    #:
    #: Not a second vocabulary — the resolver's label is reproduced unchanged so
    #: a sealed row can be reconciled against the resolver that produced it, and
    #: never re-coded into a more specific source.
    #:
    #: The producer REFUSES to seal when this would be "system": that value
    #: means no tier configured the working week and the Worker would be
    #: measured against a schedule nobody chose. The column still admits it so
    #: the vocabulary stays the resolver's, not a filtered copy of it.
    working_day_source = Column(String(32), nullable=False)

    #: ── D-1.1: the day's SCHEDULED END, frozen at seal ──────────────────────
    #: The local clock time this working day was scheduled to end, taken from
    #: `EffectiveSchedule.end_local` in the SAME resolution that produced
    #: `is_working_day` / `working_day_source` above. It is the second operand
    #: of §18 step 14 — `variance := actual_end_local - scheduled_end_local` —
    #: and it is frozen for exactly the reason D-18D froze the working-day
    #: result: the roster is mutable, and a later shift edit must not silently
    #: change what a sealed day was measured against (§17).
    #:
    #: NULLABLE, with two distinct meanings that must never be conflated:
    #:   * a row sealed BEFORE this column existed has no scheduled end and
    #:     never will — no backfill may invent one (owner ruling D-1.1);
    #:   * a row whose `scheduled_end_source` is "system" genuinely had no
    #:     configured shift end (owner ruling D-2.1).
    #: In both cases `schedule_variance_minutes` is NULL, never 0: a zero
    #: asserts the Worker left exactly on time, which is a claim about a fact
    #: nobody recorded.
    scheduled_end_local = Column(Time, nullable=True)

    #: WHICH configuration tier produced `scheduled_end_local`, carried
    #: verbatim from the resolver's own `EffectiveSchedule.sources["end_local"]`
    #: label: assignment | shift | employee | legacy_user | team | org | system.
    #: Same rule as `working_day_source` — the resolver's vocabulary is
    #: reproduced unchanged and never re-coded into a more specific source.
    #:
    #: UNLIKE `working_day_source`, "system" does NOT refuse the seal. Owner
    #: ruling D-2.1: schedule variance is "recorded; never drives the
    #: threshold" (§10) and is "never used in comparisons" (§35), so its
    #: absence withdraws one recorded field, not the whole day. D6-A's
    #: fail-closed rule stays scoped to the working-day determination, which
    #: does gate evaluation.
    scheduled_end_source = Column(String(32), nullable=True)

    #: ── D18-F2: the day's verified departure, frozen at seal ────────────────
    #: WHY THIS IS DAY-LEVEL AND NOT AN INTERVAL
    #: Per-interval provenance answers "how did the boundary that ENDED this
    #: interval come to exist?". The producer attaches a verified departure to
    #: the WRK segment that terminates at the clock-out — but that segment can
    #: legitimately be ZERO-LENGTH and therefore absent, when a break or an
    #: observed offline gap runs flush to the clock-out, or when a break spans
    #: the whole session. The audit proved three such shapes sealing with
    #: `has_verified_departure_evidence = False` despite a genuine verified
    #: clock-out, which would mislead a future MISSING_CLOCK_OUT determination.
    #:
    #: So the departure is recorded here as well: day-level EVIDENCE, derived
    #: from a D-15 boundary event and never inferred. Interval provenance
    #: semantics are UNCHANGED, `validate_intervals` is not weakened, and no
    #: zero-duration interval or new WorkState was introduced.
    #:
    #: NULL is legitimate: a LEDGER_INTEGRITY_FAULT row, or a future
    #: classifier-written MISSING_CLOCK_OUT day, genuinely has no verified
    #: departure. The three move together, and a producer-sealed row must
    #: always carry them (asserted by test, not by column).
    departure_at = Column(DateTime(timezone=True), nullable=True)

    #: WORKER or ADMIN_OVERRIDE only — the existing
    #: VERIFIED_DEPARTURE_PROVENANCE. PRODUCER_DERIVED is inadmissible here:
    #: it describes a boundary the producer authored, which is by definition
    #: not a departure.
    departure_provenance = Column(String(32), nullable=True)

    #: Opaque, auditable reference to the authoritative D-15 close event, e.g.
    #: "clock_session:12/boundary_event:34". No foreign key, matching
    #: `WorkStateInterval.source_ref` and D-15's `actor_label`.
    departure_source_ref = Column(String(128), nullable=True)

    #: ── D18-F3-S: the SOURCE-evidence fingerprint, frozen at seal ───────────
    #: A deterministic digest of the source records this version was built
    #: from — eligible session identity, D-15 verified boundary instants and
    #: provenance, complete break windows, applied offline windows.
    #:
    #: DELIBERATELY EXCLUDES resolved context (timezone, UTC offset,
    #: working-day result/source, workweek key, current time). Owner ruling
    #: D18-F3-S distinguishes a SOURCE change, which may supersede, from a
    #: CONTEXT change, which must refuse — and the existing canonical-interval
    #: fingerprint cannot make that distinction, because the context selects
    #: which sessions become intervals at all.
    #:
    #: Nullable: rows written before this column existed, and any future
    #: non-producer writer, have none.
    source_fingerprint = Column(String(64), nullable=True)

    #: Supersession chain (§23). Corrections supersede; they do not mutate.
    supersedes = Column(
        Integer, ForeignKey("work_day_ledger.id", ondelete="SET NULL"), nullable=True,
    )
    superseded_by = Column(
        Integer, ForeignKey("work_day_ledger.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_by = Column(String(64), nullable=False, default="SYSTEM")

    intervals = relationship(
        "WorkStateInterval",
        back_populates="ledger",
        cascade="all, delete-orphan",
        order_by="WorkStateInterval.start_at_utc, WorkStateInterval.seq",
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", "work_date", "ledger_version",
            name="uq_work_day_ledger_natural_key",
        ),
        # The hot read: the current (non-superseded) row for a Worker-day.
        Index(
            "ix_work_day_ledger_current",
            "organization_id", "user_id", "work_date", "superseded_by",
        ),
        Index("ix_work_day_ledger_org_week", "organization_id", "workweek_key"),
        CheckConstraint("ledger_version >= 1", name="ck_work_day_ledger_version_positive"),
        # D18-F2: the three departure fields move together. A half-recorded
        # departure would be worse than none — it would look like evidence.
        CheckConstraint(
            "(departure_at IS NULL AND departure_provenance IS NULL "
            " AND departure_source_ref IS NULL) OR "
            "(departure_at IS NOT NULL AND departure_provenance IS NOT NULL "
            " AND departure_source_ref IS NOT NULL)",
            name="ck_work_day_ledger_departure_all_or_none",
        ),
        # D18-F2, as narrowly superseded for §24.7: a departure recorded here
        # is either VERIFIED (WORKER, ADMIN_OVERRIDE) or an explicitly
        # SYNTHESIZED exit the day is deferred on. Generated from the
        # vocabulary rather than typed out, so constraint and enums cannot
        # disagree. Admissible is NOT verified — see the module constant.
        CheckConstraint(
            "departure_provenance IS NULL OR departure_provenance IN "
            f"({_ADMISSIBLE_DEPARTURE_SQL})",
            name="ck_work_day_ledger_departure_verified",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<WorkDayLedger org={self.organization_id} user={self.user_id} "
            f"date={self.work_date} v={self.ledger_version} state={self.state}>"
        )


class WorkStateInterval(Base):
    """One sealed state interval. The evidence itself.

    PROVENANCE IS MANDATORY
    -----------------------
    ``provenance`` is NOT NULL by owner ruling at Gate 6, extended by correction
    C-02 to cover derived break boundaries as well as session closes. No
    interval may exist without it, and it is immutable — a change occurs only by
    superseding the ledger version that carries it.

    DURATION
    --------
    ``duration_seconds`` is stored as integer seconds alongside the boundary
    instants so a historical figure can be verified without re-deriving it from
    timestamps whose timezone rules may since have changed. No rounding happens
    here; §16.2 places the single rounding site in evaluation.
    """

    __tablename__ = "work_state_interval"

    id = Column(Integer, primary_key=True, index=True)

    ledger_id = Column(
        Integer, ForeignKey("work_day_ledger.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    #: Denormalised for tenant-scoped queries that never want to join.
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    #: Deterministic ordering tiebreaker. Two intervals may legitimately share
    #: a start instant, and INV-8 forbids order depending on iteration.
    seq = Column(Integer, nullable=False)

    #: WorkState value: WRK / TRV / ONC / BRK_M / BRK_R / OFF / LVE / UNK.
    state = Column(String(8), nullable=False, index=True)

    start_at_utc = Column(DateTime(timezone=True), nullable=False)
    end_at_utc = Column(DateTime(timezone=True), nullable=False)
    duration_seconds = Column(Integer, nullable=False)

    #: Provenance value. NOT NULL — see the class docstring.
    provenance = Column(String(32), nullable=False, index=True)

    #: Originating record, e.g. "clock_session:1234" / "break_session:99".
    source_ref = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    ledger = relationship("WorkDayLedger", back_populates="intervals")

    __table_args__ = (
        UniqueConstraint("ledger_id", "seq", name="uq_work_state_interval_seq"),
        Index("ix_work_state_interval_order", "ledger_id", "start_at_utc", "seq"),
        Index("ix_work_state_interval_org_state", "organization_id", "state"),
        CheckConstraint("duration_seconds >= 0", name="ck_work_state_interval_duration"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<WorkStateInterval ledger={self.ledger_id} seq={self.seq} "
            f"{self.state} {self.duration_seconds}s prov={self.provenance}>"
        )


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — provenance immutability, enforced rather than documented
# ══════════════════════════════════════════════════════════════════════════════
# Owner ruling at Gate 6: provenance is IMMUTABLE. A change occurs only by
# superseding the ledger version that carries it (INV-3, §23), never by
# rewriting the row.
#
# Two listeners, deliberately:
#
#   * the attribute ``set`` listener fails at the point of mutation, so a
#     developer sees the violation where they caused it rather than at a flush
#     far away;
#   * the ``before_update`` mapper listener is the net that catches anything
#     which reaches a flush by another route.
#
# Neither can intercept a Core-level bulk UPDATE that bypasses the ORM
# entirely. That limitation is stated rather than papered over: enforcing it
# there would need a database trigger.
#
# The original note here justified deferring that trigger by "the four-head
# Alembic condition". That condition never existed — it was a parser error, see
# the package docstring. The trigger remains absent for a different and still
# current reason: the owner reviewed this limitation at Step 2 and ruled
# ORM-level enforcement sufficient, explicitly declining a database trigger.
# Adding one now would be an unsolicited strengthening of an accepted decision.

@event.listens_for(WorkStateInterval.provenance, "set", propagate=True)
def _reject_provenance_mutation(target, value, oldvalue, initiator):
    """Refuse to overwrite a provenance that has already been assigned.

    ``oldvalue`` is SQLAlchemy's NO_VALUE / NEVER_SET sentinel while the
    instance is being constructed, so the initial assignment passes. Any later
    assignment to a different value is a rewrite of evidence.
    """
    # Sentinels are not comparable values; treat anything non-string as "unset".
    if not isinstance(oldvalue, str):
        return value
    if oldvalue == value:
        return value
    raise ProvenanceImmutable(
        f"provenance is immutable: attempted to change {oldvalue!r} -> "
        f"{value!r}. Correct a sealed day by superseding it, never by "
        f"rewriting the interval."
    )


@event.listens_for(WorkStateInterval, "before_update", propagate=True)
def _reject_provenance_update(mapper, connection, target):
    """Flush-time net for a provenance change that reached the database layer."""
    history = sa_inspect(target).attrs.provenance.history
    if history.has_changes() and history.deleted:
        old = history.deleted[0]
        new = history.added[0] if history.added else None
        if old != new:
            raise ProvenanceImmutable(
                f"provenance is immutable: flush would change {old!r} -> "
                f"{new!r} on interval id={target.id}. Supersede the ledger "
                f"version instead."
            )


# ═════════════════════════════════════════════════════════════════════════════
# D18-F2 / D18-F3-S — the frozen day-level evidence is immutable too
# ═════════════════════════════════════════════════════════════════════════════
# Same rule as interval provenance, one level up: a sealed day's verified
# departure and source fingerprint are changed by SUPERSEDING the version that
# carries them, never by rewriting the row.

#: Frozen once written. ``superseded_by`` is deliberately absent — setting it
#: is how a supersession links the chain, and that is a legitimate write.
_FROZEN_LEDGER_EVIDENCE = (
    "departure_at", "departure_provenance", "departure_source_ref",
    "source_fingerprint",
    # R-12.3.4: "Changing it MUST NOT retroactively re-attribute sealed weeks."
    # The rule is provenance of how work_date was chosen, so rewriting it on a
    # sealed row would re-describe an attribution that already happened.
    "shift_attribution_rule",
    # §12.4: historical workweeks "change only by the addition of correction
    # events, never by mutation". The writer already inherits this identity
    # down a supersession chain; this is the same guarantee against a direct
    # UPDATE, which no code path legitimately performs.
    "workweek_key",
    # D-1.1: the scheduled end and its source tier are frozen on exactly the
    # same terms as the working-day result they were resolved beside. A later
    # roster edit must not reach back through a sealed row.
    "scheduled_end_local",
    "scheduled_end_source",
    # ── Owner Decision 2C ───────────────────────────────────────────────────
    # The eleven below were UNGUARDED until the append-only readiness audit,
    # which demonstrated empirically that `state`, `work_date`, `ledger_version`
    # and `timezone_iana` could be rewritten on a SEALED row. The owner declined
    # database triggers for this table — an unconditional one would break the
    # `superseded_by` link that §23/INV-3 depends on — and authorized closing
    # the ORM gap instead. This option does NOT claim database-level
    # immutability, and says so: a Core UPDATE still reaches the row. What it
    # does claim is that no ORM path can rewrite the row's identity or its
    # frozen context.
    #
    # Every one of these is written EXACTLY ONCE, by the single
    # `WorkDayLedger(...)` construction in `service.py::_write_version` — which
    # is also why freezing them costs nothing: production sets them at INSERT,
    # where `before_update` does not fire.
    #
    # IDENTITY. A row whose tenant, Worker, date or version can move is not the
    # row anything else references. `supersedes` joins them because the chain's
    # backward pointer is set at construction (`supersedes=current.id`), never
    # back-filled — the forward pointer `superseded_by` is the one that cannot
    # be, because the successor has no id until it is inserted.
    "organization_id", "user_id", "work_date", "ledger_version", "supersedes",
    # FROZEN CONTEXT. §16.4 and the producer's own D18-F3 context freeze: a day
    # is evaluated against the configuration in force when it was sealed. A
    # later timezone change, offset correction or working-day reclassification
    # that reached back through a sealed row would silently re-describe a day
    # that has already been measured, and every downstream week would be
    # answering a question about evidence that no longer exists.
    "timezone_iana", "utc_offset_seconds", "is_working_day",
    "working_day_source",
    # PROVENANCE OF THE WRITE ITSELF. Attribution that can be rewritten after
    # the fact is not attribution — the same reason `config_version` freezes
    # `captured_by_label`.
    "created_at", "created_by",
)

#: Deliberately NOT frozen, and each for a stated reason:
#:
#: ``superseded_by``      the supersession link (above).
#: ``state``              OPEN -> SEALED / MISSING_CLOCK_OUT /
#:                        LEDGER_INTEGRITY_FAULT is a lifecycle transition the
#:                        owner reserved, even though `_write_version` sets the
#:                        state at construction today and no UPDATE path exists.
#: ``sealed_at``          populated when a day is sealed.
#: ``integrity_fault_reason``
#:                        populated when a day enters LEDGER_INTEGRITY_FAULT.
#:
#: The last three are reserved rather than required: the audit found no
#: production path that updates any of them. Keeping them writable is the
#: conservative choice and costs nothing.


@event.listens_for(WorkDayLedger, "before_update", propagate=True)
def _reject_frozen_ledger_evidence_update(mapper, connection, target):
    """Refuse a flush that would rewrite frozen day-level evidence.

    TWO STRICTNESSES, SELECTED BY THE ROW'S STATE
    ---------------------------------------------
    On an OPEN row, only a change FROM a recorded value is refused: populating
    a field that was NULL is how a first write completes, and a no-op rewrite
    of the identical value is not a change.

    On a row that is no longer OPEN the rule tightens to ALL THREE transitions
    — ``NULL -> value``, ``value -> NULL`` and ``value -> different value``.
    Owner ruling D-1.1 requires it: a nullable frozen column whose NULL is
    permanently meaningful ("this day has no scheduled end, and none may be
    invented") is otherwise backfillable in place by any later writer, which is
    precisely the fabrication the ruling forbids. Leaving the old leniency
    would have made `scheduled_end_local` writable on an already-sealed row.

    This is the SAME mechanism, with a per-state bound. No second immutability
    path is introduced: one listener, one field tuple, one exception.

    THE OLD VALUE IS READ FROM THE DATABASE (Owner Decision 2, 2026-09-23)
    ---------------------------------------------------------------------
    This guard previously compared against ``attrs[field].history.deleted`` and
    skipped the field when that was empty. On an EXPIRED instance it always is:
    setting an attribute that was never loaded records a new value and no old
    one, so ``deleted`` is ``()`` and every frozen field was skipped.
    ``SessionLocal`` uses SQLAlchemy's default ``expire_on_commit=True``, so in
    production every attribute is expired after any ``commit()`` — which made
    the whole tuple inert exactly where it mattered. It only ever fired in
    suites that set ``expire_on_commit=False``.

    So the committed value is now read from the database, through the
    connection THIS flush is using — the technique ``EcoDayRecord`` already
    uses, and for the same reason it gives there: session state can be wrong in
    two ways, EXPIRED (no old value, which reads as NULL and makes a forbidden
    rewrite look like a first write) and LOADED-BUT-STALE (another transaction
    has since changed the row). Asking the database answers for whichever
    transaction is flushing, which is the only answer that means anything.

    WHAT THIS GUARD DOES NOT COVER
    ------------------------------
    A true Core ``UPDATE`` — ``session.execute(sa.update(WorkDayLedger)...)``
    — fires no mapper event at all and is NOT refused here. Owner Decision 2C
    declined a database trigger for this table, because an unconditional one
    would refuse the ``superseded_by`` link §23 / INV-3 requires. This guard
    covers every ORM flush path, expired or not; it does not and cannot claim
    database-level immutability.
    """
    inspected = sa_inspect(target)
    changed = tuple(
        field for field in _FROZEN_LEDGER_EVIDENCE
        if inspected.attrs[field].load_history().has_changes()
    )
    if not changed:
        return

    table = WorkDayLedger.__table__
    stored = connection.execute(
        select(table.c.state, *[table.c[field] for field in changed])
        .where(table.c.id == target.id)
    ).one_or_none()
    if stored is None:
        # No persisted row to protect — nothing this guard is about.
        return

    stored_state, *stored_values = stored

    # Strict if the row is not OPEN either BEFORE or AFTER this flush. Taking
    # both sides is what keeps the change from loosening anything: the old
    # reading used the in-session value alone, and a stored-value-only reading
    # would have been laxer for a flush that seals and backfills at once.
    state_history = inspected.attrs["state"].load_history()
    pending_state = (state_history.added[0] if state_history.added
                     else stored_state)
    open_value = LedgerState.OPEN.value
    sealed = stored_state != open_value or pending_state != open_value

    for field, old in zip(changed, stored_values):
        history = inspected.attrs[field].load_history()
        new = history.added[0] if history.added else None
        if old == new:
            continue
        if old is None and not sealed:
            # OPEN row completing a first write. Still refused once sealed.
            continue
        raise ProvenanceImmutable(
            f"WorkDayLedger.{field} is immutable: flush would change "
            f"{old!r} -> {new!r} on ledger id={target.id}. Correct a "
            f"sealed day by superseding it, never by rewriting it."
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

@event.listens_for(WorkStateInterval.__table__, "after_create")
def _install_interval_append_only_guard(target, connection, **kw):
    """Emit the dialect's append-only statements for the interval set.

    ``WorkDayLedger`` deliberately gets NO trigger: its ``superseded_by``
    link is an authorized UPDATE (§23 / INV-3), and an unconditional
    trigger would refuse it. See ``append_only_ddl.py``.
    """
    for statement in append_only_statements(connection.dialect.name):
        connection.execute(text(statement))

__all__ = ["WorkDayLedger", "WorkStateInterval"]
