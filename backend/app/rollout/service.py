"""
Reading and transitioning durable rollout state.
================================================

STEP 8 of the owner-authorised Phase 2 foundation.

NOT WIRED, AND NOTHING IS ON
----------------------------
No route, scheduler or existing service calls this. No control has been
transitioned. The Early Clock-Out control has zero rows, so it reads OFF for
every organization — the required final state of this step.

TWO PATHS WITH OPPOSITE DISPOSITIONS
------------------------------------
READ (:func:`rollout_state`) never raises and never blocks. A missing row, an
unreadable value, a database error — every one of them returns OFF. A control
consulted on a request path must not be able to take that request down, and the
safe answer is always "not on".

WRITE (:func:`transition`) refuses loudly. Every failure raises. An
administrator who cannot tell whether their change took effect will make it
twice.

WHAT THIS LAYER DOES NOT DECIDE
-------------------------------
Whether the thing being rolled out is CORRECT. The observation window is a
duration gate — it enforces that time has passed in SHADOW, not that anything
was observed to be right. Promotion is always an explicit act by an
administrator; there is no automatic promotion, no scheduled advance, and no
code path that raises a state without being asked to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import (
    InvalidRolloutTransition,
    ObservationWindowNotElapsed,
    RolloutOrganizationRequired,
    StaleRolloutVersion,
    UnknownRolloutControl,
    EnablementGatesNotSatisfied,
)
from .models import RolloutControl
from .states import (
    ALLOWED_TRANSITIONS,
    CONTROL_EARLY_CLOCK_OUT_V2,
    DEFAULT_MIN_OBSERVATION_DAYS,
    DEFAULT_STATE,
    RolloutState,
    can_transition,
    coerce_state,
    is_rollback,
    requires_observation_window,
)

_log = logging.getLogger("zoiko.rollout")

#: Control keys this system recognises. A transition against anything else is
#: refused. Adding a control is a deliberate edit here, not a side effect of
#: calling the service with a new string.
KNOWN_CONTROLS: frozenset = frozenset({CONTROL_EARLY_CLOCK_OUT_V2})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored instant to UTC.

    SQLite discards ``tzinfo`` on write, so an instant read back is naive even
    though it was stored aware. Comparing it against ``now`` would raise, and an
    exception inside the observation check would be an unhelpful way to discover
    a storage detail.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RolloutStatus:
    """A control's durable state, as a value rather than a live ORM row."""

    organization_id: int
    control_key: str
    state: RolloutState
    state_version: int
    is_canary: bool
    min_observation_days: int
    shadow_entered_at: Optional[datetime]
    enabled_at: Optional[datetime]
    exists: bool
    """False when no row has ever been written. The control is OFF either way;
    this distinguishes "never touched" from "deliberately turned off"."""

    @property
    def is_off(self) -> bool:
        return self.state is RolloutState.OFF

    @property
    def is_shadow(self) -> bool:
        return self.state is RolloutState.SHADOW

    @property
    def is_enabled(self) -> bool:
        return self.state is RolloutState.ENABLED

    def observation_days_elapsed(self, *, now: Optional[datetime] = None) -> Optional[float]:
        """Days since the current observation period began, or ``None``.

        ``None`` when the control is not observing — there is no window to
        measure, and returning 0 would read as "observed for zero days", which
        is a different and misleading claim.
        """
        started = _aware(self.shadow_entered_at)
        if self.state is not RolloutState.SHADOW or started is None:
            return None
        return ((now or _utcnow()) - started).total_seconds() / 86400.0


def _row(db: Session, *, organization_id: int, control_key: str):
    return db.execute(
        select(RolloutControl).where(
            RolloutControl.organization_id == organization_id,
            RolloutControl.control_key == control_key,
        )
    ).scalars().first()


# ── Read path — never raises ────────────────────────────────────────────────
def rollout_state(
    db: Session, *, organization_id: Optional[int], control_key: str
) -> RolloutState:
    """The state of one control for one organization. OFF unless proven ENABLED.

    Returns OFF for: no organization, no row, an unrecognised stored value, and
    any error reaching the database. Never raises — see the module docstring.
    """
    if organization_id is None:
        return DEFAULT_STATE
    try:
        row = _row(db, organization_id=organization_id, control_key=control_key)
    except Exception:  # pragma: no cover - defensive
        # A rollout control must not be able to fail a request. Logged so the
        # silence is visible in operations rather than only in behaviour.
        _log.warning(
            "rollout: could not read %s for organization %s; treating as OFF",
            control_key, organization_id, exc_info=True,
        )
        return DEFAULT_STATE
    if row is None:
        return DEFAULT_STATE
    return coerce_state(row.state)


