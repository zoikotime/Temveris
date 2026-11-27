"""
The persisted ECO day evaluation — SQLAlchemy model.
====================================================

EVAL-DAY, under owner rulings R1, R2, R3, R14, R15.

WHAT THIS IS (R1)
-----------------
The persisted ECO evaluation RESULT for one Worker-day against one authoritative
snapshot. It is a **derived artifact**, not evidence. The sealed work ledger
remains the authority for source intervals; this table records what the
evaluator concluded from them.

    ledger  = authoritative input
    record  = derived output

SNAPSHOT IDENTITY (R2, extended by Owner Ruling 2 F)
----------------------------------------------------
    (organization_id, user_id, work_date,
     ledger_version, policy_version, config_version,
     leave_input_snapshot_key)

Every authoritative input is in the key, so re-evaluating the same snapshot is
idempotent by construction and a changed input produces a NEW row rather than
mutating an old one (R1).

The seventh component was added when MODEL-EXCLUSION made approved-leave
overlap an authoritative input. Until then R2's premise was false in one case:
leave could change the answer without changing the identity, so a corrected
re-evaluation reported a conflict and could never be recorded. The leave input
is now frozen into an immutable ``LeaveInputSnapshot`` and named here.

R2 also states that "no separate evaluation version column is needed — the
snapshot IS the version". That holds for IDENTITY and not for AUTHORITY: two
evaluations with different inputs have different identities, so both would
insert and both would read as current. ``evaluation_generation`` exists to
serialize that, and it is a sequence of evaluations OF one day rather than a
version of its inputs. Reported as a departure from R2's wording.

The generation is DERIVED FROM THE PREDECESSOR a writer read, never from a
separately queried maximum: the two reads could disagree, and a fresh
generation beside a stale predecessor is exactly how two rows once ended up
authoritative. The predecessor's pointer is then written by a guarded statement
requiring ``superseded_by IS NULL`` and exactly one affected row. See
``record_evaluation``.

``user_id`` rather than R2's ``worker_id``: owner ruling D-09 Option C makes the
existing user model canonical for technical identifiers, and Steps 1, 6, 7, 8 and
Worker Context all use ``user_id``. Reported as a naming consistency choice.

``policy_version`` is NULLABLE and, in this increment, always NULL. The platform
has no policy-version concept distinct from configuration versioning — Step 6's
``config_version`` is the only one that exists. The column is present because R2
names it in the authoritative identity; leaving it unpopulated rather than
aliasing it to ``config_version`` avoids inventing a second meaning for one
fact. Reported as an open decision.

Because two identity components are nullable, uniqueness uses a COALESCE
expression index: NULL never equals NULL in a unique index on PostgreSQL **or**
SQLite, so a plain constraint would let duplicate snapshots through. That is the
same defect found and fixed in Step 6.

CONTENT HASH
------------
A snapshot fixes every authoritative INPUT, so the same snapshot must yield the
same OUTPUT. If it does not, the evaluator itself changed. The hash makes that
detectable: a re-evaluation producing a different result raises rather than
silently overwriting the earlier derived answer.

WHAT THIS TABLE MUST NEVER GAIN (R3)
------------------------------------
No employment-action, recommendation, disciplinary or notification-state field.
No weekly accumulation. No event state. INV-9: nothing here labels a Worker.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index,
    Integer, String, UniqueConstraint, event, func, select,
    inspect as sa_inspect,
)

from database.database import Base

from .errors import DayRecordImmutable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EcoDayRecord(Base):
    """One derived ECO evaluation for one Worker-day snapshot."""

    __tablename__ = "eco_day_record"

    # No index=True on the primary key: already indexed on both backends, and an
    # auto-generated name is a collision risk (the Step 7 lesson).
    id = Column(Integer, primary_key=True)

    # ── Snapshot identity + provenance (R2, R14) ────────────────────────────
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
    work_date = Column(Date, nullable=False, index=True)

    #: The ledger version this result was derived from. Changing the evidence
    #: changes the snapshot, producing a new record rather than a rewrite.
    ledger_version = Column(Integer, nullable=False)

    #: Always NULL in this increment — see the module docstring.
    policy_version = Column(String(64), nullable=True)

    #: Step 6 ``ConfigVersionRef.config_version_id``. NULL when the requirement
    #: could not be resolved because no version covered the date.
    config_version = Column(String(64), nullable=True)

    #: The Step 5 outcome verbatim, so an unresolved result records WHY without
    #: a caller having to re-derive it.
    requirement_outcome = Column(String(32), nullable=False)

    #: WHICH condition deferred this day, when ``state`` is
    #: ``DEFERRED_PENDING_LEDGER`` (owner ruling D-1).
    #:
    #: NULLABLE, and NULL is a real answer rather than a missing one: it means
    #: the ledger/departure deferral this state has always carried — a missing
    #: clock-out (S3) or the §24.7 unverified exit. That is the reading every
    #: record written before D-1 has, which is why the migration performs NO
    #: backfill: writing a reason onto those rows would assert a determination
    #: nobody made.
    #:
    #: NOT metadata. D-1 is explicit that the reason is part of the
    #: authoritative evaluation meaning, so it joins the snapshot identity
    #: below and the result hash — two days differing only by why they deferred
    #: are not the same evaluation.
    evaluation_deferral_reason = Column(String(32), nullable=True)

    #: The immutable classification input this evaluation used (C-1).
    #:
    #: Content-addressed — the key IS ``ClassificationSnapshot.content_hash`` —
    #: and referenced by key rather than by foreign key, exactly as
    #: ``leave_input_snapshot_key`` and ``config_version`` are. NULL for an
    #: evaluation that did not depend on break classification, including every
    #: record written before this column existed; never backfilled.
    classification_snapshot_key = Column(String(64), nullable=True)

    #: The break-treatment profile that GOVERNED this evaluation (P2-D4).
    #:
    #: ``BreakTreatmentProfile.profile_ref``, resolved historically against the
    #: work date. A RULE, not an input — which is why it is its own identity
    #: component beside ``policy_version`` and ``config_version`` rather than
    #: being folded into the classification snapshot. The snapshot answers
    #: "what were these breaks determined to be"; this answers "under which
    #: treatment were those determinations applied", and collapsing the two
    #: would invent a second meaning for one fact.
    #:
    #: NULL means no profile governed the date, so break classification was not
    #: in force and no break minute could qualify. Never backfilled, and never
    #: an implicit default: a NULL profile is the absence of the control, not a
    #: neutral one.
    break_treatment_profile_ref = Column(String(64), nullable=True)

    #: SHA-256 over identity + the computed result. See the module docstring.
    content_hash = Column(String(64), nullable=False)

    evaluated_at_utc = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # ── Normative result fields (R3) ────────────────────────────────────────
    #: One of the six EcoDayState values. Text, not a DB enum: §32 warns enum
    #: values cannot be corrected later without a breaking change.
    state = Column(String(32), nullable=False, index=True)

    #: All NULL when the state could not be computed. §10.2: an unevaluated day
    #: must not carry a number a caller could read as a result.
    required_minutes = Column(Integer, nullable=True)
    qualifying_minutes = Column(Integer, nullable=True)
    gross_shortfall_minutes = Column(Integer, nullable=True)
    exclusion_minutes = Column(Integer, nullable=True)
    attributable_minutes = Column(Integer, nullable=True)
    #: The §11 policy grace actually used to decide this day's attributable
    #: shortfall (MODEL-GRACE / GV-24).
    #:
    #: NULLABLE, and NULL is a real answer rather than a missing one: it means
    #: no grace comparison was performed on this day. That is the truth for
    #: every uncomputed and deferred state, for a NOT_A_WORKING_DAY (§11:
    #: "no evaluation occurs"), and for every record written before this
    #: column existed. §11 exists precisely so grace is not "a magic number in
    #: the threshold comparison", so a fabricated 0 is not written where no
    #: policy was applied — the migration performs NO backfill for the same
    #: reason.
    #:
    #: WHY THE COLUMN IS NECESSARY AT ALL
    #: ---------------------------------
    #: Grace ZEROES an attributable shortfall while ``gross_shortfall_minutes``
    #: and ``exclusion_minutes`` keep their true values, so the row would
    #: otherwise hold ``gross - exclusion != attributable`` with nothing on it
    #: explaining the difference — indistinguishable from an exclusion-based
    #: reduction. §32 lists the field for this reason. ``DayEvaluation``
    #: refuses to construct such a row at all; see its ``__post_init__``.
    grace_minutes_applied = Column(Integer, nullable=True)
    #: Populated only where approved source semantics define it. This increment
    #: never populates it: R12 forbids inventing schedule-variance mathematics.
    schedule_variance_minutes = Column(Integer, nullable=True)

    #: MODEL-NETTING, §18 step 10 (D-6.4). Minutes of banked surplus this
    #: day consumed to reduce its own requirement gap.
    #:
    #: A DERIVED CREDIT, NOT A BALANCE. §15.1 / INV-3: "the bank balance is
    #: itself a fold over day records, never a stored counter." This column
    #: is one day's share of that fold, recorded so the row's arithmetic
    #: explains itself; the BALANCE is never stored anywhere and is
    #: recomputed from these rows whenever it is needed.
    #:
    #: It is therefore not an account: nothing reads it and adds to it,
    #: nothing decrements it, and correcting a day does not adjust it â a
    #: correction writes a NEW authoritative record whose own fold produced
    #: its own credit (D-6.11).
    #:
    #: 0 under `NONE` and on any day that needed no credit. NULL only where
    #: no evaluation happened at all, exactly like the other measurements.
    bank_credit_minutes = Column(Integer, nullable=True)

    #: MODEL-NETTING (D-6.11). The content-addressed digest of the day records
    #: this evaluation folded — the ELEVENTH snapshot identity component.
    #:
    #: Every other component names a fact about THIS day. A banked day also
    #: depends on other days, so without this the identity cannot express
    #: "same day, different fold": correcting Monday would make Tuesday
    #: re-evaluate to a different answer under an unchanged identity, which is
    #: refused as a result conflict rather than recorded as a supersession.
    #: With it, the corrected fold is a NEW identity and the existing
    #: supersession chain carries it.
    #:
    #: NULL wherever no fold ran — under `NONE`, and on every uncomputed state.
    #: An INPUT key, exactly like `leave_input_snapshot_key` and
    #: `classification_snapshot_key`, never a result.
    bank_input_key = Column(String(64), nullable=True)
    contributes_to_accumulator_minutes = Column(Integer, nullable=True)

    shortfall_computed = Column(Boolean, nullable=False, default=False)

    # ── §35 provenance (AC-16) ──────────────────────────────────────────────
    #
    # AC-16: "Every day record persists provenance: requirement source, policy
    # version, rounding profile, netting mode, ledger version, timezone."
    # Policy and ledger version are above; these are the rest.
    #
    # Every one of them is NULLABLE and none is back-filled. The table is
    # append-only, so a row written before these existed can never acquire
    # them -- NULL is the honest record of "this evaluation predates the
    # field", exactly as it is for `netting_decision_provenance` on
    # PolicyVersion under owner ruling F.

    #: §35.1: "qualifying_seconds AND qualifying_minutes are both persisted.
    #: Without the pre-rounding value, a historical figure cannot be verified
    #: against the rounding profile that produced it (INV-8)." §28.1 also
    #: requires the raw seconds be SHOWN, so the Worker can check the rounding
    #: by hand. Computed at the single fold site; previously discarded.
    qualifying_seconds = Column(Integer, nullable=True)

    #: §10.2: "The resolved value, the winning rank, the source object
    #: identifier and the policy version identifier MUST all be persisted on
    #: the day record." The resolver already returns all three; they simply
    #: were not carried. A required figure without its source cannot be
    #: defended, which is the whole reason §10 has a provenance token per rank.
    requirement_source = Column(String(32), nullable=True)
    requirement_source_rank = Column(Integer, nullable=True)
    requirement_source_ref = Column(String(128), nullable=True)

    #: §16.6: "The rounding profile identifier is persisted on every day record
    #: and shown in every explanation surface." Only HALF_UP is implemented
    #: (§16.3's Worker-favourable default); recording WHICH profile ran is what
    #: lets a later record under a different profile be told apart from this
    #: one rather than silently compared to it.
    rounding_profile = Column(String(32), nullable=True)

    #: §15: "Netting mode is recorded on every day record and shown in every
    #: explanation." Two days with identical arithmetic under NONE and under
    #: WEEKLY_BANK are different evaluations, and the record has to say which.
    netting_mode = Column(String(32), nullable=True)

    #: §12.2: the applicable identifier "MUST be persisted on every day record
    #: -- not resolved at read time, because timezone assignments change."
    #: Frozen from the sealed ledger that governed the day.
    timezone_iana = Column(String(64), nullable=True)
    utc_offset_seconds = Column(Integer, nullable=True)

    #: §35.1: "engine semver; changes force replay". Recorded but deliberately
    #: OUTSIDE the content hash -- see compute_result_hash. A replay under a
    #: newer engine must be able to supersede a record, not collide with it.
    calculation_version = Column(String(32), nullable=True)

    # ── Breaks (INV-4) ──────────────────────────────────────────────────────
    #
    # §35 stores observed and qualifying separately for each state, because
    # they differ wherever a Tenant treats meal as unpaid -- 45 observed, 0
    # qualifying -- and INV-4 is the invariant that triggered the v1.0
    # rebuild. The arithmetic already honoured the distinction; without these
    # the RECORD could not show it, and §26.1 makes that line the Worker's
    # only chance to catch a misclassified break before the day seals.
    brk_m_seconds = Column(Integer, nullable=True)
    brk_m_qualifying_seconds = Column(Integer, nullable=True)
    brk_r_seconds = Column(Integer, nullable=True)
    #: After the cumulative daily cap (§19.1).
    brk_r_qualifying_seconds = Column(Integer, nullable=True)

    # ── Leave input identity (Owner Ruling 2, F) ────────────────────────────
    #: The immutable leave input this evaluation used —
    #: ``LeaveInputSnapshot.snapshot_key``, which is that snapshot's own content
    #: hash. Part of the SNAPSHOT IDENTITY, not merely provenance: leave is an
    #: authoritative input, so a changed leave input must produce a new record
    #: rather than a conflict.
    #:
    #: NOT NULL, and never NULL to mean "no leave" (ruling E) — a day with no
    #: contributing leave references an explicit EMPTY snapshot, so "leave was
    #: evaluated and none contributed" stays distinguishable from "leave was
    #: never considered". Referenced by key rather than by foreign key, exactly
    #: as ``config_version`` references ``ConfigVersion.version_key``.
    #:
    #: The ``server_default`` exists only so the column can be added to an
    #: existing table — SQLite refuses ADD COLUMN NOT NULL without one. It is
    #: never a value the application relies on: ``record_evaluation`` refuses
    #: an evaluation carrying no key.
    leave_input_snapshot_key = Column(
        String(64), nullable=False, server_default="",
    )

    # ── Authority (Owner Ruling 2, H) ───────────────────────────────────────
    #: Monotonic per ``(organization_id, user_id, work_date)``, from 1.
    #:
    #: THIS is what serializes authority, not the marker below. Two concurrent
    #: evaluations with DIFFERENT inputs have different identities, so both
    #: inserts would otherwise succeed and both rows would read as current. The
    #: unique constraint on (day + generation) rejects one of them, and that
    #: writer re-reads and retries — the ``WorkDayLedger`` pattern, where
    #: ``uq_work_day_ledger_natural_key`` and not the ``superseded_by`` pointer
    #: closes the race.
    evaluation_generation = Column(
        Integer, nullable=False, server_default="1", default=1,
    )

    #: The record this one replaced. Written once at insert, never updated.
    supersedes = Column(
        Integer, ForeignKey("eco_day_record.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: NULL means AUTHORITATIVE. Non-NULL means superseded by that record.
    #:
    #: The single field the immutability rule permits to change, exactly once,
    #: NULL -> a value (ruling I). See ``_reject_update`` below.
    #:
    #: Active history is switched on for this attribute below the class — see
    #: ``_ACTIVE_HISTORY`` — because the guard's decision depends on the OLD
    #: value and an expired attribute has no old value in the session.
    superseded_by = Column(
        Integer, ForeignKey("eco_day_record.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    __table_args__ = (
        Index("ix_eco_day_record_org_user_date",
              "organization_id", "user_id", "work_date"),
        # The hot read: "the authoritative record for this Worker-day".
        # Mirrors ``ix_work_day_ledger_current``.
        Index("ix_eco_day_record_current",
              "organization_id", "user_id", "work_date", "superseded_by"),
        # The race serializer. Not a partial unique index on
        # ``superseded_by IS NULL``: a pointer needs the new row's id, so the
        # insert must precede the predecessor's update, which would transiently
        # leave two rows with NULL and violate such an index inside the writer's
        # own transaction — and unique indexes are not deferrable.
        UniqueConstraint(
            "organization_id", "user_id", "work_date", "evaluation_generation",
            name="uq_eco_day_record_generation",
        ),
        CheckConstraint("evaluation_generation >= 1",
                        name="ck_eco_day_record_generation_positive"),
        # Ruling E at the schema, not only in the writer. The ``server_default``
        # on the column exists solely so it could be ADDed to an existing table;
        # it is not a value any evaluation may carry. ``trim`` matches the
        # writer's own rule, which refuses a whitespace-only key.
        CheckConstraint("length(trim(leave_input_snapshot_key)) > 0",
                        name="ck_eco_day_record_leave_key_nonempty"),
        CheckConstraint("ledger_version >= 1",
                        name="ck_eco_day_record_ledger_version_positive"),
        CheckConstraint("length(state) > 0", name="ck_eco_day_record_state_nonempty"),
        CheckConstraint("length(content_hash) > 0",
                        name="ck_eco_day_record_hash_nonempty"),
        # Minutes are counts, never negative. R12 floors the shortfall at zero;
        # this stops a negative reaching storage by any other route.
        CheckConstraint(
            "(required_minutes IS NULL OR required_minutes >= 0) AND "
            "(qualifying_minutes IS NULL OR qualifying_minutes >= 0) AND "
            "(gross_shortfall_minutes IS NULL OR gross_shortfall_minutes >= 0) AND "
            "(exclusion_minutes IS NULL OR exclusion_minutes >= 0) AND "
            "(attributable_minutes IS NULL OR attributable_minutes >= 0) AND "
            "(contributes_to_accumulator_minutes IS NULL OR "
            " contributes_to_accumulator_minutes >= 0)",
            name="ck_eco_day_record_minutes_nonnegative",
        ),
        # Its own constraint rather than a widening of the one above: that
        # constraint already exists in every migrated database, SQLite cannot
        # redefine a CHECK in place, and rewriting the text here would leave a
        # create_all database and a migrated one governed differently.
        CheckConstraint(
            "grace_minutes_applied IS NULL OR grace_minutes_applied >= 0",
            name="ck_eco_day_record_grace_applied_nonnegative",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<EcoDayRecord org={self.organization_id} user={self.user_id} "
            f"{self.work_date} lv={self.ledger_version} {self.state}>"
        )


#: The authoritative uniqueness rule for a snapshot.
#:
#: A functional index, not a UniqueConstraint, because ``policy_version`` and
#: ``config_version`` are nullable and NULL never equals NULL in a unique index —
#: on PostgreSQL AND on SQLite. A plain constraint would let two records for the
#: same snapshot both be stored, which is precisely the idempotency R15
#: requires. Declared here as well as in the migration so a create_all-built
#: database (the test suite, the bootstrap path) is governed identically.
Index(
    "uq_eco_day_record_snapshot",
    EcoDayRecord.__table__.c.organization_id,
    EcoDayRecord.__table__.c.user_id,
    EcoDayRecord.__table__.c.work_date,
    EcoDayRecord.__table__.c.ledger_version,
    func.coalesce(EcoDayRecord.__table__.c.policy_version, ""),
    func.coalesce(EcoDayRecord.__table__.c.config_version, ""),
    # The seventh component (Owner Ruling 2, F). No COALESCE: the leave key is
    # NOT NULL, because a day with no leave references an explicit empty
    # snapshot rather than NULL.
    EcoDayRecord.__table__.c.leave_input_snapshot_key,
    # ── The eighth and ninth (owner rulings C-1 and D-1) ────────────────────
    # Both COALESCE'd, for the same reason ``policy_version`` and
    # ``config_version`` are: they are nullable, NULL never equals NULL in a
    # unique index on PostgreSQL OR SQLite, and without the coalesce two
    # records for one snapshot could both be stored — precisely the
    # idempotency R15 requires.
    #
    # Their presence is what makes a classification correction a NEW
    # GENERATION rather than a rewrite: a changed snapshot key is a different
    # identity, so the writer supersedes instead of colliding.
    func.coalesce(EcoDayRecord.__table__.c.classification_snapshot_key, ""),
    func.coalesce(EcoDayRecord.__table__.c.evaluation_deferral_reason, ""),
    # ── The tenth (owner ruling P2-D4) ──────────────────────────────────────
    # The governing break-treatment profile. Present for the same reason
    # ``policy_version`` is: a changed RULE is a different evaluation of the
    # same evidence, so a profile correction must supersede rather than
    # collide. Without it a Tenant who corrected a cap would find the new
    # answer rejected as a conflicting replay of the old identity.
    func.coalesce(EcoDayRecord.__table__.c.break_treatment_profile_ref, ""),
    # ── The eleventh (owner ruling D-6.11) ──────────────────────────────────
    # The fold's INPUTS. Every component above names a fact about THIS day; a
    # banked day also depends on other days, and without this the identity
    # cannot express "same day, different fold". Correcting Monday would make
    # Tuesday's re-evaluation collide with its own predecessor instead of
    # superseding it — the exact failure the eighth and ninth components were
    # added to prevent for classifications.
    func.coalesce(EcoDayRecord.__table__.c.bank_input_key, ""),
    unique=True,
)


def compute_result_hash(
    *,
    organization_id: int,
    user_id: int,
    work_date,
    ledger_version: int,
    policy_version: Optional[str],
    config_version: Optional[str],
    leave_input_snapshot_key: str,
    result: Mapping[str, Any],
    classification_snapshot_key: Optional[str] = None,
    break_treatment_profile_ref: Optional[str] = None,
    bank_input_key: Optional[str] = None,
    requirement_source: Optional[str] = None,
    requirement_source_rank: Optional[int] = None,
    requirement_source_ref: Optional[str] = None,
    rounding_profile: Optional[str] = None,
    netting_mode: Optional[str] = None,
    timezone_iana: Optional[str] = None,
    utc_offset_seconds: Optional[int] = None,
) -> str:
    """SHA-256 over the snapshot identity and the computed result.

    Canonical JSON — sorted keys, no insignificant whitespace, ASCII — so the
    digest does not depend on dict ordering or the platform's default encoding.

    ``leave_input_snapshot_key`` joins the IDENTITY portion (Owner Ruling 2, G).
    Without it the digest could not distinguish two records differing only by
    leave input, which is exactly the integrity check this hash exists for.
    """
    payload = {
        "organization_id": int(organization_id),
        "user_id": int(user_id),
        "work_date": work_date.isoformat(),
        "ledger_version": int(ledger_version),
        "policy_version": policy_version,
        "config_version": config_version,
        "leave_input_snapshot_key": leave_input_snapshot_key,
        # C-1: an authoritative input, so it joins the IDENTITY portion beside
        # the other three. Defaulted and emitted as null when absent, matching
        # how ``policy_version`` and ``config_version`` are already carried —
        # a record that used no classification hashes exactly as it always did.
        "classification_snapshot_key": classification_snapshot_key,
        # P2-D4: the governing rule joins the IDENTITY portion beside the
        # policy and configuration versions. Emitted as null when absent, so a
        # record that no profile governed hashes exactly as it always did.
        "break_treatment_profile_ref": break_treatment_profile_ref,
        # D-6.11: the fold's INPUTS, beside the leave and classification input
        # keys it mirrors. Emitted as null when no fold ran, so a record from
        # a non-banking policy hashes exactly as it always did.
        "bank_input_key": bank_input_key,
        # AC-16 provenance. These belong to the IDENTITY half, not the result:
        # each one GOVERNS the arithmetic rather than being produced by it. Two
        # days with the same minutes resolved from different ranks, rounded
        # under different profiles, or folded under different netting are not
        # the same evaluation, and a replay whose governing rule moved must not
        # read as identical.
        #
        # Defaulted and emitted as null when absent, matching how
        # `classification_snapshot_key`, `break_treatment_profile_ref` and
        # `bank_input_key` are already carried -- so a record that carried none
        # of them hashes exactly as it always did.
        #
        # `calculation_version` is deliberately NOT here. §35.1 gives it the
        # job of forcing replay; if it joined the digest, a replay under a
        # newer engine would raise a hash conflict against the old record
        # instead of superseding it, which is the opposite of what §23 wants.
        "requirement_source": requirement_source,
        "requirement_source_rank": requirement_source_rank,
        "requirement_source_ref": requirement_source_ref,
        "rounding_profile": rounding_profile,
        "netting_mode": netting_mode,
        "timezone_iana": timezone_iana,
        "utc_offset_seconds": utc_offset_seconds,
        "result": {k: result[k] for k in sorted(result)},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# Append-only enforcement — ORM level, per the standing ruling
# ══════════════════════════════════════════════════════════════════════════════
# No database trigger. The documented Core-level bulk-UPDATE limitation applies
# here as in Steps 2, 6, 7 and Worker Context.

def _refuse(target, verb: str):
    raise DayRecordImmutable(
        f"EcoDayRecord id={getattr(target, 'id', None)} cannot be {verb}. A "
        f"recomputation produces a new record under a new snapshot identity; "
        f"historical evaluations are not rewritten (R1)."
    )


#: The ONE field the immutability rule permits to change, and only once, from
#: NULL to a value (Owner Ruling 2, I).
#:
#: The supersession pointer needs the replacement's id, which does not exist
#: until the replacement is inserted — so linking the predecessor is
#: necessarily an UPDATE. It is authorized as a NAMED, NARROW exception rather
#: than by removing the listener: every other column, and every other
#: transition of this column, stays refused.
SUPERSESSION_FIELD = "superseded_by"

#: Load the committed value from the database whenever this attribute is SET.
#:
#: The default is not to: SQLAlchemy records only the new value, and for an
#: attribute that is EXPIRED — which ``Session.commit()`` does to every
#: attribute — there is then no old value at all. The guard below read that
#: absence as NULL and so accepted a forbidden non-NULL -> different non-NULL
#: re-point. ``active_history`` closes that at the source; the guard's
#: ``load_history()`` closes it again at the point of decision. Two mechanisms
#: because a superseded record silently becoming current again is the one
#: failure this table exists to prevent.
#:
#: Requested through an attribute event rather than on the ``Column``:
#: ``active_history`` is an ATTRIBUTE-level option that ``Column`` silently
#: accepts as a dialect argument and ignores, and reaching for the attribute's
#: ``impl`` at import time is too early — the mapper is not configured yet.
#: Registering a ``set`` listener with ``active_history=True`` is the supported
#: way to turn it on. The listener itself does nothing; the flag is the point.
@event.listens_for(EcoDayRecord.superseded_by, "set", active_history=True)
def _load_old_supersession_value(target, value, oldvalue, initiator):
    """No-op. Exists so the committed value is loaded before every set."""


@event.listens_for(EcoDayRecord, "before_update", propagate=True)
def _reject_update(mapper, connection, target):
    state = sa_inspect(target)
    if not state.modified:
        return

    changed = {
        name for name in mapper.columns.keys()
        if state.attrs[name].history.has_changes()
    }
    if not changed:
        return

    if changed == {SUPERSESSION_FIELD}:
        # ``load_history()``, NOT ``.history``: the plain accessor reports what
        # the session happens to hold, which for an EXPIRED attribute is
        # nothing at all. Only the NEW value is taken from it.
        history = state.attrs[SUPERSESSION_FIELD].load_history()
        new = history.added[0] if history.added else None

        # THE OLD VALUE IS READ FROM THE DATABASE, through the connection this
        # very flush is using — never from the session. Session state can be
        # wrong in two different ways: EXPIRED, so there is no old value and an
        # absent one reads as NULL; or LOADED BUT STALE, where the session
        # holds the NULL it read before another transaction superseded the row.
        # Both made a forbidden non-NULL -> different non-NULL re-point look
        # like the authorized NULL -> value transition. Asking the database
        # answers for whichever transaction is flushing, which is the only
        # answer that means anything.
        stored = connection.execute(
            select(EcoDayRecord.__table__.c[SUPERSESSION_FIELD])
            .where(EcoDayRecord.__table__.c.id == target.id)
        ).scalar()

        # Authorized: NULL -> a value, exactly once. Everything else — a
        # different successor, a return to NULL, or a no-op rewrite — is
        # refused, so a superseded record can never become current again.
        if stored is None and new is not None:
            return
        _refuse(target, f"re-pointed from {stored!r} to {new!r}")

    _refuse(target, f"modified ({sorted(changed)})")


@event.listens_for(EcoDayRecord, "before_delete", propagate=True)
def _reject_delete(mapper, connection, target):
    _refuse(target, "deleted")


__all__ = ["EcoDayRecord", "compute_result_hash"]
