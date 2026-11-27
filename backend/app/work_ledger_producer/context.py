"""
The frozen day context: timezone (D-18E) and working day (D-18D).
=================================================================

Both are resolved STRICTLY and frozen onto the ledger at seal. Both refuse
rather than fall back — the attendance path may fail open so a bad row never
breaks clock-in, but evidence production may not, because a fallback would
record an offset or a working-week that was never configured.

TIMEZONE (D-18E)
----------------
``_tz`` deliberately fails open to ``FALLBACK_TZ_NAME`` ("Asia/Kolkata") when
``OrgSettings.default_timezone`` is unusable, and the name it then returns is
INDISTINGUISHABLE from a tenant that legitimately configured that zone. So the
producer reads the configuration field directly and refuses when it is absent
or unloadable — then does every conversion through the existing ``_tz``
helpers, so the offset recorded is exactly the one the platform applies. It
also verifies the helper agrees with the configured value, which catches a
cached or fallen-back zone before it can be sealed.

This is a strictness gate over one configuration field, not a second timezone
resolver.

WORKING DAY (D-18D, and now D2-A)
---------------------------------
The determination itself belongs to ``app.working_day_classifier``, the
dedicated semantic authority. This module ASKS it and freezes the verdict; it
does not decide. The classifier still rejects a ``system`` source — the
resolver's fail-open path, meaning no tier configured the working week — so an
unestablished classification refuses here rather than being frozen as a guess
(D8-A). The source label is persisted verbatim and never re-coded into a more
specific source (D11-A).

The one translation this module performs is from the classifier's typed
refusal to the producer's own ``RefusalReason``, so every refusal the producer
already had behaves exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import FrozenSet, Optional, Tuple, Union

from sqlalchemy.orm import Session

from app import _tz

from .outcomes import RefusalReason

#: The resolver source labels owner ruling D-18D admits, verbatim. Anything
#: else — including the resolver's own ``legacy_flexible_days`` — is refused as
#: unknown rather than mapped onto one of these, because "never infer a more
#: specific source" forbids the mapping.
PERMITTED_WORKING_DAY_SOURCES: FrozenSet[str] = frozenset({
    "assignment", "shift", "employee", "legacy_user", "team", "org", "system",
})

#: The one permitted label that still refuses: it is in the resolver's
#: vocabulary, and it means nothing was configured.
UNRESOLVED_WORKING_DAY_SOURCE = "system"


@dataclass(frozen=True)
class DayContext:
    """Everything about the target day that is frozen at seal."""

    work_date: date
    timezone_iana: str
    zone: tzinfo
    utc_offset_seconds: int
    day_start_utc: datetime
    day_end_utc: datetime
    is_working_day: bool
    working_day_source: str

    #: D-1.1: the scheduled end and its tier, from the SAME schedule
    #: resolution that produced the working-day verdict above. Optional
    #: because D-2.1 admits a day with no configured shift end.
    scheduled_end_local: Optional[time] = None
    scheduled_end_source: Optional[str] = None

    def contains(self, instant: datetime) -> bool:
        """Is this UTC instant inside the organization-local calendar day?

        Half-open ``[start, end)`` — the same convention the ledger's overlap
        rule uses, so a boundary landing exactly on local midnight belongs to
        the day that starts there and to no other.
        """
        return self.day_start_utc <= instant < self.day_end_utc


def _configured_timezone_name(db: Session, organization_id: int) -> Optional[str]:
    """``OrgSettings.default_timezone`` verbatim, or None when unusable.

    Read directly, because this is the single field ``_tz`` itself consults and
    the only place the difference between "configured" and "fell back" is
    visible.
    """
    from database import models

    row = (
        db.query(models.OrgSettings.default_timezone)
        .filter(models.OrgSettings.organization_id == organization_id)
        .first()
    )
    if row is None:
        return None
    raw = row[0]
    if raw is None:
        return None
    name = str(raw).strip()
    return name or None


def resolve_timezone(
    db: Session, organization_id: int
) -> Union[Tuple[str, tzinfo], RefusalReason]:
    """The tenant's validated zone, or the reason it cannot be established."""
    name = _configured_timezone_name(db, organization_id)
    if name is None:
        return RefusalReason.TIMEZONE_NOT_CONFIGURED
    if not _tz.is_valid_timezone(name):
        return RefusalReason.TIMEZONE_INVALID

    # The helper is the authority on the zone actually in force. If it does not
    # agree with the configured value, something (a stale cache, a fallback)
    # would make the sealed offset differ from the applied one.
    if _tz.org_timezone_name(db, organization_id) != name:
        return RefusalReason.TIMEZONE_HELPER_DIVERGENCE

    return name, _tz.org_timezone(db, organization_id)


