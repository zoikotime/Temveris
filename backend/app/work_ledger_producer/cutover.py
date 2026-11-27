"""
Reading and extending the producer cutover history (owner ruling D18-F1).
=========================================================================

Two operations:

* :func:`get_producer_cutover` — the tenant's effective watermark, which is the
  MAXIMUM recorded value, or ``None``.
* :func:`set_producer_cutover` — append a LATER value. Never an earlier one,
  and never by rewriting an existing row.

The producer reads the watermark itself. It is never a parameter: a caller that
could supply it could seal any historical day by supplying an earlier one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (
    ProducerCutoverMovedBackward,
    WorkLedgerProducerCutoverHistory,
)

#: Actor label for an unattended set, following the established convention
#: ("an id where one exists, SYSTEM otherwise").
SYSTEM_ACTOR_LABEL = "SYSTEM"


def _require_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime, got {value!r}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive watermark would be read "
            f"back as UTC and silently shift which days are eligible"
        )
    return value.astimezone(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce a stored instant to aware UTC.

    pysqlite drops ``tzinfo`` on read while PostgreSQL preserves it, so a
    stored watermark must be normalised before any comparison — otherwise
    eligibility would differ between backends, which for a forward-only
    guarantee is not an acceptable difference.
    """
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
            else value.astimezone(timezone.utc))


def get_producer_cutover(db: Session, organization_id: int) -> Optional[datetime]:
    """The tenant's EFFECTIVE watermark as aware UTC, or ``None``.

    The maximum recorded value — which is why an earlier row, however it got
    there, cannot lower it. ``None`` means REFUSE; it is never read as "no
    restriction" and no default is substituted.
    """
    latest = (
        db.query(func.max(WorkLedgerProducerCutoverHistory.producer_cutover_at))
        .filter(WorkLedgerProducerCutoverHistory.organization_id
                == organization_id)
        .scalar()
    )
    return _as_utc(latest)


def set_producer_cutover(
    db: Session,
    *,
    organization_id: int,
    cutover_at: datetime,
    actor_label: str = SYSTEM_ACTOR_LABEL,
) -> datetime:
    """Append a later watermark. Returns the effective instant afterwards.

    * ``target > current`` — INSERT a new history row.
    * ``target == current`` — idempotent no-op; nothing is written.
    * ``target < current`` — :class:`ProducerCutoverMovedBackward`.

    There is no UPDATE path and no DELETE path. Does not commit — the caller
    owns the transaction.
    """
    target = _require_aware(cutover_at, "cutover_at")
    if not isinstance(actor_label, str) or not actor_label.strip():
        raise ValueError(
            "an actor label is required; pass SYSTEM_ACTOR_LABEL explicitly "
            "for an unattended set"
        )
    label = actor_label.strip()[:64]

    current = get_producer_cutover(db, organization_id)

    if current is not None:
        if target < current:
            raise ProducerCutoverMovedBackward(
                f"producer cutover for organization {organization_id} may only "
                f"move later: {current.isoformat()} -> {target.isoformat()} "
                f"was refused"
            )
        if target == current:
            return current  # idempotent: no row, no history noise

    db.add(WorkLedgerProducerCutoverHistory(
        organization_id=organization_id,
        producer_cutover_at=target,
        set_at=datetime.now(timezone.utc),
        set_by=label,
    ))
    db.flush()
    return target


__all__ = [
    "get_producer_cutover",
    "set_producer_cutover",
    "SYSTEM_ACTOR_LABEL",
]
