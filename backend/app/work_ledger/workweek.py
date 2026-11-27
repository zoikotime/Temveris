"""
The canonical workweek-key derivation — MODEL-WORKWEEK.
=======================================================

Owner ruling W4 (W4-a). One function, one format, one production path.

THE PROBLEM THIS CLOSES
-----------------------
``WorkDayLedger.workweek_key`` was a caller-supplied string. Nothing derived it,
nothing validated it, and the only code that populated it in practice used
``date.isocalendar()`` — which hard-codes a Monday boundary and would therefore
have grouped a Sunday-start organization's days into the wrong weeks. Two
callers could disagree about which week a day belonged to and neither would be
wrong by any rule the platform stated.

THE FORMAT (W4-3)
-----------------
The key is the **local workweek START DATE**, ISO-formatted::

    "2026-08-31"        Monday-start organization, any day of that week
    "2026-08-30"        Sunday-start organization, same physical week

Ten characters, comfortably inside ``String(16)``.

ISO week notation (``2026-W36``) was deliberately NOT kept. It is defined only
for a Monday boundary, so for a Sunday-start organization it would either be
wrong or would need a private redefinition of what "W36" means — a second
vocabulary for one fact. A start date is unambiguous under every configured
boundary and sorts chronologically as text.

It also carries its own provenance, which is why no extra column records the
boundary regime: **the weekday of the key IS the regime that minted it.** A key
of ``2026-08-31`` is a Monday, so it was minted under a MONDAY start; there is
no configuration under which it could have been minted otherwise. See
:func:`boundary_regime` — it reads that fact back rather than storing it twice.

THE HISTORICAL ANCHOR (W4-1, W4-5)
----------------------------------
This function derives a key at SEAL TIME from the configuration in force then.
Once written, that key IS the day's workweek identity, permanently. A later
change to ``schedule.week_start_day`` does not move it, and historical grouping
must read the stored key rather than recompute one.

So there are two distinct questions, and only one of them belongs here:

    "which week does today belong to?"      -> week_start_date(), current config
    "which week did that sealed day
     belong to?"                            -> the persisted workweek_key

A **correction inherits** the persisted key rather than re-deriving one (owner
Ruling 4). ``supersede_day`` previously re-derived it, which let a corrected day
silently leave its historical week after a boundary change — the mutation §12.4
forbids: "Historical workweeks remain readable and reproducible indefinitely,
and change only by the addition of correction events, never by mutation."

W4-7 still permits a boundary change to create a boundary in FUTURE identity.
:func:`assert_boundary_change_allowed` is what keeps that change at a week
boundary, so it can never bisect a week already in progress.

STRICT HERE, LENIENT THERE
--------------------------
``schedule_policy.week_start_date()`` logs and falls back to MONDAY when the
setting is unusable. That is right for the weekly rollups and pay-period windows
that call it — a report with a defaulted window is better than a 500.

It is wrong for this module. §12.1 requires that "Every Tenant MUST have a
defined workweek before this control can be enabled", so an ECO day sealed
under a guessed boundary would carry an identity nobody configured. This module
therefore resolves the setting itself and FAILS CLOSED, and the lenient helper
keeps its existing contract for its existing callers.

WHAT IT IS NOT
--------------
Not a historical resolver: it reads current configuration, which is correct at
the moment of sealing and wrong for re-deriving a past day. Not a week-duration
calculation — GV-11's "169 hours" is an asserted property of a DST week, not an
input to identity, and date arithmetic is unaffected by a clock change. It does
not attribute a shift to a date either: that is the producer's, and it arrives
here already decided (owner Ruling 2).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

#: The organization-scope setting that owns the boundary. Organization-only by
#: owner Ruling 3 — see ``app.settings.scope.ORG_ONLY_KEYS``, which is what
#: refuses a team- or worker-scoped write.
WEEK_START_SETTING_KEY = "schedule.week_start_day"


class WorkweekBoundaryError(ValueError):
    """The organization's workweek boundary cannot be used as configured."""


class WorkweekBoundaryUnset(WorkweekBoundaryError):
    """No boundary is configured. §12.1 requires one before ECO is enabled."""


class WorkweekBoundaryMalformed(WorkweekBoundaryError):
    """The configured value is not a weekday name."""


class WorkweekBoundaryScopeInvalid(WorkweekBoundaryError):
    """A team- or worker-scoped override exists for an organization-only key.

    Raised rather than ignored. The row cannot be honoured — the boundary is
    organization-level (Ruling 3) — and silently ignoring it would let an
    administrator believe a worker's week had moved when it had not.
    """


class WorkweekBoundaryChangeRefused(WorkweekBoundaryError):
    """The change would take effect inside an already-open workweek."""


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _weekday_names() -> tuple:
    """The platform's weekday vocabulary, borrowed rather than restated."""
    from app.schedule_policy import keys as schedule_keys

    return tuple(schedule_keys.WEEKDAY_NAMES)


def _configured_rows(db, organization_id):
    """Every stored value for the boundary key in this organization.

    Returns ``(organization_value, has_narrower_scope)``. Reading both in one
    pass is what lets an out-of-scope override be REFUSED rather than silently
    skipped, which is the whole point of the organization-only ruling.
    """
    from database import models

    rows = (
        db.query(models.AppSettings)
        .filter(models.AppSettings.organization_id == organization_id,
                models.AppSettings.key == WEEK_START_SETTING_KEY)
        .all()
    )
    org_value = None
    narrower = False
    for row in rows:
        if row.team_key is None and row.user_id is None:
            org_value = row.value
        else:
            narrower = True
    return org_value, narrower