def is_enabled(
    db: Session, *, organization_id: Optional[int], control_key: str
) -> bool:
    """True only when the control is ENABLED for this organization."""
    return rollout_state(
        db, organization_id=organization_id, control_key=control_key
    ) is RolloutState.ENABLED


def is_shadow(
    db: Session, *, organization_id: Optional[int], control_key: str
) -> bool:
    """True only when the control is observing. Not the same as enabled."""
    return rollout_state(
        db, organization_id=organization_id, control_key=control_key
    ) is RolloutState.SHADOW


def get_status(
    db: Session, *, organization_id: int, control_key: str
) -> RolloutStatus:
    """Full durable status, including metadata. OFF and ``exists=False`` when
    no row has been written."""
    row = _row(db, organization_id=organization_id, control_key=control_key)
    if row is None:
        return RolloutStatus(
            organization_id=organization_id, control_key=control_key,
            state=DEFAULT_STATE, state_version=0, is_canary=False,
            min_observation_days=DEFAULT_MIN_OBSERVATION_DAYS,
            shadow_entered_at=None, enabled_at=None, exists=False,
        )
    return RolloutStatus(
        organization_id=row.organization_id,
        control_key=row.control_key,
        state=coerce_state(row.state),
        state_version=row.state_version,
        is_canary=bool(row.is_canary),
        min_observation_days=row.min_observation_days,
        shadow_entered_at=_aware(row.shadow_entered_at),
        enabled_at=_aware(row.enabled_at),
        exists=True,
    )


def organizations_in_state(
    db: Session, *, control_key: str, state: RolloutState
) -> Sequence[int]:
    """Organizations whose stored value is exactly this state.

    Rows carrying an unrecognised value are excluded from every state including
    OFF — they are not evidence of anything, and listing them as OFF would
    imply somebody set them so.
    """
    rows = db.execute(
        select(RolloutControl.organization_id, RolloutControl.state)
        .where(RolloutControl.control_key == control_key)
        .order_by(RolloutControl.organization_id)
    ).all()
    return tuple(org for org, raw in rows if raw == state.value)


def canary_organizations(db: Session, *, control_key: str) -> Sequence[int]:
    """Organizations explicitly designated as canaries for this control."""
    return tuple(db.execute(
        select(RolloutControl.organization_id)
        .where(
            RolloutControl.control_key == control_key,
            RolloutControl.is_canary.is_(True),
        )
        .order_by(RolloutControl.organization_id)
    ).scalars().all())


# ── Write path — refuses loudly ─────────────────────────────────────────────
def _audit(
    db: Session, *, organization_id: int, actor_user_id: Optional[int],
    control_key: str, previous: RolloutState, new: RolloutState,
    reason: Optional[str], state_version: int, at: datetime,
) -> None:
    """Record the transition on the canonical governance audit chain.

    Uses ``models.AuditLog`` — the repository's existing immutable evidence
    chain, keyed on the same integer ``organization_id`` this table uses —
    rather than a second audit system. The commercial ``emit_event`` path was
    not used: it keys on the STRING commercial organization id, which would
    record transitions under a different identifier from the one they were
    made against.

    Payload carries no personal information: an actor id, never a name or email.
    """
    from database import models  # local import: keeps the module ORM-light

    db.add(models.AuditLog(
        organization_id=organization_id,
        actor_id=actor_user_id,
        entity="RolloutControl",
        action="RolloutTransition",
        payload={
            "control_key": control_key,
            "previous_state": previous.value,
            "new_state": new.value,
            "state_version": state_version,
            "reason": reason,
            "transitioned_at": at.isoformat(),
            "rollback": is_rollback(previous, new),
        },
    ))


#: The five §44.3 gates that describe CONFIGURATION rather than evidence of a
#: process. Owner ruling G: C — these are revalidated at the moment of
#: promotion, not trusted from whenever the shadow period began. A Tenant that
#: deleted its fallback policy on day 13 of a 14-day observation must not be
#: promoted on the strength of a check that passed on day 1.
_OPERATIONAL_GATES = (
    "WORKWEEK_TIMEZONE_ATTRIBUTION",
    "REQUIREMENT_FALLBACK",
    "NETTING_DECISION",
    "BREAK_TREATMENT",
    "ADMINISTRATIVE_RECIPIENT",
)

