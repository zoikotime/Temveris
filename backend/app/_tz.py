"""
Per-organization timezone resolution.
=====================================
Attendance thresholds (shift start, grace, the absent cutoff) and day boundaries
are wall-clock concepts, so they need a timezone. Today the codebase hardcodes
IST in 20 files; this module is the single place that answers "what is this
organization's local time?" so those can be migrated onto it later.

Source of truth is ``OrgSettings.default_timezone``, which is already editable
under Company & Workspace Profile. There is deliberately **no** second timezone
setting.

Two realities this has to survive
---------------------------------
1. **The stored value is free text, not validated IANA.** Production holds
   ``'California, Sacramento'`` for one org and NULL for two others. Anything
   that is not a loadable zone falls back to Asia/Kolkata — which is exactly
   what every hardcoded ``timedelta(hours=5, minutes=30)`` in the codebase does
   today, so the fallback is behaviour-preserving rather than a guess.
2. **``zoneinfo`` may have no tz database.** ``tzdata`` IS now pinned in
   requirements.txt, so ``ZoneInfo("America/New_York")`` resolves on the
   ``python:3.12-slim`` runtime. The fixed +05:30 degradation below survives
   only as a last-resort guard for a broken image; if it ever fires, the
   WARNING it logs is a deployment fault, not a configuration one.

This module is THE source of an organization's business wall-clock. Any code
that decides a working day, an absence cutoff, a shift boundary or a report's
"today" must go through it rather than re-deriving an offset.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional, Tuple

_log = logging.getLogger("zoiko.tz")

# The offset every existing hardcoded IST literal uses. Keeping the same value
# is what makes the fallback path a no-op relative to current behaviour.
IST_OFFSET = timedelta(hours=5, minutes=30)
_IST_FIXED: tzinfo = timezone(IST_OFFSET, "IST")

# Used when an org has no usable timezone configured.
FALLBACK_TZ_NAME = "Asia/Kolkata"


def _load_zone(name: Optional[str]) -> Optional[tzinfo]:
    """Return a tzinfo for an IANA name, or ``None`` if it is unusable.

    ``None`` covers all of: empty value, free-text that is not a zone name, and
    a runtime with no tz database installed. Never raises.
    """
    if not name or not str(name).strip():
        return None
    try:
        from zoneinfo import ZoneInfo  # stdlib on 3.9+; needs a tz database
        return ZoneInfo(str(name).strip())
    except Exception:
        return None


def is_valid_timezone(name: Optional[str]) -> bool:
    """True when ``name`` is an IANA zone this runtime can actually load.

    THE validator for every write path (``PUT /org/settings``, super-admin
    organization creation, provisioning). It exists so "what counts as a
    timezone" has one answer, and so that answer is the same one ``_resolve``
    uses at read time — a value that passes here can never fall back.

    ``'California, Sacramento'``, ``'IST'`` and ``'GMT+5.5'`` are all False;
    ``'Asia/Kolkata'``, ``'America/New_York'``, ``'UTC'`` are True. Empty is
    False — callers decide whether empty means "clear it" or "reject it",
    because those differ between an update and a creation.
    """
    return _load_zone(name) is not None


# ──────────────────────────────────────────────────────────────────────────
# Resolution + cache
#
# `/employees/team` is polled every 30s per admin and the attendance monitor
# resolves a timezone per organization per tick, so an uncached lookup would
# add a round-trip to Supabase on every one. The value changes only when an
# admin edits Company & Workspace Profile, which calls invalidate_tz_cache().
# ──────────────────────────────────────────────────────────────────────────
_TZ_TTL_SECONDS = 60.0
_tz_cache: dict = {}   # org_id -> (name, tzinfo, expires_at_monotonic)


def invalidate_tz_cache(org_id=None) -> None:
    """Drop cached timezone(s). Called after OrgSettings.default_timezone is
    written so the change is live immediately rather than after the TTL."""
    if org_id is None:
        _tz_cache.clear()
    else:
        _tz_cache.pop(org_id, None)


def _use_cache(db) -> bool:
    # SQLite is the in-process unit-test DB — there is no round-trip to save,
    # and a process-global TTL cache would leak values across tests that
    # set-then-read. Same rule as app.breaks.governance._cached_setting_value.
    try:
        return db.bind.dialect.name != "sqlite"
    except Exception:
        return True


def _resolve(db, org_id) -> Tuple[str, tzinfo]:
    """(name, tzinfo) for this org. One implementation so the displayed name and
    the offset actually used can never disagree."""
    now = _time.monotonic()
    cached = _tz_cache.get(org_id) if _use_cache(db) else None
    if cached and cached[2] > now:
        return cached[0], cached[1]

    configured = None
    try:
        from database import models
        row = (
            db.query(models.OrgSettings.default_timezone)
            .filter(models.OrgSettings.organization_id == org_id)
            .first()
        )
        configured = row[0] if row else None
    except Exception:
        configured = None

    zone = _load_zone(configured)
    if zone is not None:
        name = str(configured).strip()
    else:
        if configured and str(configured).strip():
            # Free text like 'California, Sacramento' — an admin believes they
            # configured a timezone and every business decision for this tenant
            # is silently being made in another one. That must be visible.
            _log.warning(
                "tz: organization %s has an unusable default_timezone %r; "
                "falling back to %s", org_id, configured, FALLBACK_TZ_NAME,
            )
        else:
            # NULL was previously silent, which is how five organizations came
            # to be evaluated on India's clock without anyone seeing it. An
            # unconfigured tenant is a provisioning fault, not a default.
            _log.warning(
                "tz: organization %s has NO default_timezone configured; "
                "falling back to %s. Attendance, absence cutoffs and day "
                "boundaries for this tenant are being evaluated on that clock.",
                org_id, FALLBACK_TZ_NAME,
            )
        name = FALLBACK_TZ_NAME
        zone = _load_zone(FALLBACK_TZ_NAME)
        if zone is None:
            _log.warning("tz: no tz database available (tzdata missing); "
                         "using a fixed +05:30 offset for organization %s", org_id)
            zone = _IST_FIXED

    if _use_cache(db):
        _tz_cache[org_id] = (name, zone, now + _TZ_TTL_SECONDS)
    return name, zone


def org_timezone_name(db, org_id) -> str:
    """The IANA name this org's local time is computed in.

    Returns the configured value only when it actually loads, so callers never
    display a name that does not match the offset being used.
    """
    return _resolve(db, org_id)[0]


def org_timezone(db, org_id) -> tzinfo:
    """This org's tzinfo. Falls back to the documented system default
    (``FALLBACK_TZ_NAME``) when the configured value is unusable."""
    return _resolve(db, org_id)[1]


def local_now(db, org_id) -> datetime:
    """Current time as this org's wall clock (tz-aware)."""
    return datetime.now(timezone.utc).astimezone(org_timezone(db, org_id))


def local_date(db, org_id) -> date:
    """Today's calendar date in this org's local time — the "today" every
    dashboard bucket, absence cutoff and daily report must agree on."""
    return local_now(db, org_id).date()


def local_day_bounds(db, org_id, on_date: date) -> Tuple[datetime, datetime]:
    """UTC ``[start, end)`` instants of the org-local calendar day ``on_date``.

    Replaces the ``datetime.combine(d, min.time(), tzinfo=utc) - 5h30m`` idiom
    repeated across the codebase, which only happens to be correct for IST.
    """
    tz = org_timezone(db, org_id)
    start_local = datetime(on_date.year, on_date.month, on_date.day, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc)
    # Add a day in local terms, not UTC, so a DST transition keeps the boundary
    # on local midnight instead of drifting by an hour.
    end_local = datetime(on_date.year, on_date.month, on_date.day, tzinfo=tz) + timedelta(days=1)
    return start_utc, end_local.astimezone(timezone.utc)


def to_local(db, org_id, dt: Optional[datetime]) -> Optional[datetime]:
    """Project a UTC (or naive-treated-as-UTC) instant into org-local time."""
    if dt is None:
        return None
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return aware.astimezone(org_timezone(db, org_id))
