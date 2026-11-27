"""
An organization's FIRST configuration version.
==============================================

    capture_initial_configuration(
        db, organization_id=..., required_minutes=..., effective_from=...,
        actor=..., threshold_minutes=None)

NOT WIRED
---------
No route, service or scheduler calls this. There is no HTTP surface and no UI:
owner ruling 8 authorizes the mechanism, not its exposure.

WHY THIS EXISTS SEPARATELY FROM ``capture_version()``
-----------------------------------------------------
Step 6's generic writer accepts an empty candidate list, consults no role,
never sets a durable actor label, accepts any effective date past or future,
and raises instead of returning an auditable outcome. Every one of those
conflicts with a first-capture ruling, and fixing them there would change
generic Step 6 behaviour for all 39 of its call sites. So this service composes
the low-level primitives — ``ConfigVersion``, ``ConfigValue`` and
``compute_content_hash`` — and inherits every constraint, unique index and
immutability listener unchanged, exactly as the threshold capture path does.

WHAT IT REFUSES TO INVENT
-------------------------
The administrator's number is the authoritative configuration fact. Nothing
here reads a live setting, an employment-type policy, a schedule default, a
risk threshold or the 8-hour runtime fallback. A value the product merely fell
back to is not a value an organization chose, and capturing one would fabricate
the very evidence this layer exists to make trustworthy.

FIRST MEANS FIRST
-----------------
If the organization already holds any configuration version, the attempt is
refused and nothing is written. This service never becomes the subsequent-
capture path, and it never invokes the complete-snapshot supersession
mechanism — that belongs to threshold capture.

TWO SHAPES, AND NO OTHERS
-------------------------
``required_minutes`` alone, or ``required_minutes`` with an ECO threshold when
the administrator supplied one in the same action. An empty snapshot and a
threshold-only snapshot are not refused, they are unexpressible:
``required_minutes`` is a mandatory parameter with no default.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config_versioning.adapter import REQUIREMENT_KEY
from app.config_versioning.models import (
    ConfigValue, ConfigVersion, compute_content_hash,
)
from app.eco_threshold import (
    ECO_THRESHOLD_KEY,
    ECO_THRESHOLD_RANK,
    ECO_THRESHOLD_TIER,
    MAX_THRESHOLD_MINUTES,
    MIN_THRESHOLD_MINUTES,
)
# Owner ruling 7: the already-approved boundary helper and actor type are
# reused rather than reimplemented. No second workweek calculation exists, and
# nothing here reads a raw schedule setting. The dependency direction — a
# Step 6 concern importing from an ECO package — is recorded for cleanup in a
# later increment, not widened here.
from app.eco_threshold_capture.boundary import current_open_workweek_start
from app.eco_threshold_capture.outcomes import AUTHORIZED_ROLES, ActorContext

from .outcomes import (
    MAX_REQUIRED_MINUTES,
    MIN_REQUIRED_MINUTES,
    REQUIREMENT_RANK,
    REQUIREMENT_TIER,
    InitialCaptureOutcome,
    InitialCaptureRefusal,
    InitialCaptureResult,
    SnapshotShape,
)

# ── Audit vocabulary ─────────────────────────────────────────────────────────
# Ratified by owner ruling 2. Configuration audit, not an ECO event: nothing
# here says REACHED, WITHDRAWN, PRIMARY, CROSS, SEQUENCE or NOTIF. Shape
# follows the repository convention of an entity noun plus a PascalCase action.
AUDIT_ENTITY = "ConfigurationVersion"
AUDIT_ACTION_CAPTURED = "InitialConfigurationCaptured"
AUDIT_ACTION_REFUSED = "InitialConfigurationCaptureRefused"

#: A stored value as a comparable tuple of everything that carries meaning.
_ValueTuple = Tuple[Optional[int], str, int, str, Optional[int], Optional[str]]


def _value_tuple(row: ConfigValue) -> _ValueTuple:
    return (
        row.scope_user_id, row.tier, row.rank, row.key,
        row.value_minutes, row.source_ref,
    )


def _dict_tuple(row: Dict[str, object]) -> _ValueTuple:
    return (
        row["scope_user_id"], row["tier"], row["rank"], row["key"],
        row["value_minutes"], row["source_ref"],
    )


def _sort_key(t: _ValueTuple):
    # ``scope_user_id`` is nullable and None is not comparable with int; -1 is
    # the same sentinel the canonical hashing function uses.
    return (t[0] if t[0] is not None else -1, t[1], t[2], t[3])


def _is_int(value) -> bool:
    """``bool`` is an int subclass and must never pass as a minute count."""
    return isinstance(value, int) and not isinstance(value, bool)


def _any_version(db: Session, organization_id: int) -> Optional[ConfigVersion]:
    """This organization's earliest configuration version, if it has one.

    A named probe rather than an inline query so a test can blind it and prove
    that the guards behind it actually fire — which is what a lost race does.
    """
    return db.execute(
        select(ConfigVersion)
        .where(ConfigVersion.organization_id == organization_id)
        .order_by(ConfigVersion.effective_from, ConfigVersion.id)
        .limit(1)
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


def _version_at(
    db: Session, organization_id: int, on: date
) -> Optional[ConfigVersion]:
    return db.execute(
        select(ConfigVersion).where(
            ConfigVersion.organization_id == organization_id,
            ConfigVersion.effective_from == on,
        )
    ).scalars().first()


def _version_key_for(effective_from: date) -> str:
    """The public identifier for a version starting on this date.

    The same derivation the threshold capture path uses (owner ruling W5-4), so
    both writers produce one consistent scheme: ``(organization_id,
    effective_from)`` is already unique, so a date-derived key is exactly as
    unique as the constraint that exists and needs no generator or registry.
    """
    return effective_from.isoformat()


def _audit(
    db: Session,
    *,
    organization_id: Optional[int],
    actor: Optional[ActorContext],
    action: str,
    payload: dict,
) -> None:
    """Write the durable audit row.

    Durable attribution goes in ``payload`` as well as ``actor_id``, because
    ``AuditLog.actor_id`` is ``ondelete="SET NULL"`` — an FK-bound actor
    vanishes from the trail at exactly the moment the trail matters. The typed
    refusal outcome, not the detail string, is the authoritative
    classification; ``detail`` is prose for an administrator.
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
    reason: InitialCaptureRefusal,
    requested_minutes,
    requested_threshold,
    effective_from,
    detail: str,
) -> InitialCaptureResult:
    _audit(
        db,
        organization_id=organization_id,
        actor=actor,
        action=AUDIT_ACTION_REFUSED,
        payload={
            "capture": "INITIAL",
            "outcome": InitialCaptureOutcome.REFUSED.value,
            "refusal_reason": reason.value,
            "detail": detail,
            "requirement_key": REQUIREMENT_KEY,
            "attempted_required_minutes": (
                requested_minutes if isinstance(
                    requested_minutes, (int, float, str)) else None),
            "attempted_threshold_minutes": (
                requested_threshold if isinstance(
                    requested_threshold, (int, float, str)) else None),
            "attempted_effective_from": (
                effective_from.isoformat()
                if isinstance(effective_from, date) else None),
            "actor_label": (actor.label if actor else None),
            "actor_user_id": (actor.user_id if actor else None),
            "actor_role": (actor.role if actor else None),
            "actor_is_system": (actor.is_system if actor else None),
            "attempted_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    return InitialCaptureResult(
        outcome=InitialCaptureOutcome.REFUSED,
        organization_id=organization_id,
        refusal_reason=reason,
        effective_from=effective_from if isinstance(effective_from, date) else None,
        detail=detail,
    )


def capture_initial_configuration(
    db: Session,
    *,
    organization_id: int,
    required_minutes,
    effective_from,
    actor: Optional[ActorContext],
    threshold_minutes=None,
) -> InitialCaptureResult:
    """Establish this organization's first immutable configuration version.

    The caller owns the transaction boundary: this flushes but never commits,
    matching both existing capture paths. A refusal audit row is written into
    the caller's session, so a caller that rolls back after a refusal discards
    the audit with it. The two fail-closed paths that must not leave a
    half-written baseline standing — a lost unique-constraint race, and a
    snapshot that did not verify — roll the session back, which discards the
    caller's own uncommitted work alongside ours.
    """
    # ── 1. Actor and authority (owner decision 1) ────────────────────────────
    if actor is None:
        return _refuse(
            db, organization_id=organization_id, actor=None,
            reason=InitialCaptureRefusal.ACTOR_REQUIRED,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=effective_from,
            detail="no actor context supplied",
        )

    # No SYSTEM exemption, deliberately. Decision 1 prohibits creation by any
    # implicit background process, and a SYSTEM actor is one; its role is None,
    # so it falls here without needing a branch of its own.
    if actor.role not in AUTHORIZED_ROLES:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.ACTOR_NOT_AUTHORIZED,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=effective_from,
            detail=(
                f"role {actor.role!r} may not author an organization's initial "
                f"configuration"
            ),
        )

    # Stricter than the threshold path: an actor with no organization at all is
    # refused, not tolerated. This version fixes a tenant's historical floor.
    if (actor.organization_id is None
            or int(actor.organization_id) != int(organization_id)):
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.WRONG_ORGANIZATION,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=effective_from,
            detail=(
                "the actor does not belong to the organization being configured"
            ),
        )

    # ── 2. The requirement (mandatory) ───────────────────────────────────────
    if not _is_int(required_minutes):
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.REQUIRED_MINUTES_NOT_AN_INTEGER,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=effective_from,
            detail=(
                f"required_minutes must be an int, got "
                f"{type(required_minutes).__name__}"
            ),
        )
    if not (MIN_REQUIRED_MINUTES <= required_minutes <= MAX_REQUIRED_MINUTES):
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.REQUIRED_MINUTES_OUT_OF_RANGE,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=effective_from,
            detail=(
                f"required_minutes must be {MIN_REQUIRED_MINUTES}.."
                f"{MAX_REQUIRED_MINUTES} minutes"
            ),
        )

    # ── 3. The threshold (optional; absent is not invalid) ───────────────────
    if threshold_minutes is not None:
        if not _is_int(threshold_minutes):
            return _refuse(
                db, organization_id=organization_id, actor=actor,
                reason=InitialCaptureRefusal.THRESHOLD_NOT_AN_INTEGER,
                requested_minutes=required_minutes,
                requested_threshold=threshold_minutes,
                effective_from=effective_from,
                detail=(
                    f"threshold_minutes must be an int, got "
                    f"{type(threshold_minutes).__name__}"
                ),
            )
        if not (MIN_THRESHOLD_MINUTES <= threshold_minutes
                <= MAX_THRESHOLD_MINUTES):
            return _refuse(
                db, organization_id=organization_id, actor=actor,
                reason=InitialCaptureRefusal.THRESHOLD_OUT_OF_RANGE,
                requested_minutes=required_minutes,
                requested_threshold=threshold_minutes,
                effective_from=effective_from,
                detail=(
                    f"threshold must be {MIN_THRESHOLD_MINUTES}.."
                    f"{MAX_THRESHOLD_MINUTES} minutes"
                ),
            )

    # ── 4. The effective date, explicitly supplied (owner decision 2) ────────
    # ``bool`` and ``datetime`` are both handled: a bool is not a date, and a
    # datetime narrows to its date rather than being refused.
    if isinstance(effective_from, datetime):
        on = effective_from.date()
    elif isinstance(effective_from, date) and not isinstance(effective_from, bool):
        on = effective_from
    else:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.EFFECTIVE_FROM_REQUIRED,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=None,
            detail=(
                f"effective_from must be an explicit date, got "
                f"{type(effective_from).__name__}; it is never inferred"
            ),
        )

    boundary = current_open_workweek_start(db, organization_id=organization_id)
    if on < boundary:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=on,
            detail=(
                f"effective_from {on.isoformat()} precedes the current open "
                f"workweek start {boundary.isoformat()}; a backdated first "
                f"version would assert configuration for completed workweeks"
            ),
        )

    # ── 5. First means first ─────────────────────────────────────────────────
    existing = _any_version(db, organization_id)
    if existing is not None:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.ORGANIZATION_ALREADY_CONFIGURED,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=on,
            detail=(
                f"organization {organization_id} already has configuration "
                f"version {existing.version_key!r} effective "
                f"{existing.effective_from.isoformat()}; this path establishes "
                f"a FIRST version only"
            ),
        )

    version_key = _version_key_for(on)

    # Defence in depth. Both are unreachable while the check above stands — a
    # key or a date can only be taken if a version exists — and both are kept
    # so neither guard depends on the ordering of the one before it.
    key_clash = _version_with_key(db, organization_id, version_key)
    if key_clash is not None:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.VERSION_KEY_ALREADY_IN_USE,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=on,
            detail=f"version key {version_key!r} is already held",
        )

    date_clash = _version_at(db, organization_id, on)
    if date_clash is not None:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.EFFECTIVE_DATE_ALREADY_CLAIMED,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=on,
            detail=(
                f"a configuration version already starts on {on.isoformat()}"
            ),
        )

    # ── 6. The snapshot ──────────────────────────────────────────────────────
    # ``source_ref`` is NULL on both rows, and deliberately so: for a first
    # capture the provenance IS the administrative action, which is recorded in
    # ``captured_by_label`` and the audit row. Inventing a provenance string
    # here would put a label in the digest that describes nothing.
    rows: List[Dict[str, object]] = [{
        "scope_user_id": None,
        "tier": REQUIREMENT_TIER,
        "rank": REQUIREMENT_RANK,
        "key": REQUIREMENT_KEY,
        "value_minutes": required_minutes,
        "source_ref": None,
    }]
    shape = SnapshotShape.REQUIREMENT_ONLY
    if threshold_minutes is not None:
        rows.append({
            "scope_user_id": None,
            "tier": ECO_THRESHOLD_TIER,
            "rank": ECO_THRESHOLD_RANK,
            "key": ECO_THRESHOLD_KEY,
            "value_minutes": threshold_minutes,
            "source_ref": None,
        })
        shape = SnapshotShape.REQUIREMENT_AND_THRESHOLD

    content_hash = compute_content_hash(
        organization_id=organization_id,
        version_key=version_key,
        effective_from=on,
        values=rows,
    )

    version = ConfigVersion(
        organization_id=organization_id,
        version_key=version_key,
        effective_from=on,
        captured_by_user_id=actor.user_id,
        captured_by_label=actor.label,
        content_hash=content_hash,
    )

    # The boundary, re-checked on the object that will hold the rows. A typed
    # refusal rather than an ``assert``, which ``python -O`` removes.
    if version.effective_from < boundary:
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.EFFECTIVE_FROM_BEFORE_OPEN_WORKWEEK,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=on,
            detail=(
                f"the configuration version about to be written is effective "
                f"{version.effective_from.isoformat()}, before the current "
                f"open workweek start {boundary.isoformat()}"
            ),
        )

    try:
        db.add(version)
        db.flush()
        for row in rows:
            db.add(ConfigValue(
                config_version_id=version.id,
                organization_id=organization_id,
                **row,
            ))
        db.flush()
    except IntegrityError:
        db.rollback()
        return _refuse(
            db, organization_id=organization_id, actor=actor,
            reason=InitialCaptureRefusal.CONCURRENT_CAPTURE,
            requested_minutes=required_minutes,
            requested_threshold=threshold_minutes,
            effective_from=on,
            detail=(
                f"another writer claimed the first configuration version for "
                f"organization {organization_id}; re-read and re-decide"
            ),
        )

    # ── 7. Verify what was actually stored ───────────────────────────────────
    stored = list(db.execute(
        select(ConfigValue)
        .where(
            ConfigValue.config_version_id == version.id,
            ConfigValue.organization_id == organization_id,
        )
        .order_by(ConfigValue.key, ConfigValue.tier, ConfigValue.id)
        .execution_options(populate_existing=True)
    ).scalars().all())

    intended = sorted((_dict_tuple(r) for r in rows), key=_sort_key)
    persisted = sorted((_value_tuple(r) for r in stored), key=_sort_key)
    if persisted != intended:
        matched = len(set(persisted) & set(intended))
        return _fail_verification(
            db, organization_id=organization_id, actor=actor,
            required_minutes=required_minutes,
            threshold_minutes=threshold_minutes, on=on,
            detail=(
                f"stored configuration does not match what was supplied: "
                f"{matched} of {len(intended)} values match, "
                f"{len(persisted)} stored"
            ),
        )

    recomputed = compute_content_hash(
        organization_id=organization_id,
        version_key=version_key,
        effective_from=on,
        values=[{
            "scope_user_id": r.scope_user_id, "tier": r.tier, "rank": r.rank,
            "key": r.key, "value_minutes": r.value_minutes,
            "source_ref": r.source_ref,
        } for r in stored],
    )
    if recomputed != version.content_hash:
        return _fail_verification(
            db, organization_id=organization_id, actor=actor,
            required_minutes=required_minutes,
            threshold_minutes=threshold_minutes, on=on,
            detail=(
                "the stored content_hash does not describe the stored values"
            ),
        )

    # Exactly one version now exists for this organization. A first capture
    # that produced two would mean the guard above had let a second writer in.
    count = len(db.execute(
        select(ConfigVersion.id).where(
            ConfigVersion.organization_id == organization_id)
    ).scalars().all())
    if count != 1:
        return _fail_verification(
            db, organization_id=organization_id, actor=actor,
            required_minutes=required_minutes,
            threshold_minutes=threshold_minutes, on=on,
            detail=(
                f"organization {organization_id} holds {count} configuration "
                f"versions after its first capture"
            ),
        )

    _audit(
        db,
        organization_id=organization_id,
        actor=actor,
        action=AUDIT_ACTION_CAPTURED,
        payload={
            "capture": "INITIAL",
            "outcome": InitialCaptureOutcome.CAPTURED.value,
            "snapshot_shape": shape.value,
            "value_count": len(rows),
            "effective_from": on.isoformat(),
            "open_workweek_start": boundary.isoformat(),
            "config_version_id": version_key,
            "content_hash": version.content_hash,
            "captured_at_utc": (
                version.captured_at_utc.isoformat()
                if version.captured_at_utc else None),
            "required_minutes": required_minutes,
            "required_minutes_key": REQUIREMENT_KEY,
            "required_minutes_tier": REQUIREMENT_TIER,
            "required_minutes_rank": REQUIREMENT_RANK,
            "threshold_supplied": threshold_minutes is not None,
            "threshold_minutes": threshold_minutes,
            "threshold_key": (
                ECO_THRESHOLD_KEY if threshold_minutes is not None else None),
            "threshold_tier": (
                ECO_THRESHOLD_TIER if threshold_minutes is not None else None),
            "threshold_rank": (
                ECO_THRESHOLD_RANK if threshold_minutes is not None else None),
            "scope": "ORGANIZATION",
            "actor_label": actor.label,
            "actor_user_id": actor.user_id,
            "actor_role": actor.role,
            "actor_is_system": actor.is_system,
        },
    )

    return InitialCaptureResult(
        outcome=InitialCaptureOutcome.CAPTURED,
        organization_id=organization_id,
        shape=shape,
        required_minutes=required_minutes,
        threshold_minutes=threshold_minutes,
        effective_from=on,
        config_version_id=version_key,
        content_hash=version.content_hash,
        value_count=len(rows),
    )


def _fail_verification(
    db: Session,
    *,
    organization_id: int,
    actor: ActorContext,
    required_minutes: int,
    threshold_minutes: Optional[int],
    on: date,
    detail: str,
) -> InitialCaptureResult:
    """Abandon a baseline that did not verify.

    Rolled back and audited rather than raised past the audit write: a first
    version whose contents nobody verified would become an organization's
    permanent historical floor.
    """
    db.rollback()
    return _refuse(
        db, organization_id=organization_id, actor=actor,
        reason=InitialCaptureRefusal.SNAPSHOT_VERIFICATION_FAILED,
        requested_minutes=required_minutes,
        requested_threshold=threshold_minutes,
        effective_from=on,
        detail=detail,
    )


__all__ = [
    "capture_initial_configuration",
    "AUDIT_ENTITY",
    "AUDIT_ACTION_CAPTURED",
    "AUDIT_ACTION_REFUSED",
]
