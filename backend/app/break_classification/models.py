"""
Classification authority, assertions, and the immutable day snapshot.
=====================================================================

Three tables, each append-only:

    EcoClassificationAuthority   WHO may assert a break's legal category
    BreakClassificationAssertion WHAT was asserted about one break, by whom,
                                 citing what evidence
    ClassificationSnapshot       the content-addressed set of assertions ONE
                                 evaluation actually used

WHY A DEDICATED AUTHORITY, NOT A ROLE
-------------------------------------
Owner ruling P-1 forbids inferring classification authority from ``User.role``,
``Team.head_user_id`` or any other existing role model, for the same reason
``EcoViewGrant`` exists rather than reusing an admin flag: those roles were
defined for other purposes and would silently widen as they change. A grant
here means exactly one thing and changes only when someone grants it.

WHAT THIS PACKAGE DOES NOT DECIDE
---------------------------------
It does not decide WHETHER a given break is a meal or a rest. P-1 leaves the
legal and jurisdictional rules explicitly unresolved and forbids inventing
them. What is modelled here is the CHAIN OF CUSTODY: who determined it, when,
against which evidence, and how to find that evidence again. When the rules are
supplied by the appropriate authority they attach to ``evidence_ref`` — no
schema here has to change, which is exactly why the reference is preserved.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, event,
)
from sqlalchemy import inspect as sa_inspect

from database.database import Base

from .errors import ClassificationImmutable
from .vocabulary import CLASSIFICATION_RESULTS

AUTHORITY_TABLE = "eco_classification_authority"
ASSERTION_TABLE = "break_classification_assertion"
SNAPSHOT_TABLE = "classification_snapshot"

#: Rendered into CHECK constraints. Written out rather than interpolated from
#: the frozenset so the DDL text is stable and reviewable in the migration.
_RESULTS_SQL = "'BRK_M', 'BRK_R', 'UNCLASSIFIABLE'"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EcoClassificationAuthority(Base):
    """One tenant's explicit grant of ECO break-classification authority.

    Deliberately shaped after ``EcoViewGrant``: an explicit, durable,
    tenant-scoped grant to one identity, with its own provenance. Absence of a
    row is a refusal, never a fallback to some other role.
    """

    __tablename__ = AUTHORITY_TABLE

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    #: The identity holding the authority. A real user, because an assertion
    #: must name a party who can be asked what they determined.
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    #: A stable label for this authority, carried onto every assertion so the
    #: assertion stays explainable if the grant is later revoked.
    authority_label = Column(String(128), nullable=False)

    granted_by = Column(String(128), nullable=False, default="SYSTEM")
    granted_at_utc = Column(DateTime(timezone=True), nullable=False,
                            default=_utcnow)

    #: NULL means live. Revocation is a timestamp, never a delete: assertions
    #: already made under this authority stay explainable.
    revoked_at_utc = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id",
                         name="uq_eco_classification_authority_identity"),
        CheckConstraint("length(trim(authority_label)) > 0",
                        name="ck_eco_classification_authority_label_nonempty"),
        CheckConstraint("length(trim(granted_by)) > 0",
                        name="ck_eco_classification_authority_actor_nonempty"),
    )

    @property
    def is_live(self) -> bool:
        return self.revoked_at_utc is None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<EcoClassificationAuthority org={self.organization_id} "
                f"user={self.user_id} {self.authority_label}>")


class BreakClassificationAssertion(Base):
    """One authority's determination about one break interval.

    Append-only and versioned. A changed determination appends a new version
    that points at its predecessor; nothing is rewritten, so the record of what
    was believed at evaluation time survives the correction.
    """

    __tablename__ = ASSERTION_TABLE

    id = Column(Integer, primary_key=True, index=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True)

    #: The day the break belongs to, by the ledger's own START_DATE
    #: attribution. Carried so a day's assertions are one indexed read.
    work_date = Column(Date, nullable=False, index=True)

    #: The canonical source identity of the break being classified, e.g.
    #: ``break_session:12`` — the exact form the producer already writes into
    #: ``WorkStateInterval.source_ref``/``Window.source_ref``. Opaque here: no
    #: foreign key, matching the existing ``source_ref`` convention, so the
    #: assertion does not bind to a table whose rows may be archived.
    source_ref = Column(String(128), nullable=False)

    #: One of BRK_M · BRK_R · UNCLASSIFIABLE. Text, not a DB enum: §32 warns
    #: enum values are a product surface that cannot be corrected later.
    classification = Column(String(32), nullable=False)

    # ── provenance (P-1's required elements) ────────────────────────────────
    #: WHERE the determination came from — a document, a ruling reference, an
    #: import batch. Opaque and mandatory: P-1 requires the evidence reference
    #: be preserved so authoritative rules can attach to it later.
    evidence_ref = Column(String(255), nullable=False)

    #: Free text about the evidence. Never parsed, never rendered to a Worker.
    evidence_detail = Column(Text, nullable=True)

    #: The asserting authority, by grant id AND by label. The id is the link;
    #: the label survives the grant being revoked.
    authority_id = Column(
        Integer, ForeignKey(f"{AUTHORITY_TABLE}.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    authority_label = Column(String(128), nullable=False)

    asserted_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)

    # ── lineage ─────────────────────────────────────────────────────────────
    #: Monotonic per ``(organization_id, user_id, work_date, source_ref)``.
    assertion_version = Column(Integer, nullable=False, default=1)

    #: The assertion this one replaced. Written once at insert, never updated.
    supersedes = Column(
        Integer, ForeignKey(f"{ASSERTION_TABLE}.id", ondelete="SET NULL"),
        nullable=True)

    #: NULL means LIVE. Set exactly once, NULL -> a value, when superseded.
    superseded_by = Column(
        Integer, ForeignKey(f"{ASSERTION_TABLE}.id", ondelete="SET NULL"),
        nullable=True, index=True)

    #: SHA-256 over the determination and its provenance. Two identical
    #: assertions hash identically, which is what lets a snapshot be
    #: content-addressed.
    content_hash = Column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", "work_date",
                         "source_ref", "assertion_version",
                         name="uq_break_classification_assertion_version"),
        Index("ix_break_classification_assertion_day",
              "organization_id", "user_id", "work_date"),
        CheckConstraint(f"classification IN ({_RESULTS_SQL})",
                        name="ck_break_classification_assertion_vocabulary"),
        CheckConstraint("length(trim(source_ref)) > 0",
                        name="ck_break_classification_assertion_source_nonempty"),
        CheckConstraint("length(trim(evidence_ref)) > 0",
                        name="ck_break_classification_assertion_evidence_nonempty"),
        CheckConstraint("assertion_version >= 1",
                        name="ck_break_classification_assertion_version_positive"),
        CheckConstraint("length(content_hash) > 0",
                        name="ck_break_classification_assertion_hash_nonempty"),
    )

    @property
    def is_live(self) -> bool:
        return self.superseded_by is None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<BreakClassificationAssertion {self.source_ref} "
                f"{self.classification} v{self.assertion_version}>")


class ClassificationSnapshot(Base):
    """The immutable, content-addressed set of assertions ONE evaluation used.

    Modelled on ``LeaveInputSnapshot`` exactly, because C-1 says to: the public
    key IS the content hash, nothing allocates it, and two evaluations that
    resolved identical classifications share one row rather than writing two.
    """

    __tablename__ = SNAPSHOT_TABLE

    id = Column(Integer, primary_key=True, index=True)

    #: The public identity. Equals ``content_hash`` — derived, never allocated.
    snapshot_key = Column(String(64), nullable=False, index=True)

    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    work_date = Column(Date, nullable=False, index=True)

    content_hash = Column(String(64), nullable=False)

    #: How many assertions the snapshot froze. Zero is legitimate and means
    #: "this day contained no breaks to classify" — an explicit EMPTY snapshot,
    #: never a NULL reference, exactly as the leave snapshot does.
    assertion_count = Column(Integer, nullable=False)

    #: Whether any frozen assertion was UNCLASSIFIABLE. Derived at capture and
    #: stored so the recording boundary does not re-scan the set.
    has_unclassifiable = Column(Integer, nullable=False, default=0)

    captured_at_utc = Column(DateTime(timezone=True), nullable=False,
                             default=_utcnow)

    #: The frozen members, as a canonical JSON array of
    #: ``[source_ref, classification, assertion_content_hash]`` triples. Stored
    #: rather than re-derived so the snapshot explains itself after the
    #: assertions have been superseded many times over.
    members_json = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("organization_id", "snapshot_key",
                         name="uq_classification_snapshot_key"),
        Index("ix_classification_snapshot_day",
              "organization_id", "user_id", "work_date"),
        CheckConstraint("length(snapshot_key) > 0",
                        name="ck_classification_snapshot_key_nonempty"),
        CheckConstraint("length(content_hash) > 0",
                        name="ck_classification_snapshot_hash_nonempty"),
        # The public key IS the content hash. Enforced at the schema, not only
        # in the writer — the same rule ``LeaveInputSnapshot`` states.
        CheckConstraint("snapshot_key = content_hash",
                        name="ck_classification_snapshot_key_is_hash"),
        CheckConstraint("assertion_count >= 0",
                        name="ck_classification_snapshot_count_nonnegative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<ClassificationSnapshot {self.snapshot_key[:12]}… "
                f"n={self.assertion_count}>")


# ══════════════════════════════════════════════════════════════════════════════
# Content addressing
# ══════════════════════════════════════════════════════════════════════════════
def compute_assertion_content_hash(
    *,
    organization_id: int,
    user_id: int,
    work_date,
    source_ref: str,
    classification: str,
    evidence_ref: str,
    authority_label: str,
) -> str:
    """SHA-256 over the determination and the chain of custody behind it.

    ``asserted_at_utc`` and the version are EXCLUDED: they are provenance about
    the recording, not about the determination, so re-asserting the identical
    finding produces the identical digest. That is what lets an unchanged
    classification replay idempotently instead of minting a new snapshot.
    """
    payload = {
        "organization_id": int(organization_id),
        "user_id": int(user_id),
        "work_date": work_date.isoformat(),
        "source_ref": source_ref,
        "classification": classification,
        "evidence_ref": evidence_ref,
        "authority_label": authority_label,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_snapshot_hash(
    *,
    organization_id: int,
    user_id: int,
    work_date,
    members: Sequence[Sequence[str]],
) -> str:
    """SHA-256 over the day's identity plus the ORDERED frozen members.

    Members are sorted by ``source_ref`` before hashing, so the digest does not
    depend on query order, insertion order or set iteration — the determinism
    INV-8 requires and C-1 restates.
    """
    ordered = sorted([list(m) for m in members], key=lambda m: m[0])
    payload = {
        "organization_id": int(organization_id),
        "user_id": int(user_id),
        "work_date": work_date.isoformat(),
        "members": ordered,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_members_json(members: Iterable[Sequence[str]]) -> str:
    """The stored member list, ordered exactly as the hash orders it."""
    ordered = sorted([list(m) for m in members], key=lambda m: m[0])
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


# ══════════════════════════════════════════════════════════════════════════════
# Append-only enforcement
# ══════════════════════════════════════════════════════════════════════════════
#: The ONE field an assertion may change, once, NULL -> a value. Supersession
#: needs the successor's id, which does not exist until it is inserted, so
#: linking the predecessor is necessarily an UPDATE. Authorised as a NAMED,
#: NARROW exception — the same shape ``EcoDayRecord`` uses.
_ASSERTION_SUPERSESSION_FIELD = "superseded_by"

#: Revocation is the authority's one permitted transition, NULL -> a timestamp.
_AUTHORITY_REVOCATION_FIELD = "revoked_at_utc"


def _refuse(target, verb: str):
    raise ClassificationImmutable(
        f"{type(target).__name__} id={getattr(target, 'id', None)} cannot be "
        f"{verb}. A changed determination is a NEW version; what was believed "
        f"at evaluation time is not rewritten."
    )


def _guard_update(mapper, target, allowed: str):
    state = sa_inspect(target)
    if not state.modified:
        return
    changed = {name for name in mapper.columns.keys()
               if state.attrs[name].history.has_changes()}
    if not changed:
        return
    if changed == {allowed}:
        history = state.attrs[allowed].load_history()
        old = history.deleted[0] if history.deleted else None
        if old is None:
            return  # NULL -> value, the authorised transition
    _refuse(target, "updated")


@event.listens_for(BreakClassificationAssertion, "before_update", propagate=True)
def _reject_assertion_update(mapper, connection, target):
    _guard_update(mapper, target, _ASSERTION_SUPERSESSION_FIELD)


@event.listens_for(BreakClassificationAssertion, "before_delete", propagate=True)
def _reject_assertion_delete(mapper, connection, target):
    _refuse(target, "deleted")


@event.listens_for(EcoClassificationAuthority, "before_update", propagate=True)
def _reject_authority_update(mapper, connection, target):
    _guard_update(mapper, target, _AUTHORITY_REVOCATION_FIELD)


@event.listens_for(EcoClassificationAuthority, "before_delete", propagate=True)
def _reject_authority_delete(mapper, connection, target):
    _refuse(target, "deleted")


@event.listens_for(ClassificationSnapshot, "before_update", propagate=True)
def _reject_snapshot_update(mapper, connection, target):
    state = sa_inspect(target)
    if state.modified and any(state.attrs[n].history.has_changes()
                              for n in mapper.columns.keys()):
        _refuse(target, "updated")


@event.listens_for(ClassificationSnapshot, "before_delete", propagate=True)
def _reject_snapshot_delete(mapper, connection, target):
    _refuse(target, "deleted")


__all__ = [
    "EcoClassificationAuthority",
    "BreakClassificationAssertion",
    "ClassificationSnapshot",
    "compute_assertion_content_hash",
    "compute_snapshot_hash",
    "canonical_members_json",
    "AUTHORITY_TABLE",
    "ASSERTION_TABLE",
    "SNAPSHOT_TABLE",
]
