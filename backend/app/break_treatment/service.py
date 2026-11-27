"""Capture and historical resolution of break-treatment profiles."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import (
    BreakTreatmentProfileCollision,
    InvalidBreakTreatmentProfile,
)
from .models import BreakTreatmentProfile, compute_profile_content_hash
from .outcomes import ProfileOutcome, ProfileResolution


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def capture_break_treatment_profile(
    db: Session,
    *,
    organization_id: int,
    profile_ref: str,
    effective_from: date | datetime,
    meal_qualifies: bool,
    rest_qualifies: bool,
    rest_qualifying_cap_minutes: Optional[int] = None,
    captured_by: str,
    captured_at_utc: Optional[datetime] = None,
) -> BreakTreatmentProfile:
    """Append one immutable break-treatment profile.

    Every value is validated BEFORE anything is added to the session, so a
    rejected capture leaves the store byte-identical rather than a partial row
    to be rolled back. The unique constraints remain the net against a
    concurrent writer that passes these checks and then loses the race.

    Does not commit; the caller owns the transaction boundary.
    """
    eff = _as_date(effective_from)

    if not (profile_ref or "").strip():
        raise InvalidBreakTreatmentProfile(
            "profile_ref must be a non-empty key")
    if not (captured_by or "").strip():
        raise InvalidBreakTreatmentProfile("captured_by must be a non-empty label")
    for name, value in (("meal_qualifies", meal_qualifies),
                        ("rest_qualifies", rest_qualifies)):
        if not isinstance(value, bool):
            raise InvalidBreakTreatmentProfile(f"{name} must be a boolean")
    if rest_qualifying_cap_minutes is not None:
        if (not isinstance(rest_qualifying_cap_minutes, int)
                or isinstance(rest_qualifying_cap_minutes, bool)):
            raise InvalidBreakTreatmentProfile(
                "rest_qualifying_cap_minutes must be an integer or None")
        if rest_qualifying_cap_minutes < 0:
            raise InvalidBreakTreatmentProfile(
                f"rest_qualifying_cap_minutes must be >= 0, got "
                f"{rest_qualifying_cap_minutes}")

    existing_ref = db.execute(
        select(BreakTreatmentProfile.id).where(
            BreakTreatmentProfile.organization_id == organization_id,
            BreakTreatmentProfile.profile_ref == profile_ref,
        )
    ).first()
    if existing_ref is not None:
        raise BreakTreatmentProfileCollision(
            f"organization {organization_id} already has a profile "
            f"{profile_ref!r}; a changed rule is a NEW key, never a rewrite")

    existing_date = db.execute(
        select(BreakTreatmentProfile.profile_ref).where(
            BreakTreatmentProfile.organization_id == organization_id,
            BreakTreatmentProfile.effective_from == eff,
        )
    ).first()
    if existing_date is not None:
        raise BreakTreatmentProfileCollision(
            f"organization {organization_id} already has a profile effective "
            f"{eff.isoformat()} ({existing_date[0]!r}); one date resolves to "
            f"one answer")

    row = BreakTreatmentProfile(
        organization_id=organization_id,
        profile_ref=profile_ref,
        effective_from=eff,
        captured_by=captured_by,
        meal_qualifies=bool(meal_qualifies),
        rest_qualifies=bool(rest_qualifies),
        rest_qualifying_cap_minutes=(
            None if rest_qualifying_cap_minutes is None
            else int(rest_qualifying_cap_minutes)),
        content_hash=compute_profile_content_hash(
            organization_id=organization_id,
            profile_ref=profile_ref,
            effective_from=eff,
            meal_qualifies=meal_qualifies,
            rest_qualifies=rest_qualifies,
            rest_qualifying_cap_minutes=rest_qualifying_cap_minutes,
        ),
    )
    if captured_at_utc is not None:
        row.captured_at_utc = captured_at_utc
    db.add(row)
    return row


def resolve_profile_as_of(
    db: Session,
    *,
    organization_id: int,
    as_of: date | datetime,
) -> ProfileResolution:
    """Which break treatment governed ``organization_id`` on ``as_of``.

    The latest profile whose ``effective_from <= as_of``. NEVER the latest
    outright — that is what makes a future-dated profile invisible to today and
    to every historical date before it takes effect.

    Fails closed in three ways, each typed: below the historical floor
    (``NO_VERSION``), two profiles claiming one date (``AMBIGUOUS``), and a
    stored value outside its governed domain (``INVALID``). No live
    configuration is consulted in any of them.
    """
    on = _as_date(as_of)

    rows = db.execute(
        select(BreakTreatmentProfile)
        .where(
            BreakTreatmentProfile.organization_id == organization_id,
            BreakTreatmentProfile.effective_from <= on,
        )
        .order_by(BreakTreatmentProfile.effective_from.desc(),
                  BreakTreatmentProfile.id.desc())
    ).scalars().all()

    if not rows:
        return ProfileResolution(outcome=ProfileOutcome.NO_VERSION,
                                 organization_id=organization_id, as_of=on)

    winner = rows[0]
    if len(rows) > 1 and rows[1].effective_from == winner.effective_from:
        return ProfileResolution(
            outcome=ProfileOutcome.AMBIGUOUS,
            organization_id=organization_id, as_of=on,
            invalid_reason=(
                f"two profiles effective {winner.effective_from.isoformat()}"))

    if (winner.rest_qualifying_cap_minutes is not None
            and winner.rest_qualifying_cap_minutes < 0):
        return ProfileResolution(
            outcome=ProfileOutcome.INVALID,
            organization_id=organization_id, as_of=on,
            invalid_reason=(
                f"rest_qualifying_cap_minutes "
                f"{winner.rest_qualifying_cap_minutes!r}"))
    if winner.meal_qualifies is None or winner.rest_qualifies is None:
        return ProfileResolution(
            outcome=ProfileOutcome.INVALID,
            organization_id=organization_id, as_of=on,
            invalid_reason="qualification flag is NULL")

    return ProfileResolution(
        outcome=ProfileOutcome.RESOLVED,
        organization_id=organization_id,
        as_of=on,
        profile_ref=winner.profile_ref,
        effective_from=winner.effective_from,
        content_hash=winner.content_hash,
        meal_qualifies=bool(winner.meal_qualifies),
        rest_qualifies=bool(winner.rest_qualifies),
        rest_qualifying_cap_minutes=winner.rest_qualifying_cap_minutes,
    )


__all__ = ["capture_break_treatment_profile", "resolve_profile_as_of"]
