"""
Temveris – Organization holiday calendar (database side)
=========================================================
One definition of "which stored holidays apply to this organization", shared by
every consumer instead of re-expressed at each call site:

  * ``app/holidays/router.py``            – the admin Manage Holidays page
  * ``app/schedule_policy/resolver.py``   – working-day resolution
  * ``app/leave/*``                       – leave day cost
  * ``app/attendance_policy/resolvers.py``– clock-out policy
  * ``app/reports``, ``app/payroll``      – working-day counters

The country-scoping rule
------------------------
``holidays.country_code`` is nullable and NULL means **unscoped**. A holiday
applies to an organization when:

    country_code IS NULL                     (unscoped / pre-migration rows)
    OR country_code = <org's primary_country>

Employees have no country of their own, so the organization's
``OrgSettings.primary_country`` is the only defensible per-employee answer
today. When it is unset, every holiday applies — that is exactly the behaviour
before country codes existed, so an organization that never configured a
country sees no change at all.

This is what stops a workspace that keeps both a US and an Indian calendar from
marking Diwali non-working for its US staff, while guaranteeing that adding
``country_code`` could not remove a single holiday from any existing consumer:
before the migration every row is NULL, so every row still matches.

Column-scoped queries on purpose
--------------------------------
Every query here names explicit columns rather than loading the whole entity.
A full-entity load emits every mapped column, so a model column not yet present
in the database takes down the statement — and, as
``attendance_policy/resolvers.py`` documents at length, a swallowed failure
there poisons the caller's transaction. Naming columns keeps this module
working against a database that has not run the newest migration yet.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Set

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import models

_log = logging.getLogger("zoikotime.holidays.org_calendar")


def org_primary_country(db: Session, org_id) -> Optional[str]:
    """The organization's ISO-2 country, upper-cased; None when unset.

    Propagates database errors on purpose. Swallowing one here would silently
    widen the country scope to "every holiday applies" while leaving the
    session in whatever state the failure left it — the caller's recovery
    strategy (savepoint, or rollback via ``holiday_dates_or_empty``) is the
    right place to decide.
    """
    if not org_id:
        return None
    row = (
        db.query(models.OrgSettings.primary_country)
        .filter(models.OrgSettings.organization_id == org_id)
        .first()
    )
    code = (row[0] if row else None) or ""
    return code.strip().upper() or None


def country_scope_filter(db: Session, org_id, country_code: Optional[str] = None):
    """SQLAlchemy condition selecting the holidays that apply to this org.

    ``country_code`` overrides the organization's own country — the admin page
    passes it so an admin can inspect any calendar they maintain, not only the
    one their staff are measured against. Returns ``None`` when no country
    restriction applies (org country unset and no override), meaning "every
    holiday counts".
    """
    target = (country_code or "").strip().upper() or org_primary_country(db, org_id)
    if not target:
        return None
    return or_(
        models.Holiday.country_code.is_(None),
        models.Holiday.country_code == target,
    )


def _base_query(db: Session, org_id, *columns):
    return db.query(*columns).filter(models.Holiday.organization_id == org_id)


def holiday_dates(
    db: Session,
    org_id,
    *,
    dates: Optional[Sequence[date]] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    year: Optional[int] = None,
    country_code: Optional[str] = None,
    apply_country_scope: bool = True,
) -> Set[date]:
    """The set of holiday dates that apply to this organization.

    Any combination of ``dates`` / ``start`` / ``end`` / ``year`` narrows the
    window; passing none returns every holiday the organization holds.

    **Raises** on a database error rather than swallowing it. Callers that need
    to degrade choose how: ``holiday_dates_or_empty`` below rolls the session
    back, while ``schedule_policy.resolver`` wraps the call in its own SAVEPOINT
    (``_safe_load``) precisely because a full rollback there would discard
    uncommitted work the caller owns. Picking that recovery strategy for them
    would be wrong, so this function does not.
    """
    if not org_id:
        return set()
    if dates is not None and not dates:
        return set()

    query = _base_query(db, org_id, models.Holiday.date)
    if dates is not None:
        query = query.filter(models.Holiday.date.in_(list(dates)))
    if start is not None:
        query = query.filter(models.Holiday.date >= start)
    if end is not None:
        query = query.filter(models.Holiday.date <= end)
    if year is not None:
        query = query.filter(
            models.Holiday.date >= date(int(year), 1, 1),
            models.Holiday.date <= date(int(year), 12, 31),
        )
    if apply_country_scope:
        scope = country_scope_filter(db, org_id, country_code)
        if scope is not None:
            query = query.filter(scope)
    return {row[0] for row in query.all() if row[0] is not None}


def holiday_dates_or_empty(db: Session, org_id, **kwargs) -> Set[date]:
    """``holiday_dates`` that degrades to an empty set and rolls back on failure.

    For callers with no savepoint of their own, where a holiday-lookup failure
    must not abort whatever they do next. ``attendance_policy.resolvers`` learned
    this the hard way: it swallowed the error WITHOUT rolling back, so the
    session stayed poisoned, the next query in ``clock_out`` failed, and the
    Early Clock Out gate was silently skipped for every worker.
    """
    try:
        return holiday_dates(db, org_id, **kwargs)
    except Exception:
        _log.warning("holidays.org_calendar holiday_dates failed org=%s", org_id,
                     exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return set()


def holiday_names_by_date(
    db: Session,
    org_id,
    start: date,
    end: date,
    *,
    country_code: Optional[str] = None,
) -> Dict[date, str]:
    """``{date: name}`` for a range. Used by reports for holiday labelling.

    A date holding several holidays keeps the first by name order, matching the
    single-label shape reports already expect.
    """
    if not org_id:
        return {}
    try:
        query = _base_query(db, org_id, models.Holiday.date, models.Holiday.name).filter(
            models.Holiday.date >= start,
            models.Holiday.date <= end,
        )
        scope = country_scope_filter(db, org_id, country_code)
        if scope is not None:
            query = query.filter(scope)
        out: Dict[date, str] = {}
        for row_date, row_name in query.order_by(
            models.Holiday.date, models.Holiday.name
        ).all():
            out.setdefault(row_date, row_name)
        return out
    except Exception:
        _log.warning("holidays.org_calendar holiday_names_by_date failed org=%s",
                     org_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return {}


def list_org_holidays(
    db: Session,
    org_id,
    *,
    year: Optional[int] = None,
    country_code: Optional[str] = None,
    apply_country_scope: bool = True,
) -> List[Dict]:
    """Stored holidays as normalized dicts, ordered by date then name.

    ``country_code`` here is an explicit filter for the admin page, not the
    employee-facing scope: passing "US" lists the US calendar plus unscoped
    rows, which is what an admin maintaining several calendars expects to see.
    """
    from app.holidays import catalog

    if not org_id:
        return []
    query = _base_query(
        db,
        org_id,
        models.Holiday.id,
        models.Holiday.name,
        models.Holiday.date,
        models.Holiday.holiday_type,
        models.Holiday.description,
        models.Holiday.country_code,
        models.Holiday.created_at,
    )
    if year is not None:
        query = query.filter(
            models.Holiday.date >= date(int(year), 1, 1),
            models.Holiday.date <= date(int(year), 12, 31),
        )
    if country_code and apply_country_scope:
        query = query.filter(
            or_(
                models.Holiday.country_code.is_(None),
                models.Holiday.country_code == country_code.strip().upper(),
            )
        )

    rows = query.order_by(models.Holiday.date.asc(), models.Holiday.name.asc()).all()
    out = []
    for row in rows:
        normalized = catalog.normalize_entry(
            {
                "id": row.id,
                "name": row.name,
                "date": row.date.strftime("%Y-%m-%d") if row.date else "",
                "holiday_type": row.holiday_type,
                "description": row.description,
            },
            source="organization",
        )
        normalized["country_code"] = row.country_code
        normalized["created_at"] = row.created_at.isoformat() if row.created_at else None
        out.append(normalized)
    return out


def distinct_country_codes(db: Session, org_id) -> List[str]:
    """ISO-2 codes this organization actually has holidays for.

    Drives the "you maintain these calendars" hint on the admin page without a
    second round trip, and tells the classification script which organizations
    have already been scoped.
    """
    if not org_id:
        return []
    try:
        rows = (
            db.query(models.Holiday.country_code)
            .filter(
                models.Holiday.organization_id == org_id,
                models.Holiday.country_code.isnot(None),
            )
            .distinct()
            .all()
        )
        return sorted({r[0] for r in rows if r[0]})
    except Exception:
        _log.warning("holidays.org_calendar distinct_country_codes failed org=%s",
                     org_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return []


def find_duplicate(
    db: Session,
    org_id,
    on_date: date,
    name: str,
    country_code: Optional[str],
    *,
    exclude_id: Optional[int] = None,
):
    """The row that would collide with this (country, date, name), if any.

    Mirrors the database's uniqueness rule exactly so the API can answer 409
    with a readable message instead of relying on an IntegrityError. Note this
    is NOT "a holiday exists on this date": two distinct holidays may share a
    date, and two countries certainly may.
    """
    query = _base_query(db, org_id, models.Holiday.id, models.Holiday.name).filter(
        models.Holiday.date == on_date,
        models.Holiday.name == (name or "").strip(),
    )
    normalized_country = (country_code or "").strip().upper() or None
    if normalized_country is None:
        query = query.filter(models.Holiday.country_code.is_(None))
    else:
        query = query.filter(models.Holiday.country_code == normalized_country)
    if exclude_id is not None:
        query = query.filter(models.Holiday.id != exclude_id)
    return query.first()


def iter_years(db: Session, org_id) -> Iterable[int]:
    """Years this organization has holidays in, ascending."""
    if not org_id:
        return []
    rows = (
        db.query(models.Holiday.date)
        .filter(models.Holiday.organization_id == org_id)
        .all()
    )
    return sorted({r[0].year for r in rows if r[0] is not None})
