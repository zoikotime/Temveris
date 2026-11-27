"""
MODEL-HORIZON — §21.5's retroactive notification horizon.
==========================================================

Owner rulings D-7.1 and D-7.2.

§21.5, verbatim:

    "A Tenant-configurable retroactive horizon (default 90 days) bounds how far
     back a retroactive threshold event may be raised. Beyond the horizon the
     recomputation still occurs and the record is still corrected, but no
     notification is sent — a threshold notification about a week five months
     ago has no legitimate operational purpose and reads as harassment."

WHAT THIS MODULE DECIDES, AND WHAT IT DOES NOT
-----------------------------------------------
It answers ONE question:

    "Is a retroactive threshold event for this workweek still within the
     horizon that governed that workweek?"

It decides **notification eligibility and nothing else**. It does not suppress
a correction, does not touch a day record, does not withhold an event, and
does not delete or alter anything. §21.5 is explicit that beyond the horizon
the recomputation still runs and the record is still corrected; only the
notification stops. `record_corrected = true` with `notification_sent = false`
is not a contradiction — it is the rule.

Nor does it notify. No recipient, audience, delivery attempt or transport
appears here or is reachable from here; §33.1's matrix (beyond horizon =
"Never", for all four audiences) is consumed by ALERT, in a later increment.
This module hands that increment a decision it can act on.

THE CALCULATION (D-7.2)
------------------------
    elapsed_days = raise_date - workweek_end_date

    beyond = elapsed_days > retroactive_horizon_days

CALENDAR DAYS, IN THE FROZEN WORKWEEK TIMEZONE
-----------------------------------------------
Both operands are LOCAL CALENDAR DATES, and the difference is a count of whole
days between them. That is a deliberate rejection of three tempting
alternatives, each of which gives a different answer:

* **UTC timestamp subtraction** would make the boundary depend on the time of
  day the correction happened to be raised. A correction at 23:00 and one at
  01:00 the next morning are a day apart on the calendar and two hours apart in
  UTC, and §21.5 is expressed in days.
* **`elapsed.total_seconds() / 86400`** reintroduces the same problem with a
  rounding step on top.
* **The CURRENT organization timezone** would let a tenant that relocated move
  a historical week's boundary. §17's historical-reproducibility rule forbids
  it, which is why the zone comes from the LEDGER — frozen at seal under D-18E
  — and never from configuration read today.

`workweek_end_date` is the last of the canonical dates the workweek key
denotes, taken from EVAL-WEEK's own membership function. No ISO-week
arithmetic appears here; a Sunday-start tenant's week is its own seven dates,
and re-deriving that would be exactly the drift W4-3's start-date format
exists to remove.

STRICTLY GREATER-THAN (D-7.2)
------------------------------
    89 days against a horizon of 90  ->  WITHIN
    90 days against a horizon of 90  ->  WITHIN
    91 days against a horizon of 90  ->  BEYOND

The horizon is the last day still inside it, not the first day outside. `>=`
would silence a notification exactly one day early, every time.

THE POLICY IS THE WEEK'S, NOT TODAY'S
--------------------------------------
The horizon resolves as-of the **workweek start** — the same anchor
`resolve_threshold` already uses for "which rule governs this week", so one
convention answers both. A policy captured after the week cannot reach back
and change whether a correction to it was notifiable, which is the whole point
of putting the horizon on a versioned artifact (D-7.1).

`None` IS NOT 90
----------------
90 is what a CAPTURE records when the Tenant states nothing. It is never
substituted at resolution for a row that carries no horizon. A policy with a
NULL horizon refuses here rather than being evaluated against a bound nobody
configured — the same rule D-1.1/D-2.1 set for the scheduled end, and the
reason Stage 2 kept NULL and 90 distinguishable.

NOTHING IS PERSISTED BY THIS MODULE
------------------------------------
No migration, no column, no horizon state. Every input is already durable
evidence:

    raise instant        the event's own ``created_at_utc``
    workweek end         derived from the event's ``workweek_key``
    frozen timezone      ``WorkDayLedger.timezone_iana``, frozen at seal
    horizon value        the immutable ``PolicyVersion`` governing the week

so the decision replays identically from stored rows and stays OUT of the
evaluation identity and the day-record content hash (D-7.6). A clock-dependent
value inside the identity would break the determinism every other ECO
invariant rests on.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

#: Refusal reasons. Stable strings, so a caller matches on them rather than on
#: message wording.
REASON_HORIZON_POLICY_UNRESOLVED = "HORIZON_POLICY_UNRESOLVED"
REASON_HORIZON_NOT_CONFIGURED = "HORIZON_NOT_CONFIGURED"
REASON_HORIZON_TIMEZONE_UNRESOLVED = "HORIZON_TIMEZONE_UNRESOLVED"
REASON_HORIZON_MALFORMED_WORKWEEK = "HORIZON_MALFORMED_WORKWEEK"


class HorizonOutcome(str, enum.Enum):
    """What the horizon check established. Exactly one per call."""

    WITHIN = "WITHIN"
    """The correction is inside the horizon that governed the workweek. A
    retroactive threshold event remains notifiable."""

    BEYOND = "BEYOND"
    """Outside it. The recomputation still occurred and the record is still
    corrected; only the notification is withheld (§21.5)."""

    REFUSED = "REFUSED"
    """The decision could not be made. No eligibility is invented in either
    direction — neither "notify" nor "stay silent" may be guessed."""


@dataclass(frozen=True)
class HorizonResolution:
    """The decision, in a form a caller cannot misread as a boolean."""

    outcome: HorizonOutcome

    workweek_key: Optional[str] = None
    workweek_end_date: Optional[date] = None
    raise_date: Optional[date] = None
    timezone_iana: Optional[str] = None

    days_elapsed: Optional[int] = None
    retroactive_horizon_days: Optional[int] = None

    #: Which immutable policy supplied the horizon. Provenance, so a reader can
    #: verify the decision without re-resolving anything.
    policy_version: Optional[str] = None

    reason: Optional[str] = None
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome is HorizonOutcome.REFUSED:
            if not self.reason:
                raise ValueError("a REFUSED horizon must carry a reason")
            for field in ("days_elapsed", "retroactive_horizon_days"):
                if getattr(self, field) is not None:
                    raise ValueError(
                        f"REFUSED must not carry {field}: a decision that was "
                        f"not made produced no number")
            return
        if self.reason is not None:
            raise ValueError(
                f"{self.outcome.value} must not carry a refusal reason")
        for field in ("days_elapsed", "retroactive_horizon_days",
                      "workweek_end_date", "raise_date"):
            if getattr(self, field) is None:
                raise ValueError(f"{self.outcome.value} requires {field}")
        expected = (HorizonOutcome.BEYOND
                    if self.days_elapsed > self.retroactive_horizon_days
                    else HorizonOutcome.WITHIN)
        if self.outcome is not expected:
            raise ValueError(
                f"{self.outcome.value} contradicts {self.days_elapsed} days "
                f"against a horizon of {self.retroactive_horizon_days}")

    @property
    def is_refused(self) -> bool:
        return self.outcome is HorizonOutcome.REFUSED

    @property
    def is_beyond_horizon(self) -> bool:
        """True only on an established BEYOND. A refusal is not a 'beyond'."""
        return self.outcome is HorizonOutcome.BEYOND

    @property
    def notification_eligible(self) -> Optional[bool]:
        """§21.5's consequence, named. ``None`` when nothing was established.

        The ONLY thing this module decides. It says nothing about whether a
        notification is then sent — audiences, recipients and delivery belong
        to ALERT.
        """
        if self.outcome is HorizonOutcome.WITHIN:
            return True
        if self.outcome is HorizonOutcome.BEYOND:
            return False
        return None


def _local_date(instant: datetime, zone: ZoneInfo) -> date:
    """The LOCAL calendar date of a UTC instant. Never a UTC date."""
    if instant.tzinfo is None:
        # SQLite drops tzinfo on read while Postgres preserves it. Every ECO
        # boundary stores a true UTC instant (§16.4), so reading a naive value
        # as UTC is the platform's existing convention — the same one
        # `work_ledger.service._utc` and the §18 step-14 variance use.
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(zone).date()


def horizon_decision(
    *,
    raise_at_utc: datetime,
    workweek_end_date: date,
    timezone_iana: str,
    retroactive_horizon_days: int,
    workweek_key: Optional[str] = None,
    policy_version: Optional[str] = None,
) -> HorizonResolution:
    """§21.5's comparison. PURE — no database, no clock, no configuration.

    THE ONE SITE. Every caller goes through here, so the boundary is defined
    once and `>` cannot quietly become `>=` in a second copy.
    """
    try:
        zone = ZoneInfo(str(timezone_iana))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return HorizonResolution(
            outcome=HorizonOutcome.REFUSED,
            workweek_key=workweek_key,
            reason=REASON_HORIZON_TIMEZONE_UNRESOLVED,
            detail=(f"{timezone_iana!r} is not a usable zone; the local "
                    f"calendar dates §21.5 compares cannot be established"))

    raise_date = _local_date(raise_at_utc, zone)
    # Whole calendar days between two LOCAL dates. `date - date` is already a
    # whole-day timedelta, so no division, no rounding and no hours appear.
    days_elapsed = (raise_date - workweek_end_date).days

    # D-7.2: STRICTLY greater-than. The horizon is the last day still inside.
    beyond = days_elapsed > int(retroactive_horizon_days)

    return HorizonResolution(
        outcome=HorizonOutcome.BEYOND if beyond else HorizonOutcome.WITHIN,
        workweek_key=workweek_key,
        workweek_end_date=workweek_end_date,
        raise_date=raise_date,
        timezone_iana=str(timezone_iana),
        days_elapsed=days_elapsed,
        retroactive_horizon_days=int(retroactive_horizon_days),
        policy_version=policy_version,
    )


def _frozen_timezone(
    db: Session, *, organization_id: int, user_id: int, workweek_key: str
) -> Optional[str]:
    """The zone frozen on this workweek's ledger rows, or None.

    D-18E froze it at seal precisely so a later configuration change cannot
    move a historical day; reading it here rather than asking `_tz` is what
    makes the horizon reproducible.

    Rows disagreeing is a contract violation upstream, not something to resolve
    by picking one — a week whose days claim different zones has no single
    local calendar to measure against, so None is returned and the caller
    refuses.
    """
    from app.eval_week.membership import current_ledger_days

    zones = {
        str(row.timezone_iana) for row in current_ledger_days(
            db, organization_id=organization_id, user_id=user_id,
            workweek_key=workweek_key)
        if getattr(row, "timezone_iana", None)
    }
    if len(zones) != 1:
        return None
    return zones.pop()


def resolve_retroactive_horizon(
    db: Session,
    *,
    organization_id: int,
    user_id: int,
    workweek_key: str,
    raise_at_utc: datetime,
) -> HorizonResolution:
    """Is a retroactive event for this workweek still notifiable?

    Resolves the three inputs the pure comparison needs — the canonical
    workweek end, the frozen zone and the governing horizon — and refuses
    rather than guessing when any of them cannot be established.
    """
    from app.eval_week.membership import canonical_dates
    from app.policy_version import resolve_policy_as_of

    dates = canonical_dates(workweek_key)
    if not dates:
        return HorizonResolution(
            outcome=HorizonOutcome.REFUSED,
            workweek_key=workweek_key,
            reason=REASON_HORIZON_MALFORMED_WORKWEEK,
            detail=(f"{workweek_key!r} is not a canonical YYYY-MM-DD workweek "
                    f"start, so it denotes no end date"))
    week_start, week_end = dates[0], dates[-1]

    zone_name = _frozen_timezone(
        db, organization_id=organization_id, user_id=user_id,
        workweek_key=workweek_key)
    if zone_name is None:
        return HorizonResolution(
            outcome=HorizonOutcome.REFUSED,
            workweek_key=workweek_key,
            reason=REASON_HORIZON_TIMEZONE_UNRESOLVED,
            detail=(f"the ledger for {workweek_key} carries no single frozen "
                    f"timezone; §21.5's calendar days have no local frame"))

    # The policy governing the WEEK, anchored at its start — the same anchor
    # `resolve_threshold` uses for "which rule governs this week", so one
    # convention answers both. Never today's policy (D-7.1).
    policy = resolve_policy_as_of(
        db, organization_id=organization_id, as_of=week_start)
    if not policy.is_resolved:
        return HorizonResolution(
            outcome=HorizonOutcome.REFUSED,
            workweek_key=workweek_key,
            reason=REASON_HORIZON_POLICY_UNRESOLVED,
            detail=(f"no policy governs the workweek beginning "
                    f"{week_start.isoformat()} ({policy.outcome.value}); the "
                    f"horizon is versioned policy content and has no other "
                    f"source"))

    if policy.retroactive_horizon_days is None:
        # NOT 90. The capture default is what a Tenant is GIVEN when they state
        # nothing; substituting it here would assert a bound this policy never
        # configured, and a reader could no longer tell the two apart.
        return HorizonResolution(
            outcome=HorizonOutcome.REFUSED,
            workweek_key=workweek_key,
            reason=REASON_HORIZON_NOT_CONFIGURED,
            detail=(f"policy {policy.policy_version!r} governs the workweek "
                    f"but records no retroactive_horizon_days; an absent "
                    f"horizon is not the default, and is not unbounded"))

    return horizon_decision(
        raise_at_utc=raise_at_utc,
        workweek_end_date=week_end,
        timezone_iana=zone_name,
        retroactive_horizon_days=int(policy.retroactive_horizon_days),
        workweek_key=workweek_key,
        policy_version=policy.policy_version,
    )


__all__ = [
    "HorizonOutcome",
    "HorizonResolution",
    "horizon_decision",
    "resolve_retroactive_horizon",
    "REASON_HORIZON_POLICY_UNRESOLVED",
    "REASON_HORIZON_NOT_CONFIGURED",
    "REASON_HORIZON_TIMEZONE_UNRESOLVED",
    "REASON_HORIZON_MALFORMED_WORKWEEK",
]
