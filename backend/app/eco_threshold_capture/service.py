"""
ECO threshold capture — superseding complete configuration snapshot.
====================================================================

The write path for the one configuration value the ECO threshold reader reads.

    capture_threshold(db, organization_id=..., threshold_minutes=...,
                      effective_from=..., actor=...)

NOT WIRED
---------
No route, service or scheduler calls this. There is no HTTP surface, no UI and
no scheduler entry: ruling 16 authorizes the capture mechanism, not its
exposure.

WHY A CAPTURE WRITES A WHOLE SNAPSHOT, NOT ONE ROW
--------------------------------------------------
``version_as_of`` selects exactly ONE configuration version, and every
configuration domain resolves its values from that version alone — the model
says so outright: "there is no query in this design that can span two". So a
version that governs a date must carry every domain in force on that date. A
version holding only a threshold would starve requirement resolution from its
effective date forward, and the day would stop being evaluable at all.

Therefore a threshold is not appended anywhere. A capture creates a NEW version
that carries the complete snapshot currently in force forward, unchanged, and
adds the threshold to it. Nothing existing is mutated: no value is updated, no
row is deleted, no ``content_hash`` is recomputed in place. The superseded
version stays byte-identical and keeps governing every date before the new one,
so a completed workweek re-derives to the same number forever (INV-8).

This is the idiom the versioning layer documents for itself — "a correction is a
NEW version with a later effective_from" — applied to a second domain.

WHAT IT REFUSES, AND WHY REFUSALS ARE VALUES
--------------------------------------------
Ruling G requires every refused attempt to be audited, so a refusal has to be a
thing the service produces rather than an exception it throws past the audit
write. Every refusal below writes its audit row before returning.

Some things are not refused because they cannot be expressed: there is no
per-Worker parameter, no tier parameter, no key parameter and no rank
parameter. Ruling 5's constraints are held by the signature, not by validation
— the technique the ledger used when it stopped accepting a caller-supplied
``workweek_key``.

IT NEVER INVENTS A BASELINE
---------------------------
If the organization has no configuration version to carry forward, the capture
is REFUSED. It does not fabricate a starting snapshot, and it does not write a
threshold-only version either: an organization whose requirement resolution
reported "nothing captured" would start reporting "captured, but no rank
answered", which sends an administrator hunting for a broken tier that was
never configured. Establishing an organization's first configuration version is
a Step 6 question, not an ECO one.

THE TWO BOUNDARY CHECKS (ruling H, and the version-attachment rule)
-------------------------------------------------------------------
Refusing a backdated ``effective_from`` is not sufficient on its own. A
threshold reaching a configuration version whose ``effective_from`` already lies
in the past reaches back into completed workweeks with no backdated date
anywhere in the request — the version's own effective date does the reaching.

So the boundary is checked twice, against two different things:

1. the requested ``effective_from``; and
2. the ``effective_from`` of the version actually about to receive the row.

Under this service the second is implied by the first, because the receiving
version is created here at exactly the requested date. The check is still made
explicitly, on the version object, before it is added to the session — a future
caller that selects a version by some other route must not be able to walk past
ruling H because this service happened to be the only door. It is a typed
refusal rather than an ``assert`` because ``python -O`` removes asserts.

WHY VERSION CREATION IS NOT ROUTED THROUGH ``capture_version()``
----------------------------------------------------------------
``config_versioning``'s ``capture_version`` writes requirement rows: its row
builder hardcodes the requirement key, and it cannot express an ECO threshold
row. Generalizing it to accept arbitrary configuration domains would undo the
separation the read path was built on.

So this service composes the same mechanics one level down — ``ConfigVersion``,
``ConfigValue`` and ``compute_content_hash`` — and inherits every constraint,
unique index and immutability listener unchanged. The digest is produced by the
canonical hashing function, not by a private copy, so a threshold version
verifies exactly like a requirement version.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config_versioning.models import (
    ConfigValue, ConfigVersion, compute_content_hash,
)
from app.eco_threshold import (
    DatabaseThresholdProvider,
    ECO_THRESHOLD_KEY,
    ECO_THRESHOLD_RANK,
    ECO_THRESHOLD_TIER,
    MAX_THRESHOLD_MINUTES,
    MIN_THRESHOLD_MINUTES,
    ThresholdOutcome,
    resolve_threshold,
)

from .boundary import current_open_workweek_start
from .outcomes import (
    AUTHORIZED_ROLES,
    ActorContext,
    CaptureOutcome,
    RefusalReason,
    ThresholdCaptureResult,
)

# ── Audit vocabulary ─────────────────────────────────────────────────────────
# Ratified by owner ruling 6. It describes a CONFIGURATION change and must never
# be mistaken for a threshold-crossing event: nothing here says REACHED,
# WITHDRAWN, PRIMARY, CROSS, SEQUENCE or NOTIF.
#
# Shape follows the repository's existing convention: an entity noun plus a
# PascalCase action, as in ``entity="Settings", action="ScopedSet"``, and the
# refusal action follows the ``SettingsLockedRejected`` precedent for auditing
# an attempt that was turned down.
AUDIT_ENTITY = "EcoThresholdConfig"
AUDIT_ACTION_CAPTURED = "ThresholdCaptured"
AUDIT_ACTION_REFUSED = "ThresholdCaptureRefused"

#: One stored configuration value, as a comparable tuple of every field that
#: carries meaning. ``id`` and ``config_version_id`` are excluded: they identify
#: the row and the version, not the configuration, and a carried row is a
#: different row in a different version by definition.
_ValueTuple = Tuple[Optional[int], str, int, str, Optional[int], Optional[str]]


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _value_tuple(row: ConfigValue) -> _ValueTuple:
    return (
        row.scope_user_id, row.tier, row.rank, row.key,
        row.value_minutes, row.source_ref,
    )


def _sorted_tuples(rows: Sequence[ConfigValue]) -> List[_ValueTuple]:
    """Stored values in a total, deterministic order.

    ``scope_user_id`` is nullable and ``None`` is not comparable with ``int``,
    so an organization-wide row beside a Worker-scoped one cannot be sorted
    naively. Ordered by the same sentinel the canonical hashing function uses,
    which puts organization-wide rows ahead of every real user id.
    """
    return sorted(
        (_value_tuple(row) for row in rows),
        key=lambda t: (t[0] if t[0] is not None else -1, t[1], t[2], t[3]),
    )


def _row_dict(row: ConfigValue) -> Dict[str, object]:
    """A stored value as the dict shape ``compute_content_hash`` consumes."""
    return {
        "scope_user_id": row.scope_user_id,
        "tier": row.tier,
        "rank": row.rank,
        "key": row.key,
        "value_minutes": row.value_minutes,
        "source_ref": row.source_ref,
    }


def _values_of(
    db: Session, version: ConfigVersion, *, fresh: bool = False
) -> List[ConfigValue]:
    """Every value frozen into one version.

    ``fresh=True`` re-reads from the database rather than trusting whatever the
    identity map already holds — which is the only way a verification read can
    prove what was actually stored. Targeted at these rows via
    ``populate_existing`` rather than expiring the whole session, because the
    session belongs to the caller and may hold unflushed work of its own.
    """
    stmt = (
        select(ConfigValue)
        .where(
            ConfigValue.config_version_id == version.id,
            ConfigValue.organization_id == version.organization_id,
        )
        .order_by(ConfigValue.key, ConfigValue.tier, ConfigValue.id)
    )
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    return list(db.execute(stmt).scalars().all())


def _version_at(db: Session, organization_id: int, on: date) -> Optional[ConfigVersion]:
    return db.execute(
        select(ConfigVersion).where(
            ConfigVersion.organization_id == organization_id,
            ConfigVersion.effective_from == on,
        )
    ).scalars().first()


def _version_with_key(
    db: Session, organization_id: int, version_key: str
) -> Optional[ConfigVersion]:
    return db.execute(
        select(ConfigVersion).where(
            ConfigVersion.organization_id == organization_id,
            ConfigVersion.version_key == version_key,
        )
    ).scalars().first()


def _version_key_for(effective_from: date) -> str:
    """The public identifier for a version starting on this date.

    Ratified by owner ruling 5. Derived rather than invented, and deliberately
    NOT domain-prefixed.

    ``version_key`` must be a stable public identifier that survives a database
    rebuild, and ``(organization_id, effective_from)`` is already unique — so a
    key derived from the effective date is exactly as unique as the constraint
    that already exists, introduces no new uniqueness semantics, and needs no
    generator, sequence or registry. No consumer interprets it: every lookup is
    an equality match, and ordering is always by ``effective_from``.

    An ``eco-`` prefix was considered and rejected: one version holds every
    configuration domain in force on its date, so a domain-specific key would
    misdescribe a version that carries anything else — which, under the
    complete-snapshot rule, every one of them does.
    """
    return effective_from.isoformat()


def _carry_forward(source_values: Sequence[ConfigValue]) -> List[Dict[str, object]]:
    """The source snapshot's applicable values, to be copied forward verbatim.

    Every field is copied unchanged, ``source_ref`` included: the carried row
    records where that value came from, and rewriting it would relabel a
    decision somebody else made.

    Rows bearing the ECO threshold key are the one exclusion, because the new
    threshold supersedes them — and because keeping both would collide on the
    ``uq_config_value_scope`` unique index. ALL of them are excluded, at every
    tier and scope, not just the canonical one: a mis-tiered or Worker-scoped
    threshold row is a recorded defect, and the superseding snapshot records the
    decision that was just made rather than carrying the defect forward. The
    superseded version keeps it forever, so nothing is erased.
    """
    return [
        _row_dict(row) for row in source_values if row.key != ECO_THRESHOLD_KEY
    ]


def _audit(
    db: Session,
    *,
    organization_id: int,
    actor: Optional[ActorContext],
    action: str,
    payload: dict,
) -> None:
    """Write the durable audit row (ruling G).

    Durable attribution goes in ``payload``, not only in ``actor_id``, and
    that is deliberate: ``AuditLog.actor_id`` is itself ``ondelete="SET NULL"``,
    so an FK-bound actor disappears from the audit trail at exactly the moment
    the trail matters. ``payload`` is JSON and holds no foreign key, so the
    label outlives the user row.

    Ruling 8 records the known limitation: ``audit_logs`` carries no uniqueness
    or idempotency key, so repeated refused attempts write repeated rows, and a
    refusal audit is transactional with the caller's transaction. Accepted for
    W5; a future hardening item, not solved here.
    """
    from database.models import AuditLog

    db.add(AuditLog(
        organization_id=organization_id,
        actor_id=(actor.user_id if actor else None),
        target_id=None,
        entity=AUDIT_ENTITY,
        action=action,
        payload=payload,
    ))
    db.flush()


def _refuse(
    db: Session,
    *,
    organization_id: int,
    actor: Optional[ActorContext],
    reason: RefusalReason,
    requested_minutes,
    effective_from: Optional[date],
    detail: Optional[str] = None,
    previous_minutes: Optional[int] = None,
    previous_version: Optional[str] = None,
) -> ThresholdCaptureResult:
    _audit(
        db,
        organization_id=organization_id,
        actor=actor,
        action=AUDIT_ACTION_REFUSED,
        payload={
            "key": ECO_THRESHOLD_KEY,
            "tier": ECO_THRESHOLD_TIER,
            "outcome": CaptureOutcome.REFUSED.value,
            "refusal_reason": reason.value,
            "detail": detail,
            "requested_threshold_minutes": requested_minutes,
            "requested_effective_from": (
                effective_from.isoformat() if effective_from else None),
            "previous_threshold_minutes": previous_minutes,
            "previous_config_version_id": previous_version,
            "actor_label": (actor.label if actor else None),
            "actor_user_id": (actor.user_id if actor else None),
            "actor_role": (actor.role if actor else None),
            "actor_is_system": (actor.is_system if actor else None),
            "attempted_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    return ThresholdCaptureResult(
        outcome=CaptureOutcome.REFUSED,
        organization_id=organization_id,
        refusal_reason=reason,
        effective_from=effective_from,
        detail=detail,
        previous_threshold_minutes=previous_minutes,
        previous_config_version_id=previous_version,
    )


def capture_threshold(
    db: Session,
    *,
    organization_id: int,
    threshold_minutes,
    effective_from: date | datetime,
    actor: Optional[ActorContext],
) -> ThresholdCaptureResult:
    """Capture this organization's ECO weekly threshold.

    On success this writes ONE new ``ConfigVersion`` carrying the complete
    configuration snapshot currently in force plus the new threshold. The
    superseded version is not read for mutation, appended to, or deleted.

    The caller owns the transaction boundary — this flushes but never commits,
    matching the Step 6 capture path. Two consequences worth stating: a refusal
    audit row is written into the caller's session, so a caller that rolls back
    after a refusal discards the audit with it; and the two fail-closed paths
    that cannot leave a half-written snapshot standing (a lost unique-constraint
    race, and a snapshot that did not verify) roll the session back, which
    discards the caller's own uncommitted work alongside ours.
    """
    requested = threshold_minutes

    # ── 1. Actor and authority (rulings C, D, E) ─────────────────────────────
    if actor is None:
        return _refuse(
            db, organization_id=organization_id, actor=None,
            reason=RefusalReason.ACTOR_REQUIRED,
            requested_minutes=requested, effective_from=None,
            detail="no actor context supplied",
        )

    if not actor.is_system:
        # Role only. No capability is consulted: ruling D keeps a plan
        # entitlement from being able to prevent an organization recording its
        # own configuration decision.
        if actor.role not in AUTHORIZED_ROLES:
            return _refuse(
                db, organization_id=organization_id, actor=actor,
                reason=RefusalReason.ACTOR_NOT_AUTHORIZED,
                requested_minutes=requested, effective_from=None,
                detail=f"role {actor.role!r} may not capture an ECO threshold",
            )
        if (actor.organization_id is not None
                and int(actor.organization_id) != int(organization_id)):
            return _refuse(
                db, organization_id=organization_id, actor=actor,
                reason=RefusalReason.WRONG_ORGANIZATION,
                requested_minutes=requested, effective_from=None,
                detail="actor belongs to a different organization",
            )

    # ── 2. The value (ruling 5) ──────────────────────────────────────────────
    # bool is an int subclass and would otherwise slip through as 0 or 1.
    if isinstance(requested, bool) or not isinstance(requested, int):
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.VALUE_NOT_AN_INTEGER,
            requested_minutes=(
                requested if isinstance(requested, (int, float, str)) else None),
            effective_from=None,
            detail=f"threshold_minutes must be an int, got {type(requested).__name__}",
        )
    if not (MIN_THRESHOLD_MINUTES <= requested <= MAX_THRESHOLD_MINUTES):
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.VALUE_OUT_OF_RANGE,
            requested_minutes=requested, effective_from=None,
            detail=(
                f"threshold must be {MIN_THRESHOLD_MINUTES}..{MAX_THRESHOLD_MINUTES} "
                f"minutes"
            ),
        )

    on = _as_date(effective_from)

    # What was in force for that date beforehand — asked through the reader, so
    # the audit's "before" is exactly what the reader would have answered.
    previous = resolve_threshold(
        organization_id=organization_id,
        as_of_workweek_start=on,
        provider=DatabaseThresholdProvider(db),
    )
    previous_minutes = (
        previous.threshold_minutes
        if previous.outcome is ThresholdOutcome.RESOLVED else None
    )
    previous_version = previous.config_version_id

    # ── 3. Ruling H — the requested effective date ───────────────────────────
    boundary = current_open_workweek_start(db, organization_id=organization_id)
    if on < boundary:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK,
            requested_minutes=requested, effective_from=on,
            previous_minutes=previous_minutes, previous_version=previous_version,
            detail=(
                f"effective_from {on.isoformat()} precedes the current open "
                f"workweek start {boundary.isoformat()}; completed workweeks "
                f"are immutable"
            ),
        )

    # ── 4. Same-date collision (ruling 2) — permanent behaviour ──────────────
    # ``(organization_id, effective_from)`` is unique, so a second version
    # cannot start on that date, and the existing one cannot be appended to or
    # mutated. The administrator picks a later eligible date.
    occupied = _version_at(db, organization_id, on)
    if occupied is not None:
        already = db.execute(
            select(ConfigValue).where(
                ConfigValue.config_version_id == occupied.id,
                ConfigValue.organization_id == organization_id,
                ConfigValue.key == ECO_THRESHOLD_KEY,
            )
        ).scalars().first()

        if already is not None:
            return _refuse(
                db, organization_id=organization_id, actor=actor,
                reason=RefusalReason.THRESHOLD_ALREADY_CAPTURED,
                requested_minutes=requested, effective_from=on,
                previous_minutes=previous_minutes,
                previous_version=previous_version,
                detail=(
                    f"a threshold is already recorded effective "
                    f"{on.isoformat()}; capture a LATER effective date instead "
                    f"of rewriting an immutable version"
                ),
            )

        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.EXISTING_VERSION_CANNOT_RECEIVE_THRESHOLD,
            requested_minutes=requested, effective_from=on,
            previous_minutes=previous_minutes, previous_version=previous_version,
            detail=(
                f"configuration version {occupied.version_key!r} already starts "
                f"on {on.isoformat()}; a version is immutable and cannot receive "
                f"a new value, so capture a LATER effective date instead"
            ),
        )

    # ── 5. Version-key collision, independently of the date (ruling 5) ───────
    # ``version_key`` is caller-supplied elsewhere in Step 6, so another writer
    # may already hold this date-shaped key on a version effective on a
    # different date. Detected here so the unique constraint's IntegrityError
    # is not misreported as a concurrent capture, which it is not.
    version_key = _version_key_for(on)
    key_clash = _version_with_key(db, organization_id, version_key)
    if key_clash is not None:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.VERSION_KEY_ALREADY_IN_USE,
            requested_minutes=requested, effective_from=on,
            previous_minutes=previous_minutes, previous_version=previous_version,
            detail=(
                f"version key {version_key!r} is already held by a "
                f"configuration version effective "
                f"{key_clash.effective_from.isoformat()}"
            ),
        )

    # ── 6. The snapshot being superseded ─────────────────────────────────────
    source_ref_obj = db.execute(
        select(ConfigVersion)
        .where(
            ConfigVersion.organization_id == organization_id,
            ConfigVersion.effective_from <= on,
        )
        .order_by(ConfigVersion.effective_from.desc(), ConfigVersion.id.desc())
        .limit(1)
    ).scalars().first()

    if source_ref_obj is None:
        # No baseline is invented here (ruling 9, and ruling A's prohibition on
        # defaults). Establishing an organization's first configuration version
        # is a Step 6 capture-path question.
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.NO_CONFIGURATION_TO_CARRY_FORWARD,
            requested_minutes=requested, effective_from=on,
            previous_minutes=previous_minutes, previous_version=previous_version,
            detail=(
                f"organization {organization_id} has no configuration version "
                f"effective on or before {on.isoformat()} to carry forward; a "
                f"threshold-only version would leave every other configuration "
                f"domain unresolved"
            ),
        )

    source_values = _values_of(db, source_ref_obj)
    # Snapshotted as plain values BEFORE the write, so the immutability check
    # afterwards compares against something the write cannot have altered.
    source_key = source_ref_obj.version_key
    source_date = source_ref_obj.effective_from
    source_hash = source_ref_obj.content_hash
    source_expected = _sorted_tuples(source_values)
    carried_expected = [t for t in source_expected if t[3] != ECO_THRESHOLD_KEY]

    carried = _carry_forward(source_values)
    threshold_row = {
        "scope_user_id": None,
        "tier": ECO_THRESHOLD_TIER,
        "rank": ECO_THRESHOLD_RANK,
        "key": ECO_THRESHOLD_KEY,
        "value_minutes": requested,
        "source_ref": None,
    }
    complete_snapshot = carried + [threshold_row]

    # ── 7. The new version — hashed ONCE over its complete contents ──────────
    version = ConfigVersion(
        organization_id=organization_id,
        version_key=version_key,
        effective_from=on,
        captured_by_user_id=actor.user_id,
        captured_by_label=actor.label,
        content_hash=compute_content_hash(
            organization_id=organization_id,
            version_key=version_key,
            effective_from=on,
            values=complete_snapshot,
        ),
    )

    # The version-attachment boundary, on the object that will receive the row.
    if version.effective_from < boundary:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.VERSION_PREDATES_OPEN_WORKWEEK,
            requested_minutes=requested, effective_from=on,
            previous_minutes=previous_minutes, previous_version=previous_version,
            detail=(
                f"the configuration version about to receive the threshold is "
                f"effective {version.effective_from.isoformat()}, before the "
                f"current open workweek start {boundary.isoformat()}"
            ),
        )

    try:
        db.add(version)
        # Assigns the surrogate id the value rows need, without committing.
        db.flush()

        # ── 8. Carry the snapshot forward, then PROVE it carried ─────────────
        for row in carried:
            db.add(ConfigValue(
                config_version_id=version.id,
                organization_id=organization_id,
                **row,
            ))
        db.flush()

        carried_persisted = _sorted_tuples(_values_of(db, version, fresh=True))
        if carried_persisted != carried_expected:
            return _fail_verification(
                db, organization_id=organization_id, actor=actor,
                requested=requested, on=on,
                previous_minutes=previous_minutes,
                previous_version=previous_version,
                detail=(
                    f"carried configuration does not match version "
                    f"{source_key!r}: "
                    f"{len(set(carried_persisted) & set(carried_expected))} of "
                    f"{len(carried_expected)} values match, "
                    f"{len(carried_persisted)} carried"
                ),
            )

        # ── 9. Only now the new threshold ────────────────────────────────────
        db.add(ConfigValue(
            config_version_id=version.id,
            organization_id=organization_id,
            **threshold_row,
        ))
        db.flush()
    except IntegrityError:
        # The unique constraints on (organization_id, effective_from) and
        # (organization_id, version_key) are the net behind the pre-checks
        # above. A racing capture lands here; fail closed rather than retry, so
        # two administrators cannot interleave into one confused version.
        db.rollback()
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=RefusalReason.CONCURRENT_CAPTURE,
            requested_minutes=requested, effective_from=on,
            detail=(
                f"another capture claimed effective {on.isoformat()} for this "
                f"organization; re-read and re-decide"
            ),
        )

    # ── 10. The stored digest must describe the stored snapshot ──────────────
    stored = _values_of(db, version, fresh=True)
    recomputed = compute_content_hash(
        organization_id=organization_id,
        version_key=version_key,
        effective_from=on,
        values=[_row_dict(r) for r in stored],
    )
    if recomputed != version.content_hash:
        return _fail_verification(
            db, organization_id=organization_id, actor=actor,
            requested=requested, on=on,
            previous_minutes=previous_minutes, previous_version=previous_version,
            detail=(
                "the stored content_hash does not describe the stored values "
                "of the new configuration version"
            ),
        )

    # ── 11. And the superseded version must be exactly as it was ─────────────
    source_now = db.execute(
        select(ConfigVersion)
        .where(ConfigVersion.id == source_ref_obj.id)
        .execution_options(populate_existing=True)
    ).scalars().first()
    if (
        source_now is None
        or source_now.content_hash != source_hash
        or source_now.effective_from != source_date
        or _sorted_tuples(_values_of(db, source_now, fresh=True)) != source_expected
    ):
        return _fail_verification(
            db, organization_id=organization_id, actor=actor,
            requested=requested, on=on,
            previous_minutes=previous_minutes, previous_version=previous_version,
            detail=f"superseded version {source_key!r} was altered by the capture",
        )

    _audit(
        db,
        organization_id=organization_id,
        actor=actor,
        action=AUDIT_ACTION_CAPTURED,
        payload={
            "key": ECO_THRESHOLD_KEY,
            "tier": ECO_THRESHOLD_TIER,
            "rank": ECO_THRESHOLD_RANK,
            "scope": "ORGANIZATION",
            "outcome": CaptureOutcome.CAPTURED.value,
            "threshold_minutes": requested,
            "previous_threshold_minutes": previous_minutes,
            "previous_config_version_id": previous_version,
            "config_version_id": version_key,
            "superseded_config_version_id": source_key,
            "superseded_effective_from": source_date.isoformat(),
            "carried_value_count": len(carried),
            "effective_from": on.isoformat(),
            "captured_at_utc": (
                version.captured_at_utc.isoformat()
                if version.captured_at_utc else None),
            "content_hash": version.content_hash,
            "actor_label": actor.label,
            "actor_user_id": actor.user_id,
            "actor_role": actor.role,
            "actor_is_system": actor.is_system,
            "open_workweek_start": boundary.isoformat(),
        },
    )

    return ThresholdCaptureResult(
        outcome=CaptureOutcome.CAPTURED,
        organization_id=organization_id,
        threshold_minutes=requested,
        effective_from=on,
        config_version_id=version_key,
        superseded_config_version_id=source_key,
        carried_value_count=len(carried),
        previous_threshold_minutes=previous_minutes,
        previous_config_version_id=previous_version,
    )


def _fail_verification(
    db: Session,
    *,
    organization_id: int,
    actor: ActorContext,
    requested: int,
    on: date,
    previous_minutes: Optional[int],
    previous_version: Optional[str],
    detail: str,
) -> ThresholdCaptureResult:
    """Abandon a snapshot that did not verify.

    Defence in depth, and deliberately not an exception: a snapshot that failed
    to carry forward exactly would be a fabricated configuration history, so it
    is rolled back and audited rather than allowed to stand or raised past the
    audit write. The rollback discards the caller's uncommitted work with ours —
    the alternative is leaving a version whose contents nobody verified.
    """
    db.rollback()
    return _refuse(
        db, organization_id=organization_id, actor=actor,
        reason=RefusalReason.SNAPSHOT_VERIFICATION_FAILED,
        requested_minutes=requested, effective_from=on,
        previous_minutes=previous_minutes, previous_version=previous_version,
        detail=detail,
    )


__all__ = [
    "capture_threshold",
    "AUDIT_ENTITY",
    "AUDIT_ACTION_CAPTURED",
    "AUDIT_ACTION_REFUSED",
]
