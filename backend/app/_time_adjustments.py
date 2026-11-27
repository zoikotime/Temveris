"""
Aggregation helpers for the `time_adjustments` table.

A TimeAdjustment row is admin-credited compensation for work that was
genuinely done but lost to a tracker outage (power cut, system crash,
network failure that fired OFFLINE_CLOCKOUT_MS, etc.). The original
ClockSession rows stay immutable; these helpers fold the active
adjustments into worked-hours math so reports, the admin "My Team"
dashboard, and the employee's own day summary all show the corrected
totals.

Only `is_active=True` rows count. Reverts (`is_active=False`) drop out
silently so the admin's revert action is reflected in totals immediately.

Centralised here (rather than re-implemented per router) so that adding
a new aggregation surface — payroll, burnout, weekly export — is a
one-line import, not a copy-paste.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Iterable

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from database import models


def adjustment_seconds_for_day(db: Session, user_id: int, day: date_type) -> int:
    """Sum of active adjustments for one user on one IST calendar day."""
    rows = db.query(models.TimeAdjustment.seconds_added).filter(
        and_(
            models.TimeAdjustment.user_id         == user_id,
            models.TimeAdjustment.adjustment_date == day,
            models.TimeAdjustment.is_active       == True,
        )
    ).all()
    return sum(int(r[0] or 0) for r in rows)


def adjustment_seconds_by_user(
    db: Session,
    user_ids: Iterable[int],
    start: date_type,
    end: date_type,
) -> dict[int, int]:
    """Bulk: { user_id: total_active_seconds } across a date range.

    `start` inclusive, `end` exclusive — matches the half-open
    convention used by `_day_window` and friends across the codebase.
    """
    user_ids = list(user_ids)
    if not user_ids:
        return {}
    rows = db.query(
        models.TimeAdjustment.user_id,
        func.sum(models.TimeAdjustment.seconds_added),
    ).filter(
        and_(
            models.TimeAdjustment.user_id.in_(user_ids),
            models.TimeAdjustment.adjustment_date >= start,
            models.TimeAdjustment.adjustment_date <  end,
            models.TimeAdjustment.is_active       == True,
        )
    ).group_by(models.TimeAdjustment.user_id).all()
    return {uid: int(secs or 0) for uid, secs in rows}


def adjustment_seconds_for_day_by_user(
    db: Session,
    user_ids: Iterable[int],
    day: date_type,
) -> dict[int, int]:
    """Bulk single-day variant: { user_id: total_active_seconds } for `day`."""
    user_ids = list(user_ids)
    if not user_ids:
        return {}
    rows = db.query(
        models.TimeAdjustment.user_id,
        func.sum(models.TimeAdjustment.seconds_added),
    ).filter(
        and_(
            models.TimeAdjustment.user_id.in_(user_ids),
            models.TimeAdjustment.adjustment_date == day,
            models.TimeAdjustment.is_active       == True,
        )
    ).group_by(models.TimeAdjustment.user_id).all()
    return {uid: int(secs or 0) for uid, secs in rows}