def resolve_working_day(
    db: Session, user, work_date: date
) -> Union[Tuple[bool, str, Optional[time], Optional[str]], RefusalReason]:
    """The CLASSIFIER's verdict, translated into the producer's vocabulary.

    PASS-THROUGH, NOT CLASSIFICATION (D2-A)
    ---------------------------------------
    The determination belongs to ``app.working_day_classifier``, the dedicated
    semantic authority. This function does exactly two things: it asks, and it
    maps the classifier's typed refusal onto the producer's existing
    ``RefusalReason`` so every refusal the producer already had behaves
    identically. It decides nothing and must never re-derive the answer — the
    schedule resolver is deliberately no longer reachable from here.
    """
    from app.working_day_classifier import (
        ClassificationOutcome,
        ClassificationRefusal,
        classify_working_day,
    )

    classification = classify_working_day(
        db,
        organization_id=getattr(user, "organization_id", None),
        user=user,
        work_date=work_date,
    )

    if classification.outcome is ClassificationOutcome.UNCLASSIFIED:
        # D8-A: fail closed. The producer refuses and writes nothing; an
        # unestablished classification is NEVER converted into a non-working
        # day, and no UNKNOWN is invented.
        if classification.refusal is ClassificationRefusal.SOURCE_NOT_CONFIGURED:
            return RefusalReason.WORKING_DAY_UNRESOLVED
        return RefusalReason.WORKING_DAY_SOURCE_UNKNOWN

    # D-1.1: the scheduled end travels with the verdict because the
    # classifier resolved the schedule ONCE and both halves came out of that
    # single call. The producer must not reach for the resolver again.
    return (
        bool(classification.is_working_day),
        classification.working_day_source,
        classification.scheduled_end_local,
        classification.scheduled_end_source,
    )


def resolve_day_context(
    db: Session, *, organization_id: int, user, work_date: date
) -> Union[DayContext, RefusalReason]:
    """Resolve and freeze the day context, or refuse with a reason."""
    tz_result = resolve_timezone(db, organization_id)
    if isinstance(tz_result, RefusalReason):
        return tz_result
    timezone_iana, zone = tz_result

    day_start_utc, day_end_utc = _tz.local_day_bounds(
        db, organization_id, work_date)

    # The offset actually in force for THIS day, evaluated at its local start
    # rather than "now" — so a day either side of a DST transition records the
    # offset that applied to it, and a sealed day is unaffected by later
    # transitions.
    local_start = day_start_utc.astimezone(zone)
    offset = local_start.utcoffset() or timedelta(0)

    wd_result = resolve_working_day(db, user, work_date)
    if isinstance(wd_result, RefusalReason):
        return wd_result
    (is_working_day, working_day_source,
     scheduled_end_local, scheduled_end_source) = wd_result

    return DayContext(
        work_date=work_date,
        timezone_iana=timezone_iana,
        zone=zone,
        utc_offset_seconds=int(offset.total_seconds()),
        day_start_utc=day_start_utc,
        day_end_utc=day_end_utc,
        is_working_day=is_working_day,
        working_day_source=working_day_source,
        scheduled_end_local=scheduled_end_local,
        scheduled_end_source=scheduled_end_source,
    )


__all__ = [
    "DayContext",
    "PERMITTED_WORKING_DAY_SOURCES",
    "UNRESOLVED_WORKING_DAY_SOURCE",
    "resolve_timezone",
    "resolve_working_day",
    "resolve_day_context",
]