#: Every §44.3 gate, in specification order. Named here rather than imported
#: so this module states what IT requires; a test asserts the two agree, which
#: is how a gate added to §44.3 gets noticed here instead of silently skipped.
_ALL_GATES = _OPERATIONAL_GATES + (
    "SHADOW_PERIOD",
    "DPIA",
    "CONSULTATION_ATTESTATION",
    "WORKER_NOTICE",
)


def _gates_required_for(current: RolloutState,
                        target: RolloutState) -> Sequence[str]:
    """Owner ruling D6, transcribed as a lookup.

    OFF -> SHADOW binds the five OPERATIONAL gates and nothing else. Shadow
    observation has not started, so its gate cannot be met; and the three
    compliance gates are deliberately absent, because §44.2 shadow running
    changes nothing a Worker experiences and requiring a DPIA before you may
    even OBSERVE would make the observation period unreachable.

    SHADOW -> ENABLED binds all nine. The five operational ones are RE-checked
    (ruling G) rather than trusted from whenever shadow began.

    Every other transition binds nothing. Rollback and re-shadow reduce live
    exposure, and a Tenant in trouble must not be held in it until its
    paperwork is in order.
    """
    if current is RolloutState.OFF and target is RolloutState.SHADOW:
        return _OPERATIONAL_GATES
    if current is RolloutState.SHADOW and target is RolloutState.ENABLED:
        return _ALL_GATES
    return ()


def _refuse_unless_gates_pass(db: Session, *, organization_id: int,
                              at: datetime, gates: Sequence[str],
                              observation: Optional[object] = None) -> None:
    """Owner ruling D6: §44.3's gates are a machine-enforced precondition of
    promotion, not a checklist somebody is trusted to have worked through.

    WHY THE GATE LIVES HERE AND NOT IN THE CHECKER
    ----------------------------------------------
    The checker reads and reports; it holds no state and enables nothing. This
    module owns the state, so this module does the refusing. That also fixes
    the direction of the dependency: ``rollout`` composes ``enablement_gates``
    through ``ShadowObservation``, and the checker imports nothing from here.

    WHY THE OBSERVATION IS PASSED IN
    --------------------------------
    The caller already holds the row. Handing the checker the same
    ``shadow_entered_at`` and ``min_observation_days`` this function just
    validated means the gate and the window rule cannot disagree about the one
    fact they share.

    A BLOCKED TRANSITION NAMES EVERY FAILING GATE
    ---------------------------------------------
    All of them, not the first. An operator who fixes one gate and is then told
    about the next has been made to discover the requirements one refusal at a
    time.

    ``observation`` is None on OFF -> SHADOW, where no shadow period exists
    yet. The SHADOW_PERIOD gate then reports EVIDENCE_MISSING, which is
    truthful and which ``gates`` excludes from consideration for that
    transition -- the gate is not silenced, it is simply not binding here.
    """
    from app.enablement_gates import assess_enablement_gates

    readiness = assess_enablement_gates(
        db, organization_id=organization_id, as_of=at.date(), now=at,
        shadow=observation)

    binding = frozenset(gates)
    blocking = [g for g in readiness.gates
                if g.gate.value in binding and g.blocking and not g.passes]
    if not blocking:
        return

    operational = [g for g in blocking if g.gate.value in _OPERATIONAL_GATES]
    detail = "; ".join(
        f"{g.gate.value}={g.status.value} ({g.reason})" for g in blocking)
    raise EnablementGatesNotSatisfied(
        f"{CONTROL_EARLY_CLOCK_OUT_V2} for organization {organization_id} "
        f"cannot move to {_target_name(gates)}: {len(blocking)} of the "
        f"{len(binding)} §44.3 gate(s) binding on this transition are not "
        f"satisfied, {len(operational)} of them operational. {detail}",
        gates=tuple(blocking),
    )


def _target_name(gates: Sequence[str]) -> str:
    """Which transition was refused, for the message only."""
    return "ENABLED" if len(gates) == len(_ALL_GATES) else "SHADOW"


