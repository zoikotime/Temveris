"""
Granting authority, asserting classifications, and resolving a sealed day.
==========================================================================

POST-SEAL, ALWAYS (owner ruling P-2)
------------------------------------
``resolve_classification_for_day`` reads the CANONICAL sealed ledger and
refuses anything else: no ledger, an OPEN one, or one in
``LEDGER_INTEGRITY_FAULT``. The producer is never consulted and never asked to
classify — D-18 stands untouched, and a static guard in the tests asserts this
package is not imported by ``work_ledger_producer``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.work_ledger import LedgerState

from .errors import (
    ConflictingClassification,
    InvalidClassificationAssertion,
    InvalidClassificationAuthority,
)
from .models import (
    BreakClassificationAssertion,
    ClassificationSnapshot,
    EcoClassificationAuthority,
    canonical_members_json,
    compute_assertion_content_hash,
    compute_snapshot_hash,
)
from .outcomes import (
    REASON_CONFLICTING_ASSERTIONS,
    REASON_LEDGER_INTEGRITY_FAULT,
    REASON_LEDGER_NOT_SEALED,
    REASON_MALFORMED_SOURCE_REF,
    REASON_NO_LEDGER_DAY,
    REASON_UNKNOWN_CLASSIFICATION,
    ClassificationOutcome,
    ClassificationResolution,
)
from .vocabulary import (
    CLASSIFICATION_RESULTS,
    ClassificationResult,
    is_valid_classification,
)

#: The producer writes break carve-outs with this source-ref prefix
#: (``Window(start, end, "break", f"break_session:{row.id}")``). An assertion
#: must name a break, so the prefix is validated rather than assumed.
BREAK_SOURCE_PREFIX = "break_session:"


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


# ══════════════════════════════════════════════════════════════════════════════
# Authority
# ══════════════════════════════════════════════════════════════════════════════
def grant_classification_authority(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    authority_label: str,
    granted_by: str = "SYSTEM",
) -> EcoClassificationAuthority:
    """Grant one identity ECO break-classification authority in one tenant.

    Explicit and durable. P-1 forbids inferring this from ``User.role``,
    ``Team.head_user_id`` or any other existing role, so nothing here consults
    one. Does not commit.
    """
    if not (authority_label or "").strip():
        raise InvalidClassificationAuthority(
            "authority_label must be a non-empty label")
    if not (granted_by or "").strip():
        raise InvalidClassificationAuthority(
            "granted_by must be a non-empty label")

    existing = db.execute(
        select(EcoClassificationAuthority).where(
            EcoClassificationAuthority.organization_id == organization_id,
            EcoClassificationAuthority.user_id == user_id,
        )
    ).scalars().first()
    if existing is not None:
        raise InvalidClassificationAuthority(
            f"user {user_id} already holds classification authority in "
            f"organization {organization_id}")

    row = EcoClassificationAuthority(
        organization_id=organization_id, user_id=user_id,
        authority_label=authority_label, granted_by=granted_by)
    db.add(row)
    return row


def _live_authority(db: Session, *, organization_id: int,
                    authority_id: int) -> EcoClassificationAuthority:
    row = db.get(EcoClassificationAuthority, authority_id)
    if row is None or row.organization_id != organization_id:
        raise InvalidClassificationAuthority(
            f"authority {authority_id} does not exist in organization "
            f"{organization_id}")
    if not row.is_live:
        raise InvalidClassificationAuthority(
            f"authority {authority_id} was revoked at {row.revoked_at_utc}; a "
            f"revoked authority asserts nothing new")
    return row


# ══════════════════════════════════════════════════════════════════════════════
# Assertion
# ══════════════════════════════════════════════════════════════════════════════
def assert_break_classification(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date | datetime,
    source_ref: str,
    classification: str,
    evidence_ref: str,
    authority_id: int,
    evidence_detail: Optional[str] = None,
    asserted_at_utc: Optional[datetime] = None,
) -> BreakClassificationAssertion:
    """Append one immutable classification assertion.

    Re-asserting the IDENTICAL determination is idempotent: the live assertion
    is returned unchanged, because its content hash excludes the recording
    timestamp. A DIFFERENT determination appends a new version and supersedes
    the old one — never a rewrite.

    Does not commit; the caller owns the transaction boundary.
    """
    on = _as_date(work_date)

    if not is_valid_classification(classification):
        raise InvalidClassificationAssertion(
            f"classification {classification!r} is not one of "
            f"{sorted(CLASSIFICATION_RESULTS)}; a near-miss is refused rather "
            f"than corrected into a determination nobody made")
    value = (classification.value
             if isinstance(classification, ClassificationResult)
             else classification)

    if not (source_ref or "").strip():
        raise InvalidClassificationAssertion("source_ref must be non-empty")
    if not source_ref.startswith(BREAK_SOURCE_PREFIX):
        raise InvalidClassificationAssertion(
            f"source_ref {source_ref!r} does not name a break; expected the "
            f"canonical {BREAK_SOURCE_PREFIX!r} form the producer writes")
    if not (evidence_ref or "").strip():
        raise InvalidClassificationAssertion(
            "evidence_ref must be non-empty; P-1 requires the evidence "
            "reference be preserved so authoritative rules can attach later")

    authority = _live_authority(db, organization_id=organization_id,
                                authority_id=authority_id)

    digest = compute_assertion_content_hash(
        organization_id=organization_id, user_id=user_id, work_date=on,
        source_ref=source_ref, classification=value,
        evidence_ref=evidence_ref, authority_label=authority.authority_label)

    live = _live_assertion(db, organization_id=organization_id,
                           user_id=user_id, work_date=on, source_ref=source_ref)
    if live is not None and live.content_hash == digest:
        return live  # exact re-assertion: nothing new is written

    version = 1 if live is None else int(live.assertion_version) + 1
    row = BreakClassificationAssertion(
        organization_id=organization_id, user_id=user_id, work_date=on,
        source_ref=source_ref, classification=value,
        evidence_ref=evidence_ref, evidence_detail=evidence_detail,
        authority_id=authority.id, authority_label=authority.authority_label,
        assertion_version=version,
        supersedes=(live.id if live is not None else None),
        content_hash=digest)
    if asserted_at_utc is not None:
        row.asserted_at_utc = asserted_at_utc
    db.add(row)
    db.flush()

    if live is not None:
        live.superseded_by = row.id
        db.flush()
    return row


def _live_assertion(db: Session, *, organization_id: int, user_id: int,
                    work_date: date,
                    source_ref: str) -> Optional[BreakClassificationAssertion]:
    return db.execute(
        select(BreakClassificationAssertion).where(
            BreakClassificationAssertion.organization_id == organization_id,
            BreakClassificationAssertion.user_id == user_id,
            BreakClassificationAssertion.work_date == work_date,
            BreakClassificationAssertion.source_ref == source_ref,
            BreakClassificationAssertion.superseded_by.is_(None),
        ).order_by(BreakClassificationAssertion.assertion_version.desc())
    ).scalars().first()


# ══════════════════════════════════════════════════════════════════════════════
# Snapshot + resolution
# ══════════════════════════════════════════════════════════════════════════════
def get_or_create_classification_snapshot(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date,
    members: Sequence[Tuple[str, str, str]],
) -> ClassificationSnapshot:
    """Freeze this evaluation's classification input, content-addressed.

    Identical input reuses the existing row and writes nothing, which is what
    makes an unchanged replay idempotent. ``members`` are
    ``(source_ref, classification, assertion_content_hash)`` triples.
    """
    digest = compute_snapshot_hash(
        organization_id=organization_id, user_id=user_id,
        work_date=work_date, members=members)

    existing = db.execute(
        select(ClassificationSnapshot).where(
            ClassificationSnapshot.organization_id == organization_id,
            ClassificationSnapshot.snapshot_key == digest,
        )
    ).scalars().first()
    if existing is not None:
        return existing

    unresolved = any(m[1] == ClassificationResult.UNCLASSIFIABLE.value
                     for m in members)
    row = ClassificationSnapshot(
        snapshot_key=digest, content_hash=digest,
        organization_id=organization_id, user_id=user_id, work_date=work_date,
        assertion_count=len(members),
        has_unclassifiable=1 if unresolved else 0,
        members_json=canonical_members_json(members))
    db.add(row)
    db.flush()
    return row


def resolve_classification_for_day(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    work_date: date | datetime,
    ledger,
    expected_source_refs: Sequence[str],
) -> ClassificationResolution:
    """Resolve a SEALED day's break classification.

    ``expected_source_refs`` are the breaks this day's evidence contains,
    supplied by the caller that already read them — this function performs no
    producer work and makes no classification decision of its own. It reads
    assertions, checks them against the expected set, and reports.
    """
    on = _as_date(work_date)

    # ── post-seal preconditions (P-2) ───────────────────────────────────────
    if ledger is None:
        return ClassificationResolution(
            outcome=ClassificationOutcome.REFUSED,
            organization_id=organization_id, user_id=user_id, work_date=on,
            reason=REASON_NO_LEDGER_DAY,
            detail=f"no ledger day exists for {on.isoformat()}")
    if ledger.state == LedgerState.LEDGER_INTEGRITY_FAULT.value:
        return ClassificationResolution(
            outcome=ClassificationOutcome.REFUSED,
            organization_id=organization_id, user_id=user_id, work_date=on,
            reason=REASON_LEDGER_INTEGRITY_FAULT,
            detail=f"{on.isoformat()} holds inadmissible evidence")
    if ledger.state != LedgerState.SEALED.value:
        return ClassificationResolution(
            outcome=ClassificationOutcome.REFUSED,
            organization_id=organization_id, user_id=user_id, work_date=on,
            reason=REASON_LEDGER_NOT_SEALED,
            detail=(f"{on.isoformat()} is in ledger state {ledger.state!r}; "
                    f"classification consumes sealed evidence only"))

    for ref in expected_source_refs:
        if not str(ref).startswith(BREAK_SOURCE_PREFIX):
            return ClassificationResolution(
                outcome=ClassificationOutcome.REFUSED,
                organization_id=organization_id, user_id=user_id, work_date=on,
                reason=REASON_MALFORMED_SOURCE_REF,
                detail=f"{ref!r} is not a canonical break source reference")

    # ── the day's live assertions ───────────────────────────────────────────
    rows = db.execute(
        select(BreakClassificationAssertion).where(
            BreakClassificationAssertion.organization_id == organization_id,
            BreakClassificationAssertion.user_id == user_id,
            BreakClassificationAssertion.work_date == on,
            BreakClassificationAssertion.superseded_by.is_(None),
        ).order_by(BreakClassificationAssertion.source_ref,
                   BreakClassificationAssertion.id)
    ).scalars().all()

    by_ref: Dict[str, BreakClassificationAssertion] = {}
    for row in rows:
        if row.source_ref in by_ref:
            # Two LIVE assertions for one break. Refused, never resolved by
            # recency or majority — picking a winner would invent the legal
            # rule P-1 withholds.
            return ClassificationResolution(
                outcome=ClassificationOutcome.REFUSED,
                organization_id=organization_id, user_id=user_id, work_date=on,
                reason=REASON_CONFLICTING_ASSERTIONS,
                detail=(f"{row.source_ref} carries two live assertions "
                        f"({by_ref[row.source_ref].classification!r} and "
                        f"{row.classification!r})"))
        if not is_valid_classification(row.classification):
            return ClassificationResolution(
                outcome=ClassificationOutcome.REFUSED,
                organization_id=organization_id, user_id=user_id, work_date=on,
                reason=REASON_UNKNOWN_CLASSIFICATION,
                detail=f"{row.source_ref} carries {row.classification!r}")
        by_ref[row.source_ref] = row

    # ── the frozen members, and the determination ───────────────────────────
    members: List[Tuple[str, str, str]] = []
    unclassifiable: List[str] = []
    resolved: Dict[str, str] = {}

    for ref in sorted(set(str(r) for r in expected_source_refs)):
        row = by_ref.get(ref)
        if row is None:
            # No authoritative assertion at all. P-1 forbids falling back to
            # the worker-selected label, so the day defers.
            unclassifiable.append(ref)
            members.append((ref, ClassificationResult.UNCLASSIFIABLE.value, ""))
            continue
        members.append((ref, row.classification, row.content_hash))
        if row.classification == ClassificationResult.UNCLASSIFIABLE.value:
            unclassifiable.append(ref)
        else:
            resolved[ref] = row.classification

    snapshot = get_or_create_classification_snapshot(
        db, organization_id=organization_id, user_id=user_id,
        work_date=on, members=members)

    if unclassifiable:
        return ClassificationResolution(
            outcome=ClassificationOutcome.UNRESOLVED,
            organization_id=organization_id, user_id=user_id, work_date=on,
            snapshot_key=snapshot.snapshot_key,
            classifications=dict(resolved),
            unclassifiable_refs=tuple(sorted(unclassifiable)))

    return ClassificationResolution(
        outcome=ClassificationOutcome.RESOLVED,
        organization_id=organization_id, user_id=user_id, work_date=on,
        snapshot_key=snapshot.snapshot_key,
        classifications=dict(resolved))


__all__ = [
    "grant_classification_authority",
    "assert_break_classification",
    "get_or_create_classification_snapshot",
    "resolve_classification_for_day",
    "BREAK_SOURCE_PREFIX",
]