def resolve_week_start_day(db, *, organization_id) -> str:
    """The organization's configured first day of the week, or refuse.

    Fails closed on every way the configuration can be unusable: absent,
    blank, not a weekday name, or overridden at a scope the boundary does not
    honour. No value is defaulted and none is guessed — a sealed ECO day must
    never carry a week identity that nobody configured (§12.1).
    """
    org_value, narrower = _configured_rows(db, organization_id)

    if narrower:
        raise WorkweekBoundaryScopeInvalid(
            f"{WEEK_START_SETTING_KEY} has a team- or worker-scoped override "
            f"in organization {organization_id}, but the workweek boundary is "
            f"organization-level. Remove the override; it was never honoured."
        )

    if org_value is None or not str(org_value).strip():
        raise WorkweekBoundaryUnset(
            f"organization {organization_id} has no {WEEK_START_SETTING_KEY}. "
            f"§12.1 requires a defined workweek before Early Clock-Out can be "
            f"enabled, so no day may be sealed under an assumed boundary."
        )

    name = str(org_value).strip().upper()
    if name not in _weekday_names():
        raise WorkweekBoundaryMalformed(
            f"{WEEK_START_SETTING_KEY}={org_value!r} is not a weekday name in "
            f"organization {organization_id}. Refused rather than defaulted: a "
            f"guessed boundary produces a week identity nobody chose."
        )
    return name


def week_start_of(db, *, organization_id, on_date: date | datetime) -> date:
    """The first day of the week containing ``on_date``, strictly resolved.

    Pure date arithmetic on the configured start day, so a DST transition
    inside the week cannot move the boundary.
    """
    names = _weekday_names()
    start_index = names.index(resolve_week_start_day(
        db, organization_id=organization_id))
    on = _as_date(on_date)
    return on - timedelta(days=(on.weekday() - start_index) % 7)


def canonical_workweek_key(db, *, organization_id, work_date: date | datetime) -> str:
    """The workweek identity for a Worker-day, from the organization's config.

    ``work_date`` is the START_DATE-attributed local calendar date, decided by
    the producer under the rule already in force (§12.3) — so a shift running
    22:00 Sunday to 06:00 Monday resolves to Sunday's week and is counted once,
    never twice (R-12.3.5). Nothing here inspects a session or re-decides that.

    Deterministic and clock-independent: the same organization and date always
    produce the same key, and it refuses rather than defaults when the
    boundary is unusable.
    """
    return week_start_of(
        db, organization_id=organization_id, on_date=work_date).isoformat()


def boundary_regime(workweek_key: str):
    """The week-start day that minted this key, or ``None`` if unparsable.

    The key IS the week's start date, so its weekday is the boundary regime in
    force when the day was sealed — recovered from what was already persisted
    rather than stored a second time. This is what makes a sealed week's
    boundary answerable without reading today's configuration, and why owner
    Ruling 4 needed no extra column for it.
    """
    if not isinstance(workweek_key, str):
        return None
    candidate = workweek_key.strip()
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return None
    try:
        start = date.fromisoformat(candidate)
    except (ValueError, TypeError):
        return None
    if start.isoformat() != candidate:
        return None
    return _weekday_names()[start.weekday()]


def assert_boundary_change_allowed(db, *, organization_id, new_value) -> None:
    """Refuse a boundary change that would land inside an open workweek.

    Owner Ruling 5. A change applied mid-week splits one physical week into two
    identities: days already sealed keep the old key, days sealed afterwards
    get a new one, and each fragment then folds and completes on its own. Two
    weekly evaluations would arise from one week that the Worker worked once.

    The change is permitted only when the open workweek has not yet progressed
    past its first day, so no day of it can already have been sealed under the
    outgoing boundary.

    A no-op passes. So does a change made while the CURRENT value is unusable:
    there is no validly-open week to protect, and refusing would leave an
    organization unable to repair its own configuration.
    """
    from app._tz import local_date

    names = _weekday_names()
    candidate = str(new_value or "").strip().upper()
    if candidate not in names:
        raise WorkweekBoundaryMalformed(
            f"{WEEK_START_SETTING_KEY}={new_value!r} is not a weekday name. "
            f"Expected one of {', '.join(names)}."
        )

    try:
        current = resolve_week_start_day(db, organization_id=organization_id)
    except WorkweekBoundaryScopeInvalid:
        raise
    except WorkweekBoundaryError:
        # Unset or malformed: nothing coherent is open, so this is a repair.
        return

    if current == candidate:
        return

    today = local_date(db, organization_id)
    open_week_start = week_start_of(
        db, organization_id=organization_id, on_date=today)
    if open_week_start != today:
        raise WorkweekBoundaryChangeRefused(
            f"the workweek beginning {open_week_start.isoformat()} is already "
            f"open in organization {organization_id}; changing "
            f"{WEEK_START_SETTING_KEY} from {current} to {candidate} today "
            f"({today.isoformat()}) would split it into two identities. Apply "
            f"the change on the first day of a workweek."
        )


__all__ = [
    "WEEK_START_SETTING_KEY",
    "WorkweekBoundaryError",
    "WorkweekBoundaryUnset",
    "WorkweekBoundaryMalformed",
    "WorkweekBoundaryScopeInvalid",
    "WorkweekBoundaryChangeRefused",
    "assert_boundary_change_allowed",
    "boundary_regime",
    "canonical_workweek_key",
    "resolve_week_start_day",
    "week_start_of",
]