def transition(
    db: Session,
    *,
    organization_id: Optional[int],
    control_key: str,
    target: RolloutState,
    expected_version: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    actor_label: str = "SYSTEM",
    reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> RolloutStatus:
    """Move one control to ``target`` for one organization.

    ``expected_version`` is the version the caller read. ``None`` means "no row
    yet". A mismatch is rejected rather than merged: a rollback and a promotion
    racing each other must not silently resolve in favour of whoever committed
    last.

    Requesting the state a control is already in is a no-op — no version bump
    and no audit row, because nothing changed and an audit trail full of
    non-events is one nobody reads.

    Raises on every refusal. Never enables anything implicitly: ``target`` is
    always stated by the caller, and reaching ENABLED is only possible from
    SHADOW after the observation window.
    """
    if organization_id is None:
        raise RolloutOrganizationRequired(
            "a rollout transition requires an organization; there is no global "
            "rollout switch"
        )
    if control_key not in KNOWN_CONTROLS:
        raise UnknownRolloutControl(
            f"{control_key!r} is not a declared rollout control. Known: "
            f"{sorted(KNOWN_CONTROLS)}. Declare it rather than creating one by "
            f"calling with a new string."
        )
    if not isinstance(target, RolloutState):
        raise InvalidRolloutTransition(
            f"target must be a RolloutState, got {target!r}"
        )

    from app.enablement_gates import ShadowObservation

    at = now or _utcnow()
    row = _row(db, organization_id=organization_id, control_key=control_key)

    current = coerce_state(row.state) if row is not None else DEFAULT_STATE
    current_version = row.state_version if row is not None else None

    # ── Concurrency, checked before anything else is decided ────────────────
    if expected_version != current_version:
        raise StaleRolloutVersion(
            f"{control_key} for organization {organization_id} is at version "
            f"{current_version!r}, but the transition was computed against "
            f"{expected_version!r}. Re-read and decide again."
        )

    # ── Already there ───────────────────────────────────────────────────────
    if current is target:
        return get_status(
            db, organization_id=organization_id, control_key=control_key
        )

    # ── Is this a transition that exists? ───────────────────────────────────
    if not can_transition(current, target):
        allowed = sorted(
            s.value for s in ALLOWED_TRANSITIONS.get(current, frozenset())
        )
        raise InvalidRolloutTransition(
            f"{current.value} -> {target.value} is not a transition. From "
            f"{current.value} the lifecycle allows: {allowed or 'nothing'}. "
            f"Reaching ENABLED requires passing through SHADOW."
        )

    # ── The one gate that delays: promotion to live ─────────────────────────
    if requires_observation_window(current, target):
        required = (
            row.min_observation_days if row is not None
            else DEFAULT_MIN_OBSERVATION_DAYS
        )
        started = _aware(row.shadow_entered_at) if row is not None else None
        if started is None:
            raise ObservationWindowNotElapsed(
                f"{control_key} for organization {organization_id} has no "
                f"recorded observation start; it cannot be promoted.",
                days_remaining=float(required),
            )
        elapsed = (at - started).total_seconds() / 86400.0
        if elapsed < required:
            raise ObservationWindowNotElapsed(
                f"{control_key} for organization {organization_id} has observed "
                f"for {elapsed:.2f} of the required {required} days.",
                days_remaining=required - elapsed,
            )

    # ── §44.3, on whichever transition binds it (owner ruling D6) ──────────
    required_gates = _gates_required_for(current, target)
    if required_gates and control_key == CONTROL_EARLY_CLOCK_OUT_V2:
        observation = None
        if RolloutState.SHADOW in (current,) and row is not None:
            # Hand the checker the same two facts the window rule just used,
            # so the gate and the window cannot disagree about the one thing
            # they share.
            observation = ShadowObservation(
                exists=True, state=current.value,
                min_observation_days=float(row.min_observation_days
                                           or DEFAULT_MIN_OBSERVATION_DAYS),
                shadow_entered_at=_aware(row.shadow_entered_at))
        _refuse_unless_gates_pass(
            db, organization_id=organization_id, at=at,
            gates=required_gates, observation=observation)

    # ── Apply ───────────────────────────────────────────────────────────────
    if row is None:
        row = RolloutControl(
            organization_id=organization_id,
            control_key=control_key,
            state=DEFAULT_STATE.value,
            state_version=0,
            is_canary=False,
            min_observation_days=DEFAULT_MIN_OBSERVATION_DAYS,
        )
        db.add(row)

    row.state = target.value
    row.state_version = (row.state_version or 0) + 1
    row.last_transition_at = at
    row.last_transition_by = actor_label
    row.last_transition_reason = reason

    if target is RolloutState.SHADOW:
        # A fresh observation period. A control rolled back and re-shadowed
        # observes again from now rather than inheriting the earlier run's age.
        row.shadow_entered_at = at
        row.enabled_at = None
    elif target is RolloutState.ENABLED:
        row.enabled_at = at
    else:  # OFF
        row.shadow_entered_at = None
        row.enabled_at = None

    _audit(
        db, organization_id=organization_id, actor_user_id=actor_user_id,
        control_key=control_key, previous=current, new=target, reason=reason,
        state_version=row.state_version, at=at,
    )
    db.flush()

    return get_status(
        db, organization_id=organization_id, control_key=control_key
    )


def set_canary(
    db: Session,
    *,
    organization_id: int,
    control_key: str,
    is_canary: bool,
    expected_version: int,
    actor_user_id: Optional[int] = None,
    actor_label: str = "SYSTEM",
) -> RolloutStatus:
    """Designate (or undesignate) an organization as a canary. Explicit only.

    Does NOT change the rollout state — a canary is a marker saying "this
    organization goes first", not a state. Nothing here selects canaries: the
    caller names one.

    Requires an existing row, so an organization must have been transitioned at
    least once before it can be marked. That keeps "is a canary" from being the
    thing that creates a control's first row.
    """
    row = _row(db, organization_id=organization_id, control_key=control_key)
    if row is None:
        raise UnknownRolloutControl(
            f"{control_key} has no row for organization {organization_id}; "
            f"transition it before designating it a canary"
        )
    if row.state_version != expected_version:
        raise StaleRolloutVersion(
            f"{control_key} for organization {organization_id} is at version "
            f"{row.state_version}, not {expected_version}"
        )

    if bool(row.is_canary) == bool(is_canary):
        return get_status(
            db, organization_id=organization_id, control_key=control_key
        )

    from database import models  # local import: keeps the module ORM-light

    row.is_canary = bool(is_canary)
    row.state_version += 1
    db.add(models.AuditLog(
        organization_id=organization_id,
        actor_id=actor_user_id,
        entity="RolloutControl",
        action="RolloutCanaryChanged",
        payload={
            "control_key": control_key,
            "is_canary": bool(is_canary),
            "state_version": row.state_version,
            "actor_label": actor_label,
        },
    ))
    db.flush()
    return get_status(
        db, organization_id=organization_id, control_key=control_key
    )


def set_observation_days(
    db: Session,
    *,
    organization_id: int,
    control_key: str,
    days: int,
    expected_version: int,
    actor_user_id: Optional[int] = None,
) -> RolloutStatus:
    """Change how long this control must observe before it may be promoted.

    Lengthening is always allowed. Shortening is refused while the control is
    observing: an administrator who could shorten the window mid-flight could
    promote immediately, which would make the gate advisory rather than real.
    """
    row = _row(db, organization_id=organization_id, control_key=control_key)
    if row is None:
        raise UnknownRolloutControl(
            f"{control_key} has no row for organization {organization_id}"
        )
    if row.state_version != expected_version:
        raise StaleRolloutVersion(
            f"{control_key} for organization {organization_id} is at version "
            f"{row.state_version}, not {expected_version}"
        )
    if days < 0:
        raise InvalidRolloutTransition("observation days cannot be negative")
    if (
        coerce_state(row.state) is RolloutState.SHADOW
        and days < row.min_observation_days
    ):
        raise InvalidRolloutTransition(
            f"cannot shorten the observation window from "
            f"{row.min_observation_days} to {days} days while observing; "
            f"roll back to OFF first if the window was set wrongly"
        )

    from database import models  # local import: keeps the module ORM-light

    previous = row.min_observation_days
    row.min_observation_days = days
    row.state_version += 1
    db.add(models.AuditLog(
        organization_id=organization_id,
        actor_id=actor_user_id,
        entity="RolloutControl",
        action="RolloutObservationWindowChanged",
        payload={
            "control_key": control_key,
            "previous_days": previous,
            "new_days": days,
            "state_version": row.state_version,
        },
    ))
    db.flush()
    return get_status(
        db, organization_id=organization_id, control_key=control_key
    )


__all__ = [
    "RolloutStatus",
    "KNOWN_CONTROLS",
    "rollout_state",
    "is_enabled",
    "is_shadow",
    "get_status",
    "organizations_in_state",
    "canary_organizations",
    "transition",
    "set_canary",
    "set_observation_days",
]
